# Claude Code project contract

## Mission
Build and maintain a reliable automation platform for Russian-language local Telegram media.

## Runtime architecture (owner instruction, 27 October 2026)

All editorial computation runs on GitHub-hosted Actions runners: collection,
discovery, model calls, translation/enrichment, reviews, Telegram intake and delivery,
reporting and public projection. The small server is a state-storage endpoint only;
Google Drive is another supported storage backend. Do not add or enable editorial,
intake or deployment compute timers, a self-hosted Actions runner, model processing,
or a resident newsroom service on the storage server. Existing server compute is a
legacy writer to fence during a verified cutover, not the target architecture.
Keep a single canonical state, preserve Telegram receipts/offsets and require a
verified final snapshot plus read-only Actions preflight before enabling the new
writer. Do not stop a working legacy writer before replacement storage/authentication
is ready; never run it alongside the Actions writer.

## Hard boundaries

Never:
- publish directly to a production Telegram channel from an engineering task;
- change production secrets or commit credentials;
- alter advertising budgets;
- merge a PR before its required checks pass;
- weaken source provenance or factual-verification requirements;
- silently turn a human-review category into an auto-publish category;
- use the personal MTProto analytics session for sending/editing/deleting messages, joining chats, invitations, DMs, moderation or advertising actions;
- expose, copy, log or persist the MTProto session file outside its dedicated secret volume.

## Постоянное разрешение владельца на PR

21.10.2026 владелец прямо разрешил слияние PR55 и последующих PR этого проекта:
«Разрешаю и больше не спрашивай разрешения на ПРы в этом проекте».
Это заменяет прежний запрет `merge your own PR` для Stolyarovmn/relocant-media.
Создавай и сливай PR без повторного запроса, после успешных обязательных проверок
и проверки актуального head и возможности слияния. После слияния проверяй CI main.
Остальные ограничения проекта этим разрешением не отменены.

## Stack

- Python 3.12+
- FastAPI
- PostgreSQL
- SQLAlchemy / Alembic
- Pydantic
- httpx
- Telethon for isolated read-only MTProto analytics
- pytest / ruff / mypy
- Docker Compose for local/runtime deployment

## Engineering rules

Every change must:
1. preserve source provenance;
2. include tests for new behavior;
3. run `ruff check .`, `mypy src`, and `pytest`;
4. keep Telegram/API side effects behind explicit service interfaces;
5. use idempotency for webhook and scheduled processing;
6. avoid undocumented Telegram APIs for production-critical behavior;
7. keep Bot API credentials and the personal MTProto session isolated from each other.

Collector changes additionally require:
- a fixture or deterministic sample;
- a regression test;
- graceful failure when upstream HTML/API changes.

Database schema changes require:
- an Alembic migration;
- upgrade and downgrade paths;
- migration tests where practical.

MTProto analytics changes additionally require:
- no send-capable helper surface in analytics adapters;
- network-free/fake-client tests in CI;
- no use of `messages.getMessagesViews` for measurement because it may increment views;
- explicit handling of missing metrics without synthesizing zeros;
- documented owner steps for session authorization/rotation.

## Agent roles

Use narrow subagents when helpful:
- collector-engineer: source adapters and parser repairs;
- backend-engineer: API/runtime/business logic;
- database-engineer: schema, migrations and query performance;
- test-engineer: regression/integration tests;
- security-reviewer: secrets, webhook validation and permission boundaries.

## Definition of done

A task is not done until tests pass and operational impact is documented in the PR.
