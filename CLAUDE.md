# web-backend (FastAPI, SQLAlchemy Core, Alembic)

## Layout
- Routers `app/routers/*.py` (request models live here; a field missing from a response model is silently dropped), services `app/services/**`, tables `app/models/*.py`.
- Votes/quorum helpers: `app/utils/votes.py` (`ensure_established_voter`, vote population). Trust graph: `app/services/trust.py`. Each decision type keeps its own `APPROVAL_THRESHOLD = 0.66` tally (plans, phases, links, edits, software, moderation).
- Anti-bot: `app/services/standing.py` (new → warm → full → paused; weights and quorum population) and `app/services/bot_reviews.py` (3 marks open a public review; full members decide). `trust.py`'s ratio graph is display-only now.
- Calls signalling: `app/services/messages/calls.py` (Redis pub/sub). Messages: `app/services/messages/*`.
- Settings: `app/config.py` (`GOVERNANCE_*` env vars on Railway).
- Privacy: anything listed to a viewer must hide items tagged only to private (closed) communities they aren't in. Single-entity selects use `closed_community_only_tag_condition`; combined feeds use `hide_private_community_items` (both in `app/services/access_control.py`). Tag chips come from `feeds/serializers.py::_fetch_tags_for_items(viewer_id=…)`.
- Notifications: a new `kind` must be added to `KIND_TO_CATEGORY` in `app/services/notification_preferences.py` or `create_notification` drops it.
- Auth: refresh tokens rotate; reuse is allowed within 30 s, or while the replacement token is unused (`_rotation_allows_reuse` in `app/services/auth.py`).

## Rules
- Alembic revision IDs must be ≤ 32 chars (`alembic_version` is varchar(32)); Railway runs `alembic upgrade head` on start, so a bad migration crash-loops production.
- Commit messages: one plain sentence describing the user-visible effect.

## Checks
- `ruff check app tests && ruff format --check app tests`
- `venv/bin/python -m pytest -q` (uses the local Docker Postgres; fixtures `client`, `isolated_client`, `db_transaction`, `seed_user` in `tests/conftest.py`).
