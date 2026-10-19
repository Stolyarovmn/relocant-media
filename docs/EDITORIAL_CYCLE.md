# Редакционный цикл без MTProto

Путь: официальный/разрешённый источник → `relocant-ingest` → PostgreSQL `raw_item` →
canonical URL + normalized content SHA256 → event-level dedup/evidence bundle →
`relocant-editorial` → Gemini structured rank/claims/verify/draft → deterministic guards →
`editorial_run` и `post` → staging publisher через Bot API.

## Безопасный запуск

После установки зависимостей и `alembic upgrade head`:

```sh
relocant-ingest --city belgrade --sources cities/belgrade/sources
relocant-editorial --raw-item-id 1
relocant-editorial --raw-item-id 1 --transport fake
```

ID выбирается из `raw_item`; пример `1` не означает автоматический выбор новости.
Нужны `DATABASE_URL`, `GEMINI_API_KEY`, при необходимости `GEMINI_MODEL`.
По умолчанию выполняется dry-run: Gemini вызывается, результат сохраняется,
Telegram не вызывается. Fake заменяет HTTP-транспорт Bot API на `httpx.MockTransport`:
проверяет формат настоящего запроса, но не отправляет сообщения и не расходует
квитанцию реальной доставки. Gemini при этом остаётся реальным и может расходовать
квоту; интеграционные тесты подменяют и Gemini, и HTTP.

Настройки `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` и MTProto-сессия не нужны.
Оставьте `TELEGRAM_MTPROTO_ENABLED=false`. Аналитика с отсутствующим провайдером
остаётся `blocked`, не мешая редакционному циклу.

## Event-level deduplication

При ingest сохраняются исходный URL, canonical URL и SHA256 нормализованного
`title + body`. Экспорт кандидатов и редакционный сервис используют их как
детерминированные признаки дубля. Для разных источников дополнительно допускается
консервативное объединение похожих заголовков в пределах 48 часов. Похожесть
заголовка никогда не объединяет две разные публикации одного и того же источника.

Один event может содержать несколько независимых `raw_item`. В AI pipeline передаётся
не более одного документа от каждого источника. В `editorial_run.evidence.documents`
сохраняется полный использованный bundle вместе с `raw_item_id` и source-policy.
Перед отправкой каждый его элемент повторно сверяется с текущей БД. Изменение текста,
URL, даты публикации, удаление или отключение любого использованного источника
блокирует доставку.

## Детерминированные publication guards

LLM не определяет право на автоматическую публикацию. Даже если AI pipeline вернул
`READY`, приложение переводит материал в `human_review`, если выполняется хотя бы
одно условие:

- anchor-source имеет `auto_publish: false`;
- любой использованный источник имеет `policy.high_risk: true`;
- среди evidence нет источника с `ownership: official`.

Эти же условия проверяются ещё раз непосредственно перед Telegram-вызовом. Для строки
`Источник:` выбирается официальный документ из evidence bundle, если он есть.

Высокорисковые категории (`immigration`, `law`, `tax`, `medicine`, `banking`,
`politics`) по-прежнему требуют human review независимо от source-policy. Таким
образом, хороший ответ одной модели или даже согласие writer/verifier не обходят
программные ограничения.

## Версии редакционных правил

Новые `post` и `editorial_run` получают `editorial_rule_version=belgrade-editorial-v2`.
Записи, созданные до миграции `0007`, честно маркируются `legacy-v1`. Daily report
показывает число публикаций, views/interactions и observed engagement отдельно по
`editorial_rule_version`. Это позволяет сравнивать версии правил, но не доказывает
причинность: на метрики одновременно влияют тема, время публикации, охват и другие
факторы.

## Отдельное подключение staging

Только по отдельному решению владельца и с тестовым каналом:

```sh
relocant-editorial --raw-item-id 1 --transport bot-api --send-staging
```

Требуются Bot API token, numeric `TELEGRAM_STAGING_CHANNEL_ID` и numeric
`TELEGRAM_CHANNEL_ID` для исключения production-адресата. Оба ID имеют формат
`-100…` и должны различаться. `APP_ENV=prod` и `production` запрещены.
Без `--send-staging` даже `--transport bot-api` остаётся dry-run.
Работа над репозиторием не требует исполнения этой команды.

## Гарантии и восстановление

- Сохраняются event evidence bundle, URL, цитаты, решения и черновик.
- Повторная обработка того же raw item возвращает сохранённый результат без Gemini.
- `human_review`, `blocked`, `skipped` никогда не отправляются автоматически.
  Интерфейс ручного одобрения пока не реализован.
- Изменившиеся доказательства, отключённый источник или несовпадение города
  блокируют отправку. Изменение источника после проверки требует нового ревью;
  автоматического пересоздания редакционного результата нет.
- Перед внешней отправкой фиксируется `sending`; успех даёт `sent` и отдельный
  staging message id. Поля production-публикации у `post` не заполняются.
- Тайм-аут/ошибка оставляют `uncertain`; авария процесса может оставить `sending`.
  Автоматического повтора нет, поскольку Bot API не предоставляет ключ идемпотентности.
  Владелец должен сверить фактическое сообщение в staging с квитанцией в БД.
  Нельзя сбрасывать состояние в `pending` без проверки: это может создать дубль.
- Длинные сообщения свыше 4096 UTF-16 единиц блокируются целиком.
- Scheduler отдельно ведёт leases, run journal и экспоненциальный retry/backoff для
  безопасно повторяемых внутренних задач. Этот retry не применяется к неопределённой
  внешней доставке Telegram.

## Проверки без внешних сервисов

Используйте отдельную тестовую БД: тесты очищают таблицы.

```sh
alembic upgrade head
ruff check .
mypy src
pytest -q
```

`TEST_DATABASE_URL` должен указывать на мигрированную тестовую PostgreSQL.
Без него интеграционные тесты пропускаются. CI всегда задаёт этот параметр,
поднимает PostgreSQL и проверяет upgrade → downgrade → upgrade.
Production Telegram и Gemini в тестах не вызываются.
