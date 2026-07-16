# Contributing to Social Production (web-backend)

Thank you for helping improve the API backend.

## Prerequisites

- Python 3.12+
- Docker (recommended) or local PostgreSQL 16 + Redis 7

## Setup

```bash
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000` — docs at `/docs` in development.

## Architecture

- `app/routers/` — HTTP routes (thin)
- `app/services/` — business logic and authorization
- `app/models/` — SQLAlchemy table definitions
- `app/services/access_control.py` — visibility rules for private communities, events, feeds

Authorization must be enforced in the service layer. Never rely on the frontend to hide data.

## Tests

See [`docs/CI_TESTING.md`](docs/CI_TESTING.md) for CI maintenance and E2E test guidance.

```bash
docker compose exec backend python -m pytest tests/ -v
```

Or locally with Postgres/Redis running:

```bash
pip install -e .
alembic upgrade head
python -m pytest tests/ -v
```

## Lint/format

```bash
pip install ruff
ruff check app tests
ruff format --check app tests
```

## Branching

1. Branch from `main`
2. Add or update tests for behavior changes
3. Run pytest before opening a PR
4. Open a PR against `main`

## Frontend contract

The SvelteKit app expects specific API shapes. See the frontend repo's [`web/docs/WEB_BACKEND_CONTRACT.md`](https://github.com/social-production/web/blob/main/docs/WEB_BACKEND_CONTRACT.md).

Contract smoke test: `tests/test_contract_smoke.py`.

## Deployment

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for Railway setup.
