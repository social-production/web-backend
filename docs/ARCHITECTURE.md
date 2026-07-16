# Backend architecture

Guide for keeping `web-backend` readable as features grow. Prefer small domain packages over monolith service files.

## Package tree (after service splits)

```
app/
  routers/                 # HTTP transport only — parse request, call service, return response
  services/
    access_control.py      # Entity visibility / membership gates
    governance_votes.py    # Shared vote math and thresholds
    projects/
      actions.py           # Join/leave, signals, activities, values
      helpers.py           # Shared project helpers
      detail/              # Detail hydration (hydrate.py, plans.py)
      phases/              # Phase/update/edit/revert governance
      software/            # PRs, merge capability, repository replacement
    events/
      actions.py
      helpers.py
      detail.py
      phases/              # Event phase/update/edit governance
    content/               # threads, posts, help_requests, roles, scopes
    messages/              # conversations, messaging, contacts, linked_chats
    feeds/                 # selects, serializers, scope, builder
    bootstrap/             # directory, onboarding, activity rail, summary
  schemas/                 # Pydantic models (prefer over large inline router models)
  models/                  # SQLAlchemy table definitions
  auth/                    # Cookies, JWT, dependencies
  middleware/              # CSRF, rate limits, security headers
  utils/                   # Shared pure helpers (votes, etc.)
```

Thin barrels such as `app/services/projects_phases.py` and `app/services/projects_software.py` re-export public APIs so existing imports keep working while logic lives in packages.

## Layering rules

1. **Routers = transport only.** No SQL, no vote math, no membership loops in routers.
2. **Services own business rules.** Routers call services; services call models, `access_control`, and shared utils.
3. **No cross-package `_private` imports.** If another package needs a helper, promote it to a public name (or put it in a shared module). Underscore aliases may remain in barrels for compatibility.
4. **Governance votes** go through `app/services/governance_votes.py` (and `app.utils.votes` for population counts).
5. **Access checks** go through `app/services/access_control.py` — fail closed for private entities.
6. **File-size guideline:** aim for ~400 lines per module. When a file grows past that and mixes concerns (gates vs serializers vs request handlers), split by concern like `phases/` and `software/`.

## Adding a feature (recipe)

1. **Service module** — add or extend `app/services/<domain>/…` with clear public functions.
2. **Schema** — add request/response models in `app/schemas/` when the router would otherwise accumulate inline Pydantic classes.
3. **Router** — thin route in `app/routers/` that validates auth, calls the service, returns the schema.
4. **Test** — cover happy path + permission failure in `tests/` (use `tests/conftest.py` fixtures). Prefer in-process e2e scripts already wired through `test_e2e_scripts_pytest.py` when the flow spans multiple endpoints.
5. **Contract** — if the frontend adapter needs the shape, update `web/docs/WEB_BACKEND_CONTRACT.md`.

## Testing and CI

- Local: `python -m pytest tests/ -q`
- Lint: `ruff check app tests` and `ruff format --check app tests` (also in GitHub Actions)
- See [`CI_TESTING.md`](CI_TESTING.md) and [`SECURITY.md`](SECURITY.md)

## Anti-patterns

- Copying a full import header into every extracted file and leaving unused imports (run ruff).
- `importlib` + `globals().update(...)` to share helpers — use explicit imports.
- Growing a second monolith next to a package (put new code in the package).
- Importing `app.services.projects` eagerly from modules that `projects` also imports — use lazy package `__init__` or import submodule paths carefully.
