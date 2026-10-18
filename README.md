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
Оставшиеся этапы: [план развития](docs/roadmap.md).
Реальная отправка в staging требует явного включения; production Telegram
в инженерных задачах не используется.
