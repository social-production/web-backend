# Social Production Web Backend

Phase 1 backend — FastAPI + PostgreSQL 16 + Redis, Alembic migrations, Docker Compose.

## Recommended Beginner Setup

Use Docker unless you specifically want to manage Python, PostgreSQL, and Redis yourself.

Prerequisites:

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- Git, if you are cloning the project for the first time

From the `web-backend` folder:

```bash
docker compose up -d --build
```

Migrations and seed data run automatically on first start.

Check that it worked:

- API docs: `http://localhost:8000/docs`
- Lightweight health check: `http://localhost:8000/healthz`
- Readiness check: `http://localhost:8000/readyz`

Then start the frontend from the `web` folder:

```bash
npm install
npm run dev
```

Open `http://localhost:5173`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | Set to `production` in deployed environments to enable startup safety checks |
| `DATABASE_URL` | set in compose | PostgreSQL connection string |
| `REDIS_URL` | set in compose | Redis connection string |
| `JWT_SECRET` | local dev value | Secret for JWT tokens. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `JWT_EXPIRE_MINUTES` | `60` | Token lifetime in minutes |
| `MESSAGE_ENCRYPTION_KEY` | local dev value | Fernet key for message encryption. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed origins, e.g. `https://socialproduction.example` |

With Docker, local development values are set in `docker-compose.yml`. Add any overrides to a `.env` file in this folder — Docker Compose picks them up automatically.

Set `APP_ENV=production`, real secrets, and explicit CORS origins before exposing the service externally. Production startup fails fast if placeholder secrets or wildcard CORS are used.

## Manual Setup (No Docker)

Use this only if you do not want Docker.

Prerequisites:

- Python 3.11+
- PostgreSQL 16
- Redis 7
- A local database named `social_production`

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .

# Set environment variables (or add to a .env file)
export APP_ENV=development
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/social_production
export REDIS_URL=redis://localhost:6379/0
export JWT_SECRET=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
export MESSAGE_ENCRYPTION_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
export CORS_ORIGINS=http://localhost:5173

# Run migrations and seed
alembic upgrade head
python scripts/seed.py

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Running Tests

```bash
# With Docker:
docker compose exec backend python -m pytest tests/ -v

# Without Docker (venv active):
python -m pytest tests/ -v
```

## Troubleshooting

### Port already in use

- **5432** (PostgreSQL): stop any local postgres or change the host port in `docker-compose.yml`
- **6379** (Redis): stop any local redis or change the port mapping
- **8000** (backend): find the process with `lsof -i :8000` and stop it

### Backend container exits immediately

```bash
docker compose logs backend
```

Common causes: `DATABASE_URL` unreachable because postgres is still starting (wait and retry), or invalid secret values.

### Frontend cannot reach the backend

Make sure `CORS_ORIGINS` includes `http://localhost:5173` and that the backend is reachable at `http://localhost:8000/docs`.

### Start fresh (wipes all data)

```bash
docker compose down -v
docker compose up -d --build
```

Migrations and seed run automatically on startup.
