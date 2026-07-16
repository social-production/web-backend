# Backend architecture guide

## Layout

```
app/
  routers/          # HTTP transport only — no SQL
  services/         # Business logic grouped by domain
    feeds/          # selects.py, serializers.py, scope.py, builder.py
    bootstrap/      # directory.py, activity_rail.py, summary.py, …
    projects/       # detail.py, actions.py, helpers.py
    governance_votes.py  # Shared vote math
  schemas/          # Pydantic models (prefer over inline router models)
  auth/             # Cookies, JWT, dependencies
  middleware/       # CSRF, rate limits, security headers
```

## Rules

1. Routers call services; services call models and `access_control`.
2. Do not import `_private` helpers across service packages — promote to public module APIs.
3. Governance vote summaries go through `app/services/governance_votes.py`.
4. Add pytest coverage for new endpoints; use `tests/conftest.py` fixtures.

## Adding an endpoint

1. Add service function under the correct `app/services/<domain>/` package.
2. Add Pydantic schemas in `app/schemas/` when the router grows.
3. Add thin route in `app/routers/`.
4. Document shape in `web/docs/WEB_BACKEND_CONTRACT.md` if the frontend adapter needs it.

See also [`CI_TESTING.md`](CI_TESTING.md) and [`SECURITY.md`](SECURITY.md).
