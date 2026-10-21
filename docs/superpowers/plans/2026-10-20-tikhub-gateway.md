# TikHub Gateway Implementation Plan

> **For agentic workers:** Implement natively, one stage at a time. The user has explicitly authorized continuing automatically after each stage passes. Do not wait for a repeated stage approval. Missing secrets and paid-request constraints remain real execution gates.

**Goal:** Give each employee an independent key for transparent TikHub proxying and asynchronous ClickHouse audit logs.

**Architecture:** Keep the existing single-instance APISIX + etcd Compose project. Manage consumers and routes through a repository-owned Python CLI and root-only runtime state outside Git. Add an isolated mock upstream and ClickHouse through Compose; production 9080 is published after authentication and audit verification; admin and ClickHouse stay on loopback.

**Tech Stack:** APISIX 3.19.0, etcd 3.6.15, Docker Compose v2, Python standard library, ClickHouse.

**Spec:** User-approved requirements in this chat, including the later changes: no employee numbers exist; names are displayed and mapped to stable internal employee IDs; future Feishu integration is outside this implementation; general audit logs may lose data during exceptional failures; at most one real validation request and at most USD 0.50.

## Global Constraints

- No real keys, employee names, or private inventory in Git or reports.
- Only the `apikey` header authenticates coworkers. Query parameters and Bearer credentials do not authenticate them.
- Strip only the gateway prefix; preserve method, raw Query, request body, upstream status and response body.
- Override the upstream Authorization header; remove the coworker's key header.
- No automatic retries of business requests. No real request before the key is securely entered and endpoint price has been checked against the authorized budget.
- Do not modify or restart unrelated server stacks, Docker configuration, or public firewall rules.
- Use local counters, not Redis. Rates count attempts and are not a currency ledger.
- All infrastructure and policy changes originate from this repository; private user/key inventory is managed by the CLI.

## Review Focus

- Duplicate/missing headers, query-only keys, spoofed identity headers: fail before reaching upstream.
- Encoded/repeated/empty Query values and binary bodies: prove preservation against an isolated echo service.
- Revocation, rotation and a worker's asynchronous configuration update: observe eventual runtime behavior with a bounded wait.
- Logger outage and unauthorized requests: preserve business availability, log 401/429, and explain retry/drop limits.
- Interrupted deploy or missing secret: leave the live route disabled and retain recoverable private state.

### Stage 2: Individual credentials

Files: `deploy/tikhub/gatewayctl.py`, `test_gatewayctl.py`, `receive_tikhub_key.py`, `README.md`.

Interfaces: private inventory `/etc/tikhub-gateway/consumers.json`; Admin API at `127.0.0.1:9180`; `person-add`, `user-disable`, `user-rotate`, `user-key`, `apply-users`, `init-tests` commands. Consumer names are stable internal IDs and display names are private metadata.

- [x] Add locked, atomic 0600 inventory storage and idempotent managed-consumer synchronization.
- [x] Add header-only pre-auth guard and a loopback acceptance route returning the authenticated identity.
- [x] Test missing/wrong/duplicate/query-only keys, two identities, revocation and rotation.
- [x] Deploy and record only sanitized results.

### Stage 3: Transparent proxy

Files: `gatewayctl.py`, `docker-compose.yml`, `test_gatewayctl.py`, `README.md`.

Interfaces: mock route uses `/__gateway_acceptance/tikhub/api/v1/*`; production uses `/tikhub/api/v1/*`. Both use the same route builder. Production Authorization is an APISIX environment-secret reference; the mock route uses a fake token only.

- [x] Add a private mock service using the existing pinned APISIX image; no published mock ports.
- [x] Verify methods, exact Query, binary body, prefix stripping, overwritten Authorization and removed apikey.
- [x] Add a live route only once the real secret is present. Keep retry count zero.
- [x] Research the cheapest official endpoint and perform the single authorized request only if its cost is known to fit USD 0.50.

### Stage 4: Per-employee count limits

Interfaces: private per-user `count` and `time_window` policies, `consumer_name` counter key, `policy=local`, rejection 429.

- [x] Add quota management and shared counters across managed business routes.
- [x] Verify isolation between two test users, rejection without reaching upstream, and the local-counter restart boundary.
- [x] Document default limits clearly and allow operator changes through the CLI.

### Stage 5: Audit storage

Files: `docker-compose.yml`, `gatewayctl.py`, `clickhouse.sql`, tests and README.

Interfaces: ClickHouse listens only on loopback and the private Compose network. The APISIX logger has a fixed field allowlist and environment-referenced credentials. Use finite retries; failures are observable through a repository health command and Docker error logs.

- [x] Pin the official ClickHouse image; configure persistent data and root-only runtime credentials.
- [x] Create a bounded-retention table and least-privilege log writer.
- [x] Verify 200/401/429/upstream failures, identity attribution, secret exclusion and outage/recovery behavior.
- [x] Provide queries for usage by employee and endpoint without claiming actual monetary spend.

### Stage 6: Operational acceptance

- [x] Verify restart persistence, revoke/rotate commands, backups and recovery instructions.
- [x] Remove temporary test keys and disable acceptance routes after the final test; keep the optional mock profile out of normal startup.
- [x] Match GitHub, local and server files byte-for-byte, publish sanitized acceptance results, and report any unverified real-world boundary explicitly.

## 2026-10-20 execution note

Stages 2, 4 and 5 passed runtime acceptance. Stage 3 mock preservation passed. The sole live attempt returned 502 during upstream TLS verification before HTTP transmission; NGINX static proxy verification is now enabled to load the system CA store at startup, and verification depth is explicitly 5. A separate additional-attempt authorization was requested. Do not retry the business request without that answer.

## 2026-10-21 completion

The user explicitly authorized one additional real request after the TLS failure. The original ledger is preserved, the dated follow-up ledger reserves at most one extra request, and three network-free tests pin those boundaries. The additional request returned HTTP 200 and business code 200; its single ClickHouse row has the correct employee identity, request ID and upstream 200. No response credentials were exposed. Publication is checked against local and deployed source trees before final delivery.
