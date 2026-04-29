# ClawCU v0.4.1

🌐 Language:
[English](RELEASE_v0.4.1.md) | [中文](RELEASE_v0.4.1.zh-CN.md)

Release Date: April 29, 2026

> `v0.4.1` polishes the CLI lifecycle surface based on heavy-user feedback. Every change is additive or opt-in; no breaking changes to existing workflows.

* * *
## Highlights

### CLI Redesign (v0.4.x foundation)

- **`list` default is now version-free**
  - `clawcu list` no longer prints a version footer. Pass `--versions` to see available service versions when you need them.
  - Scripts that parsed the old footer will need no change — the footer was human-readable, not structured.

- **`token` / `approve` moved under `clawcu openclaw`**
  - Root-level `clawcu token` and `clawcu approve` still work as hidden aliases with a deprecation warning.
  - New location: `clawcu openclaw token <name>` and `clawcu openclaw approve <name>`.

- **Removed `pull`**
  - `clawcu create` now auto-pulls via the service layer. The separate `pull` step was redundant.

- **Removed `hermes identity set`**
  - Use `docker cp` or `clawcu exec <name>` to edit Hermes persona files directly.

- **Removed `a2a up` and `a2a bridge serve`**
  - A2A is strictly opt-in at create time (`--a2a`). Long-lived services should be deployed via docker-compose or systemd, not the CLI.

### Scenario Optimizations

- **`tui` checks instance status before launch**
  - If the instance is stopped, the CLI exits with a clear message: `Run clawcu start <name> to start it before entering the TUI.`
  - No auto-start — on resource-constrained machines the user decides which instance to start.

- **`remove` auto-stops running instances**
  - `clawcu remove <name> --delete-data` now stops a running container before removal (10s grace).
  - Previously this failed with a Docker error; now it's a single command.

- **`logs --follow` ANSI reset on Ctrl+C**
  - Pressing Ctrl+C during `clawcu logs <name> --follow` sends an ANSI reset sequence so the terminal does not keep Docker's colour codes.

- **`getenv --table` grouped output**
  - `clawcu getenv <name> --table` renders env vars in a rich table grouped by A2A / Sensitive / General.
  - Sensitive values are masked by default; pass `--reveal` to show them.

- **`setenv --reload` hot-reload**
  - `clawcu setenv <name> KEY=VALUE --reload` sends SIGHUP to the running container after writing the env file.
  - Best-effort: services that support signal-triggered config reload benefit without recreation. If the signal fails, the CLI tells you to use `--apply` instead.

- **`snapshots` subcommand group**
  - `clawcu snapshots list [name]` — list upgrade/rollback snapshots for an instance (or all instances).
  - `clawcu snapshots clean --keep-last N [name]` — prune old snapshots, keeping the most recent N per instance.
  - After every successful `upgrade`, ClawCU automatically prunes snapshots older than the most recent 10 (history-referenced snapshots are never deleted).

- **`upgrade --list-versions` fallback enhancement**
  - When the remote registry is unreachable, the CLI now prints a prominent `[green]Fallback:[/green]` message showing local images and the exact command to upgrade from a local tag.

- **`config` help with service-specific examples**
  - `clawcu config --help` now shows OpenClaw and Hermes usage examples, including `--non-interactive` passthrough.

### A2A Evolution (since v0.3.0)

- OpenClaw sidecar ported from Node.js to Python (stdlib only) for parity with Hermes.
- Shared `_common/` package extracted — inbound limits, outbound HTTP, MCP dispatcher, peer cache, protocol helpers, and readiness probes are now unified across both sidecars.
- Security hardening: scheme allow-list (`http`/`https` only), redirect blocking, 4 MiB response body caps, socket-level request timeouts, Content-Length validation, and oversized-body rejection.
- True streaming + async task store/worker (layer 3).
- A2A_REGISTRY_TOKEN bearer gate on registry reads.
- `--lookup-timeout` flag on `clawcu a2a send`.

* * *
## Compatibility

`v0.4.1` is a drop-in upgrade from `v0.3.0`.

- Existing managed instances keep running with the same image tag, ports, and env.
- The removed commands (`pull`, `hermes identity set`, `a2a up`, `a2a bridge serve`) were hidden or undocumented in `v0.3.0` usage.
- `clawcu token` / `clawcu approve` root aliases still work with a deprecation warning.
- New flags (`--table`, `--reload`, `--versions`) are strictly opt-in.

* * *
## Test Coverage

**848 tests** (pytest), up from 479 at `v0.3.0`.

* * *
## Recommended Workflow

```bash
# Create and start
clawcu create openclaw --name writer --version 2026.4.1

# Check env before sharing a clone
clawcu getenv writer --table

# Clone without secrets, then apply a provider
clawcu clone writer --name writer-shared --exclude-secrets
clawcu provider apply <provider> writer-shared

# Upgrade with safety snapshot + auto-cleanup
clawcu upgrade writer --version 2026.4.2

# Clean up old snapshots manually if needed
clawcu snapshots clean --keep-last 5
```

* * *
## Closing Note

`v0.4.x` is the first release to treat the CLI as a **lifecycle tool**, not a runtime. Long-lived services (registry, bridges) are removed from the CLI surface; the CLI focuses on create, start, stop, upgrade, rollback, remove, env, logs, and snapshots. A2A remains opt-in at create time and works exactly as before.
