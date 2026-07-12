# Railway deployment (backend)

See the frontend repo's canonical guide: [web/docs/DEPLOYMENT.md](https://github.com/social-production/web/blob/main/docs/DEPLOYMENT.md).

## Backend-specific notes

- Uses root [`Dockerfile`](../Dockerfile) and [`railway.toml`](../railway.toml)
- Health check: `GET /readyz` (DB + Redis)
- `DATABASE_URL` from Railway Postgres is auto-normalized to `postgresql+psycopg://` in [`app/config.py`](../app/config.py)
- Required production env: `APP_ENV`, `JWT_SECRET`, `MESSAGE_ENCRYPTION_KEY`, `CORS_ORIGINS`, `DATABASE_URL`, `REDIS_URL`

## Local production-like test

```bash
cp .env.production.example .env.production
docker compose -f docker-compose.prod.yml up --build
```
