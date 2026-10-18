# Редакционный цикл без MTProto

Путь: официальный источник → `relocant-ingest` → PostgreSQL `raw_item` →
`relocant-editorial` → Gemini structured rank/claims/verify/draft →
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

- Сохраняются снимок доказательств, URL, цитаты, решения и черновик.
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
