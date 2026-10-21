# TikHub Admin Web Implementation Plan

> **For agentic workers:** Implement this plan task-by-task using the user's authorized multi-agent workflow. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add an admin/password session-protected employee key management site on host port 8081 to the existing remote gateway and GitHub repository.

**Architecture:** FastAPI/Jinja2 calls the existing gateway CLI through allowlisted argv commands. CLI merges APISIX consumers and the private employee inventory and queries ClickHouse using a new SELECT-only user. One web worker holds expiring sessions and one-time key results; Compose provides isolated credentials and a dedicated private inventory directory.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, Uvicorn, stdlib unittest, Docker Compose.

**Spec:** docs/superpowers/specs/2026-10-21-tikhub-admin-web-design.md

## Global Constraints

- Host 8081 maps container 0.0.0.0:8080. No IP allowlist or change to Limesurvey 8080.
- Fixed username admin; ADMIN_PASSWORD from private env file. No nonempty password means startup failure.
- Anonymous management requests redirect 303 to /login. POST mutations require CSRF and same origin. No TikHub master credential in web container.
- New keys revealed once, never in URLs/logs; existing CLI user-key retains TTY restriction.
- Today's usage uses Asia/Shanghai calendar day and uniqExact(request_id), business paths only. Missing data sources show errors.
- All user mutations through gatewayctl.py. Existing employee keys retained during migration.
- Publish code to existing GitHub jialingfan110-eng/apisix; deploy /opt/tikhub-gateway on root@118.196.147.13.
- No paid TikHub requests in acceptance, no unrelated service changes.

## Review Focus

- Expired, forged, or logged-out sessions must not perform mutations or expose keys (Task 2).
- Failed API writes must not create a second account on duplicate request; existing disabled consumers remain visible (Task 1).
- Invalid names and forwarded headers must not cause shell execution, SQL interpolation, or origin bypass (Tasks 1 and 2).
- Partial state migration must preserve originals and reject ambiguous inventories (Task 3).
- Existing Compose services retain credentials/config; web receives only minimal credentials and localhost-only Admin publication is preserved (Task 3).

---

### Task 1: CLI interfaces and aggregate queries

**Files:** Modify deploy/tikhub/gatewayctl.py; Create deploy/tikhub/web_ops.py and deploy/tikhub/test_web_ops.py.

**Interfaces:**
- Admin(key, base_url='http://127.0.0.1:9180/apisix/admin'). main reads APISIX_ADMIN_URL from CLI env file.
- `web-list` JSON: {consumers:[{username,display_name,key_prefix,created_at,status,managed,today_calls}],day,timezone}.
- `web-usage` JSON: {consumers:[{username,display_name,calls}],day,timezone}.
- `web-create NAME --request-id UUID` JSON on first success: {username,display_name,key}; duplicate request never returns key again, does not create new account, returns already_created:true.
- CLI env uses CLICKHOUSE_READ_PASSWORD and CLICKHOUSE_URL; web queries as gateway_reader. State persists created_at/request_id and supports legacy users.

- [x] Add tests for enabled/disabled/foreign/pending consumers, masked keys, legacy timestamps, error propagation, midnight bounds, duplicate requests, invalid names, no key on duplicate.
- [x] Run `python3 -m unittest test_gatewayctl test_web_ops -v` from deploy/tikhub; confirm new tests fail.
- [x] Implement the interfaces using existing Inventory lock, apply_user and independent key generation; no user values interpolated into SQL.
- [x] Rerun focused tests, including existing gatewayctl regressions.
- [x] Commit the reviewed CLI change.

### Task 2: Session-protected HTML application

**Files:** Create deploy/tikhub/admin-web/app.py, templates/, static/style.css, requirements.txt, test_app.py.

**Interfaces:**
- `create_app(backend=None, environ=None)` FastAPI factory; `app=create_app()` production entry.
- Backend `.run(command, *args)` returns decoded CLI JSON or raises sanitized error. Fixed command allowlist.
- Environment ADMIN_PASSWORD required; ADMIN_ALLOWED_HOSTS, ADMIN_ORIGIN, GATEWAY_ENV_FILE, GATEWAY_STATE, GATEWAY_CTL configurable deployment settings.
- GET/POST /login; POST /logout; GET /; POST /consumers; POST /consumers/{id}/disable and /enable; GET /usage; authenticated once-only result route.

- [x] Add TestClient/fake backend tests for missing password, all anonymous routes, invalid credentials, brute-force backoff, successful login, logout/expiry/forgery, CSRF, Host/Origin/XFF handling, HTML escaping, argv boundaries and single reveal/idempotency.
- [x] Verify tests fail, then implement one worker in-memory opaque sessions (8 hours), short-lived login CSRF, hmac.compare_digest credential verification, no-store responses and once-only key delivery. Do not log credentials, request bodies or backend output.
- [x] Run focused tests. UI uses simple Chinese labels, employee identifiers, key prefix, time, status, counts and CSS bars. No CDN.
- [x] Commit the reviewed application change.

### Task 3: Compose, private initialization, migration and docs

**Files:** Modify deploy/tikhub/docker-compose.yml and README.md; Create admin-web/Dockerfile, admin_setup.py, admin_migrate.py, test_admin_setup.py; update .gitignore if needed.

**Interfaces:**
- Private /etc/tikhub-gateway/admin.env supplies ADMIN_PASSWORD, reader password and APISIX admin key copy, never master key. CLI-only minimal env is separate root0600.
- New inventory /etc/tikhub-gateway/consumer-store/consumers.json. Migration locks old and new, backups outside Git, no overwrite, dry-run/report/rollback CLI.
- Admin web uses checked static bridge address, APISIX allow_admin only adds that /32. Reader grant SELECT ON tikhub.gateway_access only.

- [x] Test migration retention, locks, ambiguity rejection, safe permissions and secret initialization with dummy values.
- [x] Implement root-only no-echo password setup and idempotent random reader password; keep existing credentials unchanged. Require 12+ password characters, no unsafe env newlines.
- [x] Add pinned Python base image and dependencies; non-writable root filesystem, no Docker socket, no master env mount, no capabilities, one worker, healthcheck uses authenticated gate redirect without probing TikHub.
- [x] Add optional admin profile so old gateway commands remain usable without admin secrets. README exact setup/start/verify/recover/password-change commands and HTTP scope.
- [x] Run all relevant Python tests and secret-safe Compose validation.

### Task 4: Remote acceptance and GitHub publication

**Files:** Update deploy/tikhub/ACCEPTANCE.md and plan checkboxes; export outputs/ internal admin delivery notes.

- [x] Check host 8081 and reserved IP availability, SSH health and other container baseline. Backup this gateway configuration/state privately.
- [x] Stage code and image, migrate inventory preserving key hashes, add read-only user and narrow Admin /32; recreate only changed project services.
- [x] User enters ADMIN_PASSWORD via no-echo setup tool in their terminal. Never transmit password in chat or print env.
- [x] Verify public anonymous redirect, wrong-password rejection, authenticated table/usage; create/disable/enable and clean a disposable test account, no paid calls. Verify reader cannot INSERT, minimal env/mounts, gateway and other services healthy.
- [x] Review diff, run secret checks; commit and push GitHub using existing authorized publishing access. Report actual deployed commit and limitations.
