# Security posture and hardening backlog

## Current model (test deployment)

- **API gateway only**: clients never access Postgres/Redis directly
- **JWT bearer auth** on personal data, messages, notifications, settings
- **Central access control** in `app/services/access_control.py`
- **Messages encrypted at rest** (Fernet)
- **IP rate limiting** via Redis
- **Production secret validation** at startup

## Fixed for test deploy

- Closed community member list requires scope visibility (`list_scope_members` + `assert_can_view_scope`)

## Deferred hardening (post-test)

| Priority | Item | Notes |
|----------|------|-------|
| High | httpOnly cookie auth | Replace `localStorage` JWT; add CSRF strategy |
| High | Security headers | HSTS, CSP, X-Frame-Options, X-Content-Type-Options |
| Medium | Fail-closed Redis | Token revocation + rate limits when Redis unavailable |
| Medium | Shorter JWT + refresh tokens | Default 7-day access tokens |
| Medium | Per-user rate limits | Complement IP limits on search/feeds/bootstrap |
| Low | Dependency scanning | Dependabot / audit in CI |
| Low | Custom domain + WAF | Production edge |

## Test-deploy limitations

- Frontend route guards are client-only (`ssr = false`) — API must remain authoritative
- OpenAPI disabled in production by default
- Trusted testers only until hardening phase completes

Track implementation in GitHub issues as you prioritize each item.
