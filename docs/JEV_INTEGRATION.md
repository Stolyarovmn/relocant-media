# TypeSafe Jev — отключаемый shadow-наблюдатель серверной редакции

Jev работает только как **неавторитетный semantic observer** реального серверного
редакционного контура. Канонические данные находятся в существующей локальной SQLite,
которую использует `relocant_media.ops.server_runtime`; отдельная PostgreSQL или копия БД
в GitHub Actions для Jev не создаётся.

**Единственный runtime-выключатель — `JEV_ENABLED`.** При значении, отличном от `true`,
код Jev не создаёт HTTP-клиент, не читает `TYPESAFE_API_KEY` и не обращается к TypeSafe.

## Production configuration

Systemd unit `deploy/server/relocant-editorial.service` загружает root-owned файл:

```text
/etc/relocant-media.env
```

Именно там, вне Git и GitHub Actions, должны находиться production-настройки Jev:

```dotenv
JEV_ENABLED=false
TYPESAFE_API_KEY=
```

Для controlled shadow benchmark владелец может изменить только:

```dotenv
JEV_ENABLED=true
TYPESAFE_API_KEY=<server secret>
```

Ключ нельзя помещать в Git, PR, `.env.example`, Docker Compose, логи или run artifacts.
GitHub Actions secret `TYPESAFE_API_KEY` серверной редакцией не используется и для этого
контура не требуется.

Официальный API-контракт интеграции: `POST https://api.typesafe.ai/v1/systemone`, Bearer
auth, модель `jev-latest`, типизированные `noul`-вопросы с вероятностями от 0 до 1.
Интеграция использует уже установленный `httpx`; отдельный SDK не требуется.

## Где Jev работает

### 1. Shadow ranking реальных кандидатов

Production timer запускает:

```text
relocant-editorial.service
  -> deploy/server/run-cycle.sh
  -> relocant_media.ops.server_runtime
  -> relocant_media.ops.server_cycle
  -> primary_cycle.run_cycle(...)
```

`run_cycle()` работает с той же существующей SQLite, из которой редакция выбирает реальные
кандидаты. После обычного SQL/детерминированного выбора он вызывает Jev только при
`JEV_ENABLED=true` и записывает результат в:

```text
manifest.json -> jev_shadow
```

Jev оценивает до пяти публичных материалов официальных источников по локальной
релевантности и практической пользе. Он не меняет `candidate_count`, порядок кандидатов,
очередь, Gemini rank/claims/verify/draft или publication policy.

### 2. Shadow semantic dedup той же SQLite

После успешного `server_runtime` и всё ещё под общим editorial worker lock wrapper запускает:

```text
python -m relocant_media.ops.newsroom_status \
  --database \
  --city belgrade \
  --limit 100 \
  --shadow-duplicates
```

Результат сохраняется рядом с артефактами реального цикла:

```text
jev-shadow-duplicates.json
```

Сначала применяется существующий lexical shortlist. Jev получает только ограниченное
число пар официальных публичных материалов и возвращает дополнительный shadow signal.
`mode=shadow_only`, `authoritative=false`, `decision_effect=none`: автоматического merge,
удаления, блокировки или изменения события нет.

Если diagnostic shadow dedup завершится ошибкой, wrapper записывает `status=unavailable`
и сохраняет исходный exit status серверной редакции. То есть отказ TypeSafe не превращает
успешный editorial cycle в failed cycle.

## Что отправляется в TypeSafe

Rank-shadow и duplicate-shadow передают только ограниченные title/body excerpts публичных
материалов официальных источников. URL, приватные submissions, контакты, Telegram tips,
Viber/WhatsApp catalog entries и другие private/discovery-only данные в Jev не передаются.

Текст ограничивается в интеграции, запросы имеют короткий timeout и не retry'ятся. В
результатах сохраняются только безопасные telemetry-поля, включая статус, модель,
`duration_ms`, token usage и semantic signals; ключ и request body не логируются.

## Границы безопасности

- Jev не является источником фактов и не заменяет official evidence.
- Deterministic dedup остаётся authoritative.
- Evidence recheck, freshness, moderation, human-review и high-risk guards не меняются.
- Belgrade/Novi Sad routing остаётся детерминированным; Jev не выбирает город.
- Telegram publication не включается и не выключается через Jev.
- При ошибке TypeSafe Jev fail-open только для самого shadow-слоя.
- До размеченного benchmark никакие Jev thresholds не могут блокировать или удалять новости.

## Benchmark

Сравнивать нужно данные **реальных server runs**, а не отдельную пустую БД в CI:

```text
candidate_count
порядок raw_item_id
jev_shadow.status
model
duration_ms
input_tokens
output_tokens
signals
jev-shadow-duplicates.json
```

Shadow-включение не должно менять candidate selection или публикационные решения. Для
semantic dedup нужен отдельно размеченный набор `SAME / RELATED_BUT_DISTINCT / DIFFERENT`
и измерения precision/recall, false-SAME и material-update false merges. До таких данных
нельзя утверждать, что Jev улучшил качество или сократил стоимость редакции.

## GitHub Actions

GitHub Actions остаётся инженерным CI/CD-контуром и не запускает production newsroom.
Workflow `.github/workflows/editorial-primary.yml`, который пытался поднять отдельный
редакционный цикл без канонической SQLite, удалён. CI по-прежнему проверяет код на PR и на
точном SHA `main`; production deployment допускает только green `main` и затем использует
серверную SQLite через `/etc/relocant-media.env`.
