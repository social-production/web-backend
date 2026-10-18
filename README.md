# Social Production: web-backend

The API behind the Social Production website: FastAPI, PostgreSQL 16 and Redis. The site lives in [web](https://github.com/social-production/web).

- API: https://web-backend-production-dd75.up.railway.app (health: `/readyz`)
- Live site: https://web-production-885ec.up.railway.app

## Run it locally

You need Docker.

```bash
cp .env.example .env
docker compose up -d --build
```

The API runs at http://localhost:8000 (interactive docs at `/docs`) and reloads when you edit code. Migrations and seed data run on start.

Then start the site from the `web` folder with `npm run dev`.

Start over with an empty database: `docker compose down -v && docker compose up -d --build`.

## Settings

`.env.example` lists every setting with a comment. The ones that matter most:

| Setting | What it does |
|---|---|
| `JWT_SECRET`, `MESSAGE_ENCRYPTION_KEY` | Must be real secrets in production; the app refuses to start with placeholders. |
| `CORS_ORIGINS` | Which sites may call the API. |
| `GOVERNANCE_*` | Warm-up rules for new accounts and voting thresholds. |
| `GITHUB_TOKEN`, `GITHUB_REPO` | Turns site feedback into GitHub issues. |

## Checks

```bash
ruff check app tests && ruff format --check app tests
python -m pytest -q   # uses the Docker Postgres
```

## How the code is laid out

Routes are in `app/routers/`, logic in `app/services/`, tables in `app/models/`, migrations in `alembic/versions/`. Migration IDs must be 32 characters or fewer, because production runs `alembic upgrade head` on start and a bad ID stops it booting. More in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Deploy

Pushing to `main` redeploys on Railway and runs migrations. Push this repo before `web` when the site needs new fields, and wait for `/readyz` to return 200. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/SECURITY.md](docs/SECURITY.md).
