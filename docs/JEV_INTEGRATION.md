# TypeSafe Jev — отключаемый shadow-наблюдатель

Jev — неавторитетный semantic observer. Он не меняет очередь, candidate selection,
Gemini rank/claims/verify/draft, deterministic dedup или publication policy.

**Единственный выключатель — `JEV_ENABLED`.** При выключенном Jev адаптер не читает
`TYPESAFE_API_KEY`, не создаёт HTTP-клиент и не обращается к TypeSafe.

## GitHub Actions configuration

Настройки, запрошенные владельцем:

- Repository variable `JEV_ENABLED=true` (или `false` для полного выключения).
- Repository secret `TYPESAFE_API_KEY`.

Не переносите ключ на DB-сервер, в Git, PR, Compose, логи или артефакты.

Workflow [jev-smoke.yml](../.github/workflows/jev-smoke.yml) запускается вручную или при
изменении своего workflow/адаптера на `main`. Он использует тот же HTTP-адаптер, что и
rank/dedup, но только фиксированный публичный пример из
[официальной документации](https://docs.typesafe.ai/api/system-one). Один включённый запуск
делает не более одного запроса, без retry. При `JEV_ENABLED=false` secret не передаётся
шагу, запрос не выполняется. При отсутствии variable используется `false`.

Проверка не требует `DATABASE_URL`, Gemini или Telegram credentials. Она не читает БД,
не собирает новости, не импортирует публикационные модули и не отправляет сообщения.
`TELEGRAM_RUNTIME_ENABLED`, `TELEGRAM_MTPROTO_ENABLED` и `EDITORIAL_SEND_ENABLED`
принудительно выключены. Ошибка API даёт failed smoke; нормальный редакционный
shadow-слой при этом по-прежнему fail-open.

Artifact `jev-smoke-<run_id>-<attempt>/manifest.json` содержит:

```text
kind = jev_api_smoke
production_benchmark = false
newsroom_state_accessed = false
telegram_transport = none
jev_shadow.status/model/duration_ms/input_tokens/output_tokens/probabilities
```

`ok` подтверждает успешный HTTP-вызов и разбор ответа; `disabled` — отсутствие вызова;
`missing_api_key` — отсутствие доступного ключа; `unavailable` — сетевую/API/контрактную
ошибку. Exception text, credentials и полный request/response body не сохраняются.
Это проверка подключения, **не benchmark редакционного качества**.

## Что подтверждено о редакционном runtime

См. [проверку фактического пути](RUNTIME_VERIFICATION.md). Не следует выдавать наличие
systemd units в Git за доказательство их установки, а наличие Actions workflow — за
доказательство успешного запуска или публикации.

Владелец указал целевую схему: выполнение в GitHub Actions, сервер хранит БД.
Доступ GitHub-hosted runner к существующему каноническому SQLite в текущем коде не найден.
Без подтверждения этого пути нельзя создавать вторую пустую БД, объявлять её production
или подменять SQLite PostgreSQL. Добавление PostgreSQL secret проблему не решает.

Удалённый в #143 `editorial-primary` вызывал staging/fake preparation, а не полноценный
публикующий `server_runtime`. Его слепое восстановление не восстанавливает рабочую
редакцию. Изменение существующего publisher/timer и перенос канонического состояния не
входят в smoke-проверку.

## Существующие точки Jev в коде

`primary_cycle.run_cycle()` после детерминированного выбора вызывает `rank_shadow()`
при включённом Jev и пишет `manifest.json -> jev_shadow`. Общий код поддерживает
PostgreSQL и SQLite; это не утверждение о фактически используемом backend/хосте.

`newsroom_status --database --city belgrade --limit 100 --shadow-duplicates` использует
существующий lexical shortlist и добавляет bounded Jev duplicate signals. В
`deploy/server/run-cycle.sh` есть opt-in вызов после успешного цикла, под тем же lock.
Наличие этого wrapper не подтверждает его исполнение и не требует переноса ключа туда.

## API и передаваемые данные

Официальный контракт: `POST https://api.typesafe.ai/v1/systemone`, Bearer auth,
`jev-latest`, `noul`-вопросы, вероятности 0..1 и `usage.input_tokens/output_tokens`.

Rank-shadow передаёт до пяти title/body excerpts материалов с official ownership;
duplicate-shadow — до восьми пар официальных материалов, отобранных сервисом.
URL и private/discovery-only материалы не должны передаваться. Публичные excerpts
ограничены 600 символами на поле; timeout — 3 секунды, без retry. Smoke использует
только публичный billing example TypeSafe, без данных редакции.

## Границы и будущий benchmark

`mode=shadow_only`, `decision_effect=none`: автоматического удаления, merge событий или
блокировки кандидатов нет. Jev не является evidence и не заменяет official-source,
freshness, moderation, high-risk review, recheck, городскую маршрутизацию или Gemini.

После подтверждения доступа к каноническому состоянию нужно измерить реальные rank и
dedup signals, latency, usage, selection agreement и ошибки на размеченных русских и
сербских материалах. Сохранение выбора уже покрыто regression-тестами общего цикла,
но качество и экономию нельзя объявлять доказанными без production measurements.
