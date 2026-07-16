# Security posture and hardening backlog

## Current model

- **API gateway only**: clients never access Postgres/Redis directly
- **httpOnly cookie sessions** with CSRF double-submit (`sp_access`, `sp_refresh`, `sp_csrf`)
- **Bearer tokens** still accepted for tests and API clients
- **Central access control** in `app/services/access_control.py`
- **Messages encrypted at rest** (Fernet)
- **IP + per-user rate limiting** via Redis (fail-closed in production)
- **Security headers** on API and SvelteKit responses
- **Production secret validation** at startup
- **Shorter access tokens** (15 min) with refresh rotation (7 days)

## Completed hardening (engineering phase 2)

| Item | Status |
|------|--------|
| httpOnly cookie auth | Done |
| CSRF strategy (SameSite=Lax + `X-CSRF-Token`) | Done |
| Security headers | Done (API middleware + `hooks.server.ts`) |
| Fail-closed Redis for rate limits | Done in production |
| Shorter JWT + refresh tokens | Done |
| Per-user rate limits on search/feeds/bootstrap | Done |
| Dependency scanning in CI | Done (`pip audit`, `npm audit`) |
| Ruff in CI | Done |

## Deferred

| Priority | Item | Notes |
|----------|------|-------|
| Low | Custom domain + WAF | Production edge |
| Low | Dependabot config file | CI audit covers PRs; add Dependabot for auto-PRs |
| Research | P2P auth model | Phase 3 product roadmap |

## Test-deploy notes

- Frontend route guards are client-only (`ssr = false`) — API remains authoritative
- OpenAPI disabled in production by default
- See also [`CI_TESTING.md`](CI_TESTING.md) for test maintenance guidance

## Frontend

Cookie auth driver: [`web/src/lib/api/drivers/fastapi/client.ts`](https://github.com/social-production/web/blob/main/src/lib/api/drivers/fastapi/client.ts)

Adapter guide: [`web/docs/ADAPTERS.md`](https://github.com/social-production/web/blob/main/docs/ADAPTERS.md)
