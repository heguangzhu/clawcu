# Changelog

All notable changes to ClawCU are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- `--output {table|json|yaml}` global protocol — unify JSON schema across list/inspect.
- Provider bundle provenance via `.clawcu-instance.json` metadata.
- Active provider as a first-class field.

## [0.3.8] - 2026-04-22

### Changed
- A2A iteration 8 closure pass: the last four park items in the A2A backlog — P1-L (push-based peer-cache refresh), P2-J (fleet-wide shared outbound limit), P2-M (`a2a_list_peers` / `a2a_get_peer_card` MCP tools), P2-O (per-request role flag) — are formally closed as WONTFIX, same posture as P1-E (iter 6) and P1-K (iter 7). Each has a documented re-open clause tied to an external trigger (user ask, multi-sidecar deployment, multi-LLM per-sidecar). With this, every P0/P1/P2 opened across the iter 1–7 A2A review cycle is resolved; the backlog is empty. See `a2a-design-8.md` and `a2a-review-8.md` for the per-item justification. No code change.

## [0.3.7] - 2026-04-22

### Added
- A2A iteration 7 defensive cleanup: the outbound-limit sweep timer now emits a one-line `outbound-sweep failed: …` warning (Node `console.warn`, Python `logging.WARNING` on `clawcu.a2a.outbound_limit`) if `sweep()` ever raises. Sweep itself is still swallowed so the cleanup thread/timer never dies and never touches the request path — the log is purely a breadcrumb for operators grepping sidecar output.

### Changed
- Node sidecar: `createOutboundSweepTimer` is now wired inside `main()` instead of at module scope, so `require("server.js")` from a test file no longer starts a real `setInterval`. Production behavior is unchanged; Python already sited the call inside `main()` so no change there.
- Iteration 7 formally closes P1-K (bootstrap race against eager MCP config loading) as WONTFIX — verified that both OpenClaw and Hermes gateways still load MCP lazily in their latest releases, and there's been no user-facing signal that this is changing. Re-open if an upstream release ever moves to eager load.

## [0.3.6] - 2026-04-22

### Added
- A2A iteration 6 cleanup: outbound-limit periodic `sweep()` timer wired into both sidecars so long-running instances with many distinct `thread_id`s no longer accumulate empty buckets. Default 5 minutes; tunable via `A2A_OUTBOUND_SWEEP_INTERVAL_MS` (set `0` to disable). Node handle is `.unref()`'d; Python uses a daemon thread — neither blocks graceful shutdown.
- Optional `role` field in the templated MCP tool description, gated on `A2A_TOOL_DESC_INCLUDE_ROLE=true`. When enabled the per-peer line renders `- analyst [senior market analyst] (market data, ...)`; empty roles cleanly omit the brackets. Default stays off so the description is byte-for-byte unchanged from 0.3.5.

### Changed
- Iteration 6 formally closes P1-E (JSON log format toggle) as WONTFIX — six iterations with zero user asks, not shipping a format the docs don't advertise. Reconsider if an ask lands.

## [0.3.5] - 2026-04-22

### Added
- A2A iteration 5 headline: MCP tool description is templated with a live peer summary pulled from the registry, so the LLM reads "Available peers: analyst (market data), editor (prose)" on `tools/list` instead of a static blurb. Helps the LLM decide *which* peer to call before inventing a name that 404s. 30-second TTL cache on the peer list (5-minute stale-OK) keeps chatty LLMs off the registry; self-exclusion prevents A→A loops; peers beyond the 16-cap collapse to `...and N more`. Registry hiccups never fail `tools/list` — a static fallback is always there. Rollback: `A2A_TOOL_DESC_MODE=static` env var restores the old static description. Identical semantics on both runtimes (Node `mcp.js` + Python `sidecar.py`).
- MCP error responses now carry `requestId` in the `data` object on every JSON-RPC error path, so a JSON-RPC-only client can correlate an error to the `X-A2A-Request-Id` header without parsing transport headers.
- `outbound_limit` empty-bucket cleanup (`sweep()`): long-running sidecars that see many one-shot `thread_id`s no longer grow the `hits` dict unboundedly. Opportunistic cleanup — cheap when key count is small, skippable under load.
- CLI warns (but does not fail) when `--a2a-hop-budget > 16`: past that soft ceiling the hop budget stops being a useful loop-protection knob.
- Adapter × bootstrap integration tests: one pytest for Hermes YAML (Python-to-Python) and one for OpenClaw JSON that spawns Node to drive `bootstrap.js` with the env the Python adapter produces. Catches drift between what `run_spec()` injects and what `run_bootstrap()` reads.

## [0.3.4] - 2026-04-22

### Added
- A2A iteration 4 headline: sidecar-side auto-wiring of the `a2a` MCP entry into the service's `mcpServers` config on start when `A2A_ENABLED=true` (and reversal/cleanup when off). Closes the last gap between "MCP server exists" (0.3.3) and "LLM sees the tool on first chat turn." Adapter injects `A2A_SERVICE_MCP_CONFIG_PATH`/`A2A_SERVICE_MCP_CONFIG_FORMAT` defaults per service (OpenClaw JSON, Hermes YAML); a user-provided env file wins. Merge is atomic (temp file + rename) and refuses to overwrite malformed configs.
- Self-origin outbound rate limit, shared bucket across `/a2a/outbound` and `/mcp` tool-call. Keyed on `thread:<id>` when present, else `self:<agent-name>`. Default 60 calls / rolling 60s / key, tunable via `A2A_OUTBOUND_RATE_LIMIT`. Prevents one LLM turn with 200 parallel `a2a_call_peer` calls from nuking the provider quota. Over-limit returns `HTTP 429` with `retry_after_ms` on the REST path and JSON-RPC `-32001` with `{httpStatus: 429, retryAfterMs}` on the MCP path.
- Node end-to-end `/mcp` test: spins up the sidecar binary with stub registry + stub peer and exercises `initialize`, `tools/list`, `tools/call` happy + unknown-peer paths — the `/mcp` handler is a closure inside `main()` and unit tests alone did not cover the wiring.

## [0.3.3] - 2026-04-22

### Added
- Co-resident MCP server on the A2A sidecar: `POST /mcp` on the same neighbor port speaks streamable-http and exposes a single tool `a2a_call_peer(to, message, thread_id?)` that wraps `/a2a/outbound` in-process. Lets the LLM call other agents natively without any hand-rolled HTTP tool. Auto-wiring into the service's `mcpServers` config is tracked as a follow-up — for now point `mcpServers.a2a.url` at `http://127.0.0.1:<bridge-port>/mcp` (shown by `clawcu inspect`). See [docs/a2a-sidecar.md §LLM-facing MCP tool](docs/a2a-sidecar.md).
- `clawcu inspect <instance>` A2A section now surfaces `A2A_HOP_BUDGET`, `A2A_REGISTRY_URL`, and the auto-registered MCP server URL so operators can read the A2A wiring without `getenv | grep`.

### Fixed
- Socket-error status codes unified across both sidecars and both endpoints: `504` for network-layer failures (socket timeout, connection refused, DNS), `502` for peer-reported HTTP errors. Previously `/a2a/send` mapped URLError to `502` while `/a2a/outbound` mapped it to `504`, confusing grep-based debugging.

## [0.3.2] - 2026-04-22

### Added
- `clawcu create --a2a-hop-budget N` sets the sidecar's per-request hop limit (default `8`). The value is validated at the CLI layer (`N >= 1`, requires `--a2a`) and persisted to the instance env file as `A2A_HOP_BUDGET` so it survives `clawcu recreate`. Lets operators tune the loop-detection threshold without editing env files by hand. See [docs/a2a-sidecar.md §Configuring the hop budget](docs/a2a-sidecar.md).
- `X-A2A-Request-Id` correlation: both sidecars accept an incoming `X-A2A-Request-Id` header (or mint a fresh opaque id when absent), log it at every /a2a/send and /a2a/outbound transition, forward it to the next hop on `/a2a/outbound`, and echo it in every response body (`"request_id"` key) + response header. Operators can now grep sidecar logs across containers for a single federation call. Plays nice with any higher-layer id — if you pre-tag, the sidecar keeps your id.

### Fixed
- A2A outbound on Linux Docker: the openclaw/hermes adapters now pass `--add-host host.docker.internal:host-gateway` when `--a2a` is set, and inject a default `A2A_REGISTRY_URL=http://host.docker.internal:9100` (respecting user overrides) so `/a2a/outbound` can reach the host-side registry on Linux — Docker Desktop already resolved this DNS name automatically.

## [0.3.1] - 2026-04-22

### Added
- A2A outbound primitive: sidecars expose `POST /a2a/outbound` on the same container-local port as `/a2a/send`. Any in-container caller (future MCP server, native plugin, or author-written tool) POSTs `{to, message, thread_id?, registry_url?, timeout_ms?}` and the sidecar handles registry lookup + peer forwarding, returning `{from, to, reply, thread_id}`. Closes the mid-turn "Agent A must query Agent B before replying" gap without touching the service's tool-calling system. See [docs/a2a-sidecar.md §Outbound A2A from within the container](docs/a2a-sidecar.md).
- Hop-budget loop protection: `X-A2A-Hop` integer header increments on every outbound POST; inbound `/a2a/send` rejects with `508 Loop Detected` when `hop >= A2A_HOP_BUDGET` (default `8`). Prevents A→B→A→B runaways.

## [0.3.0] - 2026-04-22

### Added
- A2A v0 protocol: `--a2a` flag at instance creation bakes a sidecar into the service image so it exposes `GET /.well-known/agent-card.json` + `POST /a2a/send` on a neighbor port alongside the stock service. Works for both OpenClaw (Node sidecar) and Hermes (Python sidecar). Image tag `clawcu/{service}-a2a:{base}-plugin{clawcu-version}.{sha}` uses a source-sha fingerprint so editable-install drift still triggers a rebake.
- `clawcu hermes identity set <name> <path>` installs a user-authored SOUL.md into a Hermes instance's datadir so `prompt_builder.load_soul_md` picks up the new persona on the next chat turn (no restart).
- A2A sidecar layer: per-peer rate limit, tee'd log file, readiness probe against gateway, optional conversation-history via `thread_id` (path-traversal hardened; per-peer JSONL under `<datadir>/threads/`).

## [0.2.11] - 2026-04-22

### Added
- `create` and `upgrade` now accept optional `--image` overrides while keeping `--version` as the required logical version label. The selected runtime image is persisted on the instance so later `recreate`, orphan recovery, and `rollback` keep following the same image chain.

## [0.2.10] - 2026-04-19

### Added
- `clawcu list` caches the "Available versions" registry fetch at `<clawcu_home>/cache/available_versions.json`, valid for the local calendar day. Subsequent runs on the same day are served from cache; a new day, a changed `image_repo`, or `--no-remote` triggers a refetch. Failures are never cached so transient outages do not linger.
- When the registry fetch fails (network down, DNS, auth) or `--no-remote` is set, the footer surfaces local Docker images as an offline fallback on a continuation line under the error, so the user sees actionable candidates instead of just a red error.

## [0.2.9] - 2026-04-19

### Added
- `clawcu list` appends an "Available versions" footer with the 10 most recent stable releases per service (OpenClaw, Hermes), newest first. Prereleases (`-beta`, `-rc`, `-alpha`) are filtered; `upgrade --list-versions` still exposes them for testers. Skipped in `--json` / `--agents` / `--removed` views; pass `--no-remote` for a strictly offline render.

### Fixed
- `clawcu <cmd>` with no args now prints full help and exits 0 when the command requires arguments — it's a "what does this take?" query, not a failed invocation. Partial invocations (some args, still missing a required one) keep POSIX exit 2 but now print the full help alongside the `Missing option` error so every flag is visible.

## [0.2.8] - 2026-04-19

### Added
- `list --removed` surfaces the `port` from `.clawcu-instance.json` metadata when available (falls back to `-` for pre-metadata orphans).

### Fixed
- `remove --removed` now explicitly rejects `--delete-data` / `--keep-data`; previously those flags were silently accepted despite having no effect.

## [0.2.7] - 2026-04-19

### Added
- `list` rejects conflicting `--source` / `--removed` / `--local` / `--managed` / `--all` combinations with explicit errors.

### Fixed
- Hint for "Removed instance 'X' was not found" now correctly points at `list --removed` instead of the generic "instance not found" suggestion.

## [0.2.6] - 2026-04-18

### Added
- Orphan instance lifecycle: `list --removed`, `recreate <orphan>`, `remove <orphan> --removed`.
- Self-describing datadir via `.clawcu-instance.json` metadata.

## [0.2.0] - 2026-04-17

### Added
- Multi-service support: `openclaw` and `hermes` as first-class services.
- Shared command surface across services.

## [0.1.0] - 2026-04-15

### Added
- Initial public release.
- OpenClaw lifecycle management: pull, create, list, inspect, start/stop, upgrade, rollback, clone, snapshots.

[Unreleased]: https://github.com/heguangzhu/clawcu/compare/v0.3.8...HEAD
[0.3.8]: https://github.com/heguangzhu/clawcu/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/heguangzhu/clawcu/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/heguangzhu/clawcu/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/heguangzhu/clawcu/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/heguangzhu/clawcu/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/heguangzhu/clawcu/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/heguangzhu/clawcu/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/heguangzhu/clawcu/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/heguangzhu/clawcu/compare/v0.2.11...v0.3.0
[0.2.11]: https://github.com/heguangzhu/clawcu/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/heguangzhu/clawcu/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/heguangzhu/clawcu/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/heguangzhu/clawcu/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/heguangzhu/clawcu/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/heguangzhu/clawcu/compare/v0.2.0...v0.2.6
[0.2.0]: https://github.com/heguangzhu/clawcu/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/heguangzhu/clawcu/releases/tag/v0.1.0
