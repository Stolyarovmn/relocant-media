# Relocant Media

Automation platform for a Russian-language local Telegram media network for relocants.

## v1 goals

- Collect local official/news/event sources.
- Rank and cluster stories.
- Draft Russian Telegram posts with source provenance.
- Moderate comments and accept reader tips/corrections.
- Accept advertiser requests through Telegram workflows.
- Store post snapshots and produce daily analytics.
- Keep editorial/config changes reviewable in Git.

## Architecture

- **Telegram**: channel, linked discussion group, separate forum community, direct messages, suggested posts.
- **GitHub**: source/config/prompt versioning, CI/CD, issues/PRs, scheduled batch jobs.
- **Gemini API**: routine discovery, extraction, ranking, translation and drafting.
- **Claude Code**: engineering tasks, collector repairs, refactors and PR implementation.
- **ChatGPT**: management, daily analysis, experiment evaluation and architecture decisions.
- **PostgreSQL**: operational state, submissions, comments, ads and analytics.
- **FastAPI runtime**: Telegram webhooks and realtime workflows.

See [`docs/architecture.md`](docs/architecture.md).
Hosted Actions runtime with Drive state and Pages projection:
[`docs/ACTIONS_RUNTIME.md`](docs/ACTIONS_RUNTIME.md).
Optional TypeSafe Jev shadow scoring is controlled entirely by `JEV_ENABLED`:
see [`docs/JEV_INTEGRATION.md`](docs/JEV_INTEGRATION.md).

## Local start

```bash
cp .env.example .env
docker compose up --build
```

Health check:

```bash
curl http://localhost:8080/health
```

## Tests

```bash
python -m pytest
```

## Status

Реализован редакционный цикл без MTProto: источник → PostgreSQL → Gemini
structured rank/claims/verify/draft → сохранённый черновик → staging publisher.
По умолчанию используется dry-run; fake transport проверяет Bot API без Telegram.
Интеграционные тесты заменяют Gemini и Telegram и используют отдельный PostgreSQL.

Запуск и восстановление: [редакционный цикл](docs/EDITORIAL_CYCLE.md).
Альтернатива без AI API и текущее рабочее состояние:
[редакция по подписке](docs/OPERATIONAL_STATE.md).
[Автоматический сбор и выбор кандидатов без AI API](docs/SUBSCRIPTION_PIPELINE.md).
Оставшиеся этапы: [план развития](docs/roadmap.md).
Реальная отправка в staging требует явного включения; production Telegram
в инженерных задачах не используется.
