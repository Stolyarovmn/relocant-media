# Claude Code project contract

## Mission
Build and maintain a reliable automation platform for Russian-language local Telegram media.

## Hard boundaries

Never:
- publish directly to a production Telegram channel from an engineering task;
- change production secrets or commit credentials;
- alter advertising budgets;
- merge your own PR;
- weaken source provenance or factual-verification requirements;
- silently turn a human-review category into an auto-publish category;
- use the personal MTProto analytics session for sending/editing/deleting messages, joining chats, invitations, DMs, moderation or advertising actions;
- expose, copy, log or persist the MTProto session file outside its dedicated secret volume.

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
