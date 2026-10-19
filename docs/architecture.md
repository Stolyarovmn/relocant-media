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
rank/claims/verify/draft → deterministic source-policy guards → `editorial_run` + `post` →
`EditorialService.deliver` → staging publisher → Bot API.

`READY` допускается к автоматической доставке только когда в evidence bundle присутствует
официальный источник, anchor-source разрешает `auto_publish`, а использованные источники не
помечены `high_risk`. Снимок всего evidence bundle повторно сверяется с БД непосредственно
перед отправкой. Каждый `editorial_run` и `post` хранит `editorial_rule_version`, поэтому
аналитика может сравнивать наблюдаемые метрики разных наборов правил, не выдавая корреляцию
за причинность.

CLI `relocant-editorial` по умолчанию формирует preview, а fake transport заменяет внешний
HTTP-вызов. Доставка в staging не заполняет production-поля публикации и не запускает сбор
метрик опубликованного поста. MTProto используется отдельной аналитикой и может оставаться
выключенным. Подробности: [редакционный цикл](EDITORIAL_CYCLE.md).

FastAPI handles Telegram webhooks and realtime flows. PostgreSQL stores operational state. Batch collection/analysis can be triggered by GitHub Actions.

## Ads isolation

Newsroom and ad-budget execution are separate trust domains. Editorial code may produce content
metrics and recommendations, but it does not receive an ad-spend capability. Any future Telegram
Ads executor must live behind its own credentials and budget guard, start read-only where possible,
and require `OWNER_APPROVAL` for spend/bid/budget changes until an explicit owner policy defines a
bounded autonomous action. A compromised or hallucinating editorial component therefore cannot
spend advertising money.

## Safety boundary

No AI may autonomously:
- increase ad budget outside an owner-defined cap or bypass `OWNER_APPROVAL`;
- publish high-risk legal/medical/financial content;
- auto-publish an event without an official primary source;
- change production secrets;
- merge its own engineering changes.
