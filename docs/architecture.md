# Architecture v1

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

## Runtime

FastAPI handles Telegram webhooks and realtime flows. PostgreSQL stores operational state. Batch collection/analysis can be triggered by GitHub Actions.

## Safety boundary

No AI may autonomously:
- increase ad budget outside an owner-defined cap;
- publish high-risk legal/medical/financial content in v1;
- change production secrets;
- merge its own engineering changes.
