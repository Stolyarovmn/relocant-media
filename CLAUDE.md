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
- silently turn a human-review category into an auto-publish category.

## Stack

- Python 3.12+
- FastAPI
- PostgreSQL
- SQLAlchemy / Alembic
- Pydantic
- httpx
- pytest / ruff / mypy
- Docker Compose for local/runtime deployment

## Engineering rules

Every change must:
1. preserve source provenance;
2. include tests for new behavior;
3. run `ruff check .`, `mypy src`, and `pytest`;
4. keep Telegram/API side effects behind explicit service interfaces;
5. use idempotency for webhook and scheduled processing;
6. avoid undocumented Telegram APIs for production-critical behavior.

Collector changes additionally require:
- a fixture or deterministic sample;
- a regression test;
- graceful failure when upstream HTML/API changes.

Database schema changes require:
- an Alembic migration;
- upgrade and downgrade paths;
- migration tests where practical.

## Agent roles

Use narrow subagents when helpful:
- collector-engineer: source adapters and parser repairs;
- backend-engineer: API/runtime/business logic;
- database-engineer: schema, migrations and query performance;
- test-engineer: regression/integration tests;
- security-reviewer: secrets, webhook validation and permission boundaries.

## Definition of done

A task is not done until tests pass and operational impact is documented in the PR.
