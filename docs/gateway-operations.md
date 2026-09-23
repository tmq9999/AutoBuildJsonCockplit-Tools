# Gateway operations

Gateway is opt-in and separate from the existing OAuth workbench. The approved
Vietnamese administration UI is implemented at `/service/` on the private admin
listener. Independent review findings have regression fixes. Live provider contract
verification and production deployment remain operator acceptance steps.

## Local setup

Install Python extras `.[dev,service,gateway-test]`. PostgreSQL 17 is required for
the service, not for OAuth-only mode. Use a dedicated database and role. Configure:

```text
AUTOBUILD_GATEWAY_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/DATABASE
AUTOBUILD_GATEWAY_MASTER_KEY_FILE=/absolute/private/keyring.json
```

Never paste these values into tickets. CLI reads environment variables; this page
contains only examples, not working credentials. Initialize the keyring explicitly:

```bash
.venv/bin/python -m autobuild_json.gateway init-keyring --key-file /absolute/private/keyring.json
.venv/bin/python -m autobuild_json.gateway migrate
.venv/bin/python -m autobuild_json.gateway serve
```

Run private admin in another process using `python -m autobuild_json.gateway admin`;
it keeps existing local admin sessions/CSRF and mounts `/api/service/`. Default
gateway address is `http://127.0.0.1:8788`, admin `http://127.0.0.1:8787`.
Check whether the original admin/OAuth server is running before using the same port;
use `admin --port 8789` to avoid interrupting it. Open `http://127.0.0.1:8789/service/`
for the new UI. Set a **different** `AUTOBUILD_DATA_DIR` if the original OAuth server
is running; two admin coordinators must not own the same data directory.
Do not start two OAuth coordinators
against the same data directory. Existing `python -m autobuild_json` is unchanged.

Run `python -m autobuild_json.gateway maintenance` for expired-request reconciliation.
One PostgreSQL lease holder does recovery; starting two maintenance workers does not
permit duplicate settlement. It never replays generation or automatically releases
Kiot keys. A dispatched request without usage remains `usage_pending`; the operator
must reconcile using evidence and an audited adjustment. A crashed request whose
authoritative usage was persisted can be settled automatically once.

## API configuration

Create customer → issue key → create provider and encrypted credential → create
public model and binding → grant the key model/protocol/quota policy. New keys have
zero total quota and an empty model allowlist. Quota fields are integer micro-token
units: 1 token quy đổi = 1,000,000 micro-units. Decimal coefficient parsing is exact;
never convert quota through JavaScript floating-point arithmetic. Private admin
API serializes micro-unit fields as decimal strings; it accepts validated digit
strings or integers. The UI displays token units and converts with BigInt.

The UI includes customers, key creation/edit/rotation/revocation, protocol/model
permissions, quota and model coefficients, provider/credential configuration,
model mapping, proxy profiles, OAuth import/refresh, usage/audit and a JSON
playground. Playground uses a client key supplied in memory and charges that key;
it does not grant admin bypass of model or quota policy. Provider discovery stages
names for review and never publishes them automatically. Secrets remain write-only.

Provider `rpm_limit` and `concurrency_limit` are database-authoritative and shared
across customer keys. Credential defaults also cap RPM=600 and concurrency=16;
the stricter provider/credential bound wins. A route reporting usage beyond its
input/output bound is disabled for investigation, even with zero token coefficients.
Customer charge stays capped by its reservation; actual upstream cost stays separate.

Public endpoints require the issued client key. Admin cookies/tokens never work as
upstream keys. Provider credentials are write-only. Supported core endpoints:
OpenAI Chat/Responses, Anthropic Messages, Gemini generate/stream, Ollama chat/generate.
Unsupported options return explicit errors; model compatibility is route-dependent.
Codex OAuth is contract-specific and live-unverified until an authorized smoke
test succeeds. It does not silently discard a requested unsupported output cap.

## Proxy configuration

OAuth batch API accepts `proxy_mode`: `legacy`, `direct`, `fixed`, `pool`,
`kiotproxy`, `profile`. Static entries use `proxies_text`; Kiot keys use
`kiot_keys_text`, one key per line, plus `proxy_region` and `proxy_protocol`.
`profile` requires service mode and `proxy_profile_id`. Secrets are never stored
in public request logs or browser storage. Manual OAuth link/token exchange retains
its static proxy option; browser egress is the operator's responsibility.

Provider proxy profiles are operator configuration; assigning one explicitly trusts
its public proxy endpoints. Unassigned ad-hoc proxies require an explicit origin
allowlist. Private endpoints require both origin and CIDR allowance. Remote DNS
needs enforcement on the trusted proxy/firewall.
Environment proxy variables are ignored. Kiot leases are exclusive per key; slots
are held through the HTTP operation, cooldown/TTL are respected, and no direct
fallback or automatic `/out` occurs. Control API logs omit the query key.

## Keyring rotation and backups

Keyring contains an active encryption key (`v1` initially) and a **stable** separate
`client_keys` key for client-key HMAC and request digests. Never regenerate
`client_keys` during encryption-key rotation: that would invalidate issued keys.
Retain old encryption versions until all records and backups using them have been
migrated. Keyring files are private regular files, not symlinks; mode 0600 on POSIX.

```bash
.venv/bin/python -m autobuild_json.gateway backup --output /private/new-snapshot.enc
.venv/bin/python -m autobuild_json.gateway restore --input /private/new-snapshot.enc --acknowledge-empty-target
```

Backup is a repeatable-read logical snapshot, encrypted with an authenticated key
derived from the active keyring version; the keyring itself is never included.
Current snapshot size bound is 64 MiB plaintext. Export refuses to overwrite a file.
Restore requires matching schema revision, required decryption keys and client-key
HMAC material, and refuses any populated target. Restore holds table locks and
inserts all rows in one transaction. Use a new database, migrate it, then restore;
never repoint restore at production to test it. Test fixtures exercise this flow
with synthetic records. Legacy `data/runs/*` and JSON exports are not automatically
included or encrypted by this service backup; protect them separately.

## Publishing and limitations

Loopback is the default. Non-loopback bind requires explicit allowed Host values,
trusted reverse-proxy IPs and TLS-proxy confirmation. Do not use wildcard trusted
forwarders. The reverse proxy must enforce aggregate pre-auth rate/body limits;
the built-in pre-auth IP guard is bounded and process-local. Issued-key quota/RPM
and concurrency remain database-authoritative across gateway workers.

Gateway does not execute model tool calls or shell commands. It does not grant
rights to resell a provider's service; verify the provider contract independently.
Do not route customer traffic through providers that prohibit the intended use.
No live account or Kiot key was used in offline tests. No public deployment has
been performed by this implementation.
