# Backend provider seams

How to keep FastAPI/Postgres/Redis replaceable without rewriting product logic.

## Package layout

```
app/
  ports/                 # Protocols: AuthProvider, CacheStore, SearchProvider, AccessPolicy, …
  adapters/
    postgres/            # Current relational implementations
    redis/               # Cache + token revocation
  domain/                # Pure policy (no Session / Redis / FastAPI)
  errors.py              # Domain errors (not HTTPException)
  unit_of_work.py        # Explicit commit/rollback helpers
  http_errors.py         # Maps AppError → HTTP
  services/              # Application services (migrate toward ports over time)
  routers/               # Transport only
```

## Rules

1. New infra coupling (Redis keys, FTS SQL, JWT cookies) goes behind a port + adapter.
2. Services raise `app.errors.AppError` subclasses; routers/handlers map to HTTP.
3. Prefer `app.unit_of_work.commit/rollback` over scattered `db.commit()` when touching migrated code.
4. Pure decision rules live in `app.domain` or modules like `services/moderation/thresholds.py`.
5. Dependency wiring for ports lives in `app.dependencies`.

## First ports (implemented)

| Port | Current adapter | Swap candidate |
|------|-----------------|----------------|
| `CacheStore` | `adapters/redis/cache_store.py` | Upstash / memory |
| `TokenRevocationStore` | `adapters/redis/token_revocation.py` | Supabase session revoke / KV |
| `SearchProvider` | `adapters/postgres/search.py` | Supabase RPC / Meilisearch |
| `AccessPolicy` | `adapters/postgres/access_policy.py` | RLS-backed checks |
| `AuthProvider` | `adapters/postgres/auth.py` (`JwtAuthProvider`) | Supabase Auth |
| `NotificationsProvider` | `adapters/postgres/notifications.py` | Alternate notification store |
| `MessagingProvider` | `adapters/postgres/messaging.py` | Alternate messaging backend |
| `FeedProvider` | `adapters/postgres/feeds.py` | Alternate feed engine / RPC |
| `PeopleSuggestionsProvider` | `adapters/postgres/people_suggestions.py` | alternate ranking store |

## Provider strategy

### Supabase (medium-term)

Realistic first replacement path:

- Keep Postgres schema; move auth to Supabase Auth behind `AuthProvider`.
- Keep search on Postgres FTS initially via `SearchProvider`.
- Replace Redis uses gradually via `CacheStore` / `TokenRevocationStore`.
- Optionally encode visibility with RLS while keeping `AccessPolicy` as the app-facing API.

### Holochain (long-horizon)

Not a drop-in provider. Feeds, moderation electorates, messaging encryption, and multi-table governance assume centralized queryability. Portable pieces today: domain thresholds, signal ratios, frontend contracts. Treat Holochain as a redesign track after ports mature.

## Frontend switch

Build with `VITE_BACKEND=fastapi|supabase|holochain`. Registry: `web/src/lib/api/drivers/registry.ts`.

Unimplemented providers fail at driver creation with an actionable message pointing at the expected backend workspace.

Plug-in checklist: `web/docs/PROVIDER_IMPLEMENTATION_CHECKLIST.md`.
