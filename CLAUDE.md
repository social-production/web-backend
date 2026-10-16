# web-backend (FastAPI, SQLAlchemy Core, Alembic)

## Layout
- Routers `app/routers/*.py` (request models live here; a field missing from a response model is silently dropped), services `app/services/**`, tables `app/models/*.py`.
- Votes/quorum helpers: `app/utils/votes.py` (`ensure_established_voter`, vote population). Trust graph: `app/services/trust.py`. Each decision type keeps its own `APPROVAL_THRESHOLD = 0.66` tally (plans, phases, links, edits, software, moderation).
- Calls signalling: `app/services/messages/calls.py` (Redis pub/sub). Messages: `app/services/messages/*`.
- Settings: `app/config.py` (`GOVERNANCE_*` env vars on Railway).

## Rules
- Alembic revision IDs must be ≤ 32 chars (`alembic_version` is varchar(32)); Railway runs `alembic upgrade head` on start, so a bad migration crash-loops production.
- Commit messages: one plain sentence describing the user-visible effect.

## Checks
- `ruff check app tests && ruff format --check app tests`
- `venv/bin/python -m pytest -q` (uses the local Docker Postgres; fixtures `client`, `isolated_client`, `db_transaction`, `seed_user` in `tests/conftest.py`).
