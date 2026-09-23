# Implementation decisions and trade-offs

This is the exhaustive per-plan ruling record at the UI handoff; later entries supersede earlier checkpoint assumptions. Decisions are retained for review without including secrets or account data.

- Ruling: “duyệt” approves the recommended Native execution and worktree setup in the written plan — no repeated approval prompt — cost if wrong: user can change execution method without losing work.

- Ruling: Task 1's PostgreSQL setup may build official PostgreSQL 17 binaries in ignored worktree-local dependencies, without system installation — no Docker/PostgreSQL is installed — cost if wrong: local build time and disk only, no host service changes.

- Task 1: Ruling: use test-only Zonky packaged PostgreSQL 17.6 from Maven Central after official source configure proved bison absent — SHA256 23da5a044b4fb7a5a081a45008c95749c873305328d73f86aefd56922ce1d29d verified; binaries ignored, not shipped — cost if wrong: test-only binary supply-chain exposure, no system service or real credentials.

- Task 1: Ruling: test fixture directly owns a random password-protected temporary PostgreSQL cluster when no explicit URL exists — easier reproducible isolated integration tests; original URL mode also requires dedicated loopback abgw_test database — cost if wrong: additional seconds per test session.

- Task 1: Ruling: test schemas use libpq/psycopg connections only after validated loopback test URL; Python outbound guard stays fully closed, including unrelated localhost ports — libpq bypasses Python socket methods — cost if wrong: native connector guarding depends on URL validation, addressed by explicit rejection tests.

- Task 1: Ruling: use build --no-isolation in the already isolated worktree venv because Python 3.14 has no ensurepip — default build failed before compilation — cost if wrong: packaging is verified against this venv, not a freshly pip-created build env.

- Task 1: Ruling: migrations take an explicit connected Database instead of an ini with a connection URL; test helper lifetime owns its temporary cluster without a persistent manifest — avoids persisted credentials and accidental independent migration target — cost if wrong: CLI must call migrate.upgrade explicitly (Task 18).

- Task 2: Ruling: client-key HMAC pepper must have at least 32 bytes; tests use 32-byte synthetic values rather than short plan examples — avoids accepting weak configured pepper — cost if wrong: old short test configs must be replaced.

- Task 4: Ruling: reservations and active slots are durable request rows instead of duplicate tables, with customer/key lock serializing all admissions/settlements — same accounting authority with fewer divergent states — cost if wrong: future queries must account for request state/deadline explicitly.

- Task 4: Ruling: provider monetary budgets use separate transaction/attempt holds after customer admission, with Engine compensating a failed budget admission by releasing the unspent customer hold — no HTTP before both holds exist — cost if wrong: crash between holds leaves a conservative pending hold for recovery, not overspend.

- Task 5: Ruling: aliases point only to canonical models (not alias chains), rejecting cycles structurally — simpler explicit model permission check — cost if wrong: operators must map alias directly to final model.

- Task 6: Ruling: use own HTTPX AsyncBaseTransport over httpcore pools with GuardedBackend, rather than mutating HTTPX private pool; httpcore AutoBackend is the only private import — enables connection-time IP pinning while preserving Host/SNI — cost if wrong: pin tested httpcore 1.x and adapt import on major upgrade.

- Task 7: Ruling: Kiot control client invokes HTTPX transport directly (not AsyncClient logging wrapper) to avoid logging query credentials; production uses guarded CoreTransport — no global logger suppression — cost if wrong: custom call wrapper must preserve timeout/close behavior, covered in client/manager tests.

- Task 7: Ruling: lease hard deadlines do not extend on renew; renew verifies ownership and heartbeat aborts at deadline/lost DB — makes reclaim after hard deadline+15s safe without indefinite lease extension — cost if wrong: long operations must request an adequate deadline at admission.

- Task 10: Ruling: ingress IP flood limiter is bounded process-local defensive guard; financially authoritative key RPM/concurrency remains PostgreSQL. Deployment reverse proxy must enforce global pre-auth limits across workers — avoids DB write per unauth request — cost if wrong: per-IP allowance scales with worker count until reverse-proxy rule applied.

- Task 10: Ruling: custom streaming ASGI response consumes generator in the same task that acquired proxy lease; disconnect watcher cancels that task — avoids context-owned heartbeat cancelling a different Starlette stream task — cost if wrong: maintain ASGI response lifecycle tests explicitly.

- Task 12: Ruling: same key RPM/concurrency authority is used for count and generation, a stricter combined limit rather than a second unconstrained counter — prevents free counter flooding while not charging generation tokens — cost if wrong: frequent counters reduce generation RPM available to the key.

- Task 15: Ruling: imports are staged unverified and deduplicated by issuer/account ID; explicit verified refresh promotes health. Avoid trust in expired/unverified JWT metadata — cost if wrong: first activation requires refresh rather than instantly using access token.

- Task 15: Ruling: Codex cannot honor explicit max_output_tokens, so Engine reserves certified model output bound without injecting a cap; requests with explicit unsupported cap fail before dispatch — cost if wrong: clients always sending caps cannot use that route until contract supports one.

- Task 16: Ruling: proxy lease encloses the HTTP attempt, while coordinator's source reservation remains through durable terminal write. Shared external lease releases when HTTP ends rather than after file write — no IP-sensitive HTTP remains at that point — cost if wrong: another worker may rotate after HTTP completes but before result file persistence.

- Ruling: continue independent Task18/19 backend operations and SDK checks while Task17 awaits required UI draft approval — plan explicitly permits backend progress; no implementation of unapproved UI — cost if wrong: task completion order differs, ledger is authoritative.

- Task 18: Ruling: encrypted logical backup limit 64 MiB; use pg row JSON under repeatable-read, restore to empty same-revision schema under exclusive table locks — avoids requiring pg_dump binaries in app deployment — cost if wrong: larger installations need a dedicated larger backup strategy before scale-up.

- Task 18: Ruling: keyring separates stable client_keys HMAC secret from active encryption key, to prevent encryption rotation from revoking client API keys — cost if wrong: keyring restore must preserve that additional key; backup tests check key identity.

- Ruling: monetary/weighted micro units use decimal strings on private admin wire to preserve values beyond JS safe integer; public inference token usage stays numeric — prevents rounding — cost if wrong: integrations to private admin must accept documented decimal strings.

- Ruling: production uses same approved split table/editor layout for entity tabs; detailed forms reflect actual API semantics (model permission shared across protocols, per-model coefficients) rather than illustrative per-protocol pricing blocks — spec is authority — cost if wrong: no separate per-protocol prices.

- Final rulings on declined judgments: live provider/Kiot correctness remains unverified without authorized live keys; remote proxy DNS requires operator firewall/trusted proxy; resale rights remain provider-contract responsibility. These are documented deployment limitations, not claimed test coverage. Visual fidelity assessed by main agent local Chrome screenshots against v3, not by code reviewer.

