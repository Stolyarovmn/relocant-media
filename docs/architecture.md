# Architecture v2

## Telegram topology

One city consists of:

1. Public news channel.
2. Linked discussion supergroup for comments.
3. Separate forum supergroup for topic-based community discussion.
4. One admin bot for publishing, moderation, intake and advertiser workflows.

The discussion group and forum group are intentionally separate.

## Control plane

GitHub stores code, prompts, source registry, city policy and experiment configuration. Changes should flow through PRs.

For `Stolyarovmn/relocant-media` the owner granted standing permission on 21.10.2026 for
agents working under the project contract to create and merge project PRs without asking again,
but only after required checks pass, the current head and mergeability are verified, and `main`
CI is checked after merge. This delegation does not override any other safety boundary.

## AI roles

- Gemini API: routine production inference and web-grounded research.
- Claude Code: implementation, parser repair, migrations, tests and refactors.
- Optional Claude API: second-opinion verification for selected high-risk stories.
- ChatGPT: daily management/analysis, experiment evaluation and architecture decisions.
- Gemini Notebook: curated human-facing research/knowledge base, not a runtime dependency.

An LLM verdict is never sufficient by itself to make a story publishable. Grounded citations,
source policy, exact evidence snapshots, event deduplication and delivery state are checked by
normal application code.

## Runtime

Редакционный путь без MTProto: collector → `raw_item` → canonical URL/content hash →
event-level dedup/evidence bundle → `EditorialService.prepare` → Gemini structured
rank/claims/verify/draft → deterministic source-policy and audience guards →
`editorial_run` + `post` → `EditorialService.deliver` / channel publisher → Bot API.

`READY` допускается к автоматической доставке только когда в evidence bundle присутствует
официальный источник, anchor-source разрешает `auto_publish`, использованные источники не
помечены `high_risk`, а текущая политика аудитории считает материал достаточно полезным.
Снимок всего evidence bundle повторно сверяется с БД непосредственно перед отправкой.
Каждый `editorial_run` и `post` хранит `editorial_rule_version`, поэтому аналитика может
сравнивать наблюдаемые метрики разных наборов правил, не выдавая корреляцию за причинность.

Рабочий серверный контур использует SQLite и запускает редакционный цикл systemd раз в три
часа. Отдельный пятиминутный intake-процесс собирает закрытую предложку и использует ту же
редакционную очередь и общую блокировку. Несколько одновременно пишущих копий SQLite не
поддерживаются.

CLI и инженерные тесты по умолчанию используют preview/fake transport. Реальная отправка
осуществляется только явным операционным контуром и не должна быть побочным эффектом CI или
engineering task. MTProto используется отдельной read-only аналитикой и может оставаться
выключенным. Подробности: [редакционный цикл](EDITORIAL_CYCLE.md) и
[серверная редакция](SERVER_EDITORIAL.md).

FastAPI handles Telegram webhooks and realtime flows. Batch collection/analysis can also be
triggered by GitHub Actions where this does not create a second canonical editorial state.

## Ads isolation

Newsroom and ad-budget execution are separate trust domains. Editorial code may produce content
metrics and recommendations, but it does not receive an ad-spend capability. Any future Telegram
Ads executor must live behind its own credentials and budget guard, start read-only where possible,
and require `OWNER_APPROVAL` for spend/bid/budget changes until an explicit owner policy defines a
bounded autonomous action. A compromised or hallucinating editorial component therefore cannot
spend advertising money.

## Safety boundary

No AI or engineering agent may autonomously:
- increase ad budget outside an owner-defined cap or bypass `OWNER_APPROVAL`;
- publish high-risk legal/medical/financial content;
- auto-publish an event without an official primary source;
- change production secrets;
- merge a PR before required checks pass or without the standing/project-specific owner delegation described above;
- weaken source provenance, factual verification, delivery idempotency or human-review category boundaries.
