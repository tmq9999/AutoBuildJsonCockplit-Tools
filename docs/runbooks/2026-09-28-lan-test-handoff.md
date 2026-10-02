# LAN test switch — 2026-09-28

Scope: stop this project's tunnel bridges and expose only the public gateway
on `192.168.134.128:8788`. Keep admin and PostgreSQL loopback-only; preserve the
current feature worktree, private data, keyring and proxy configuration.

- [x] Inspect interface, listeners, current runner and overlapping edits.
- [ ] Observe failing tests for explicit RFC1918-only HTTP opt-in.
- [ ] Add settings validation and runner `--gateway-host`; document behavior.
- [ ] Run focused regression tests, full available suite and static checks.
- [ ] Stop only verified project tunnel processes; restart the existing runner.
- [ ] Verify LAN health, authentication/Host boundaries, private admin listener.

Rollback: restart the same runner with `--gateway-host 127.0.0.1`; do not
recreate or reset operator data. Do not restart public tunnels automatically.

HTTP LAN mode is for trusted-network testing only. No live model request is
part of this switch; host-side reachability still needs the operator's check.
