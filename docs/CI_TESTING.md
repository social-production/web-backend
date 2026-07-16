# CI and test maintenance

## Pipeline

GitHub Actions ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) runs on every push/PR to `main`:

1. Postgres 16 + Redis 7 service containers
2. `pip install -e .`
3. `alembic upgrade head`
4. `ruff check` / `ruff format --check`
5. `pip audit` (dependency scan)
6. `python -m pytest tests/ -v`

Four E2E scripts require `TEST_BASE_URL` (external server) and are skipped in CI.

## March 2026 CI fix retrospective

After tracking `__init__.py` files ignored by the `_ *` gitignore rule, CI moved from import errors to **test drift** failures.

### Category A — API shape drift in E2E scripts

Tests asserted old response keys. The API had moved on; tests had not.

| Test | Issue | Resolution |
|------|-------|------------|
| `test_comments_notifications` | `get_thread_by_slug()` nests `discussion` under `thread` | Assert `thread_payload["thread"]["discussion"]` |
| `test_personal_service_requests` | Conversations list uses `items`, not `conversations` | Use `items` for list and messages |
| `test_phase_naming` | Home feed filters by scope membership | Seed `scope_memberships` for creator |
| `test_actions_search_notifications` | Event phases use `event-plan`, not `phase-2` | Send valid event phase id |

### Category B — Timing flake

`scheduled_at: datetime.now()` fails `ensure_future_scheduled_start()` when request handling crosses the clock boundary. Use `now + timedelta(hours=1)` in activity POST bodies.

### Category C — Production bugs found by deeper test runs

Activity commit routes referenced `project_activity_roles` / `event_activity_roles` without importing them from `app.models`.

### Category D — Environment sensitivity

`test_feed_update_fields` needed `last_activity_at=now` so seeded projects appear in the public feed on databases with many existing rows.

## Preventing recurrence

- Prefer [`tests/conftest.py`](../tests/conftest.py) helpers (`future_scheduled_at`, `seed_user`, `seed_channel_with_membership`, `seed_scope_membership`, `seed_project`, `seed_event`, `register_and_login_client`) over copy-pasted seeds.
- Keep E2E script assertions aligned with [`WEB_BACKEND_CONTRACT.md`](https://github.com/social-production/web/blob/main/docs/WEB_BACKEND_CONTRACT.md).
- Run `python -m pytest tests/ -q` locally before pushing (Postgres + Redis required).
