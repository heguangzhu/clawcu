# Changelog

All notable changes to ClawCU are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- `--output {table|json|yaml}` global protocol — unify JSON schema across list/inspect.
- Provider bundle provenance via `.clawcu-instance.json` metadata.
- Active provider as a first-class field.

## [0.3.22] - 2026-04-23

### Fixed
- **P2-N1 (a2a-review-22)** — `call_hermes` in the Hermes Python sidecar read the local upstream `/v1/chat/completions` response with unbounded `resp.read()` — the iter-21 `_read_capped` cap only covered outbound peer/registry responses, not the inbound local-upstream path. A buggy or misconfigured Hermes that streams an unbounded response could OOM the sidecar; each concurrent `/a2a/send` from a remote peer triggers a separate `call_hermes`, multiplying memory pressure. Fix: `call_hermes` now uses `_read_capped(resp, cap=A2A_LOCAL_UPSTREAM_CAP)` where `A2A_LOCAL_UPSTREAM_CAP = 64 MiB` (separate from the 4 MiB outbound cap because local chat completions with tool calls can legitimately be several MB). The HTTPError catch in `/a2a/send` also caps the upstream error body at 4 KiB (only `body[:500]` is used). Two new Python tests exercise both the 2xx and HTTPError paths against stub servers streaming oversized payloads.
- **P2-O1 (a2a-review-22)** — `readJsonBody` in the OpenClaw Node sidecar rejected oversized request bodies but did not call `req.destroy()`, leaving the incoming stream open. A slow-drip client (1 byte/s on a 120 s timeout) could hold file descriptors and event-loop references for the full timeout period even though the handler already returned 400. Fix: `req.destroy()` is called immediately when the body exceeds the limit. One new Node test verifies the socket is destroyed within 1 s rather than lingering.

## [0.3.21] - 2026-04-22

### Fixed
- **P2-M1 (a2a-review-21)** — Outbound response bodies were read with unbounded `resp.read()` (Python) and string-concat pumps (Node) with no byte cap. A compromised peer, registry, or anything squatting on a probed plugin port could stream gigabytes of body — measured ≈3 GB/s at loopback — and OOM the sidecar / CLI / registry process before the 30 s request timeout fires. Fix: new `A2A_MAX_RESPONSE_BYTES = 4 MiB` cap with a `_read_capped(resp, cap)` helper applied at all five Python outbound sites (`clawcu.a2a.client._http_json`, `hermes.sidecar.lookup_peer`, `hermes.sidecar.fetch_peer_list`, `hermes.sidecar.forward_to_peer`, `clawcu.a2a.registry._fetch_card_at`) and a `readCappedBody(res, limit)` helper wired into Node's `postJson` + `httpRequestRaw` (covering `lookupPeer` / `forwardToPeer` / `fetchPeerList`). Overflow aborts the socket (`res.destroy()` on Node, `_ResponseTooLarge` on Python) so the attacker cannot buffer the full payload in memory before the error surfaces. Applies to both 2xx and HTTPError-body paths. Six new Python tests + three new Node tests exercise each call site against a stub server that streams 5 MiB; 4 MiB cap is asserted as an exported constant on both sides.

## [0.3.20] - 2026-04-22

### Fixed
- **P1-L1 (a2a-review-20)** — The iter-17 / iter-19 scheme allow-list (`_validate_outbound_url`) was bypassed by HTTP redirects. CPython 3.14's default `HTTPRedirectHandler.http_error_302` admits redirects into `{"http", "https", "ftp", ""}`, but `_validate_outbound_url` only gated the URL passed *into* `urlopen` — so a poisoned registry or peer endpoint that responded with `302 Location: ftp://attacker/` was silently followed, bypassing the allow-list and connecting the sidecar / CLI / registry to an attacker-chosen ftp:// URL (empirically reproduced against `ftp.gnu.org`). Fix: all five outbound urlopen sites (`clawcu.a2a.client._http_json`, `hermes.sidecar.lookup_peer`, `hermes.sidecar.fetch_peer_list`, `hermes.sidecar.forward_to_peer`, `clawcu.a2a.registry._fetch_card_at`) now use a module-level `_OPENER = build_opener(_NoRedirectHandler)` where `_NoRedirectHandler.redirect_request` returns `None`. 3xx responses surface as `HTTPError(30X)` through the existing error-handling arms; the redirect chain never reaches a second network call. Five new Python tests verify each call site with a stub server that 302s to `ftp://ftp.gnu.org/README` — the FTP URL is never touched.

## [0.3.19] - 2026-04-22

### Fixed
- **P2-K1 (a2a-review-19)** — Defense-in-depth parity between the sidecar and CLI after the iter-17/18 SSRF fix. `clawcu.a2a.client._http_json` (covering `post_message`, `lookup_agent`, `list_agents`) now runs a `_validate_outbound_url` pass before `urlopen`, rejecting any scheme outside `{http, https}`. Closes the registry-poisoning → confused-deputy variant where an attacker who can push to the registry (or tamper with a sidecar's `A2A_SELF_ENDPOINT` env) could direct the CLI to POST its sender/target/message JSON body to any stdlib-urllib-supported URL scheme (e.g. `file:///` or `ftp://attacker/`). The CLI now behaves identically to the sidecar under adversarial registry input. Helper + `_BadClientUrl` exception mirror the hermes sidecar copy byte-for-byte (no shared-lib refactor: the hermes sidecar is loaded via `spec_from_file_location` and cannot import from the `clawcu.*` package at runtime).

## [0.3.18] - 2026-04-22

### Fixed
- **P1-J1 (a2a-review-18)** — The openclaw Node sidecar had the same client-supplied `registry_url` override pattern on `/a2a/outbound` as the Hermes Python sidecar did before iter-17. Node's `parseHttpUrl` already enforced an http/https scheme allow-list so the `file://`/`ftp://`/`gopher://` smuggling vector was closed, but an attacker could still point the sidecar at any http(s) URL to either (a) probe internal services and read up to 200 chars of response via the `registry lookup …: <body>` error message, or (b) serve a malicious card that redirected `forwardToPeer` into a blind POST of the outbound body to an attacker-chosen http URL. Fixed by mirroring the iter-17 Python gate: a new `readAllowClientRegistryUrl` helper reads `A2A_ALLOW_CLIENT_REGISTRY_URL` (default off), and `/a2a/outbound` returns 400 `"client-supplied 'registry_url' is disabled by server policy"` whenever the body field is present without the operator opt-in. `hasOwnProperty` gating prevents the `{registry_url: null}` / `{registry_url: 42}` bypass that the old `typeof === "string"` check would have silent-ignored into using the default registry.

## [0.3.17] - 2026-04-22

### Fixed
- **P1-I1 (a2a-review-17)** — `/a2a/outbound` accepted a client-supplied `registry_url` field in the request body and forwarded it verbatim to `lookup_peer(…)`, enabling SSRF: an attacker able to reach the sidecar port could (1) probe arbitrary URLs reachable from the sidecar's network namespace and read up to 200 chars of the response via the `registry lookup …: <body>` error message (cloud-metadata services, internal admin APIs, neighbor containers, `host.docker.internal` loopback), or (2) serve a malicious card from their own HTTP server and coerce the sidecar into a blind POST of `{"from":<self>,"to":"x","message":<attacker>}` to whatever URL the card's `endpoint` field points at. Fixed in two layers: (a) the body-level `registry_url` override is now gated by `Config.allow_client_registry_url` (env `A2A_ALLOW_CLIENT_REGISTRY_URL`, default off) — requests with the field are rejected 400 unless the operator opts in; (b) a shared `_validate_outbound_url` helper enforces an http/https scheme allow-list on both `registry_url` (when allowed) and the card's `endpoint` field in `forward_to_peer`, rejecting `file://`, `ftp://`, `gopher://`, `dict://`, and other URL schemes at the sidecar boundary regardless of trust in the registry.

## [0.3.16] - 2026-04-22

### Fixed
- **P1-H1 (a2a-review-16)** — The Hermes sidecar's `ThreadingHTTPServer` had no socket-level timeout, so a slowloris-style request (valid `Content-Length` but no body, or an incomplete request line without the header terminator) would pin the worker thread indefinitely in `rfile.read(length)` / `BaseHTTPRequestHandler.readline`. An attacker able to reach the port (trivially on Linux, where the iter-12 default still binds `0.0.0.0`) can open N connections, stall each one, and exhaust `ThreadingHTTPServer`'s unbounded thread-per-request worker pool. The fix overrides `Handler.setup()` to call `self.request.settimeout(cfg.inbound_request_timeout_s)` (default 30 s, configurable via `A2A_INBOUND_REQUEST_TIMEOUT_S`, 0 disables) so every rfile read is bounded at the socket layer. The openclaw Node sidecar already has a 5-minute bound from Node's `http.server` default `requestTimeout`; this brings the Python sidecar into parity with a tighter default.

## [0.3.15] - 2026-04-22

### Fixed
- **P1-G1 (a2a-review-15)** — Hermes sidecar's Content-Length parser accepted hostile values. A negative Content-Length (`-1`) passed through `int()` unharmed and then wedged the worker thread in `self.rfile.read(-1)` waiting for an EOF that never comes on a keep-alive socket — each such request ties up a ThreadingHTTPServer thread indefinitely, trivially DoSing the sidecar from anywhere it's reachable. Non-numeric Content-Length raised an uncaught `ValueError` that dropped the connection without a proper 400. The new `_parse_content_length` helper rejects both shapes: negative length returns 400 with `negative Content-Length: …`, non-numeric returns 400 with `invalid Content-Length: …`, and the iter-14 oversized path still returns 413 with `request body exceeds …`. Applied to `/a2a/send`, `/a2a/outbound`, and `/mcp` (MCP keeps its JSON-RPC `-32700` error code). The openclaw Node sidecar is unaffected because its `readJsonBody` counts bytes from the data stream and ignores Content-Length entirely.

## [0.3.14] - 2026-04-22

### Fixed
- **P1-F1 (a2a-review-14)** — The Hermes Python sidecar read inbound request bodies with `self.rfile.read(Content-Length)` and no upper bound on `/a2a/send`, `/a2a/outbound`, and `/mcp`. An attacker able to reach the sidecar port (trivially on Linux, where the iter-12 P1-D1 default still binds `0.0.0.0`) could send `Content-Length: 10_000_000_000` with a matching stream and force the python process into OOM. The openclaw Node sidecar has enforced a 64 KiB cap via `readJsonBody` since iter 2. Hermes now checks `Content-Length` against `_max_body_bytes()` (default 64 KiB, override via `A2A_MAX_BODY_BYTES`) on all three POST handlers and returns `413 Payload Too Large` before any read — preserving the existing error shapes (plain `{"error": ...}` for `/a2a/send` + `/a2a/outbound`, JSON-RPC `MCP_ERR_PARSE` for `/mcp`).
- **P2-F1 (a2a-review-14)** — `localize_endpoint_for_host` in `a2a/client.py` substituted the CLI-visible host without bracket-wrapping IPv6 literals, so an operator setting `CLAWCU_A2A_HOST_HOSTNAME=::1` got `http://::1:9149/a2a/send` — a malformed URL that `urlsplit` misparses (the last colon is ambiguous between address and port). The replacement is now wrapped in brackets when it contains `:`, yielding `http://[::1]:9149/a2a/send`. IPv4 / hostname replacements are untouched (no colon → no brackets).

## [0.3.13] - 2026-04-22

### Fixed
- **P2-E1 (a2a-review-13)** — When the sidecar probe fails on a `status="running"` instance, the registry falls back to a placeholder card synthesized from `card_from_record(record, host=...)`. That `host` was the registry's own bind interface (typically `127.0.0.1` in the CLI path), so the placeholder endpoint was `http://127.0.0.1:<port>/a2a/send`. Peer *containers* can't reach that — loopback inside a peer container points to itself, not to the host's loopback. The iter-9 P1-A3 machinery already resolves a proper advertise host (`host.docker.internal` on Darwin/Windows, respects `CLAWCU_A2A_ADVERTISE_HOST`, per-record override via `a2a_advertise_host`); `_build_cards` now uses that host for the placeholder. Happy-path traffic (probe succeeds → sidecar's self-reported card) is unchanged. CLI traffic still works because iter-11 P1-C1's `localize_endpoint_for_host` rewrites `host.docker.internal` → `127.0.0.1` before POST.

## [0.3.12] - 2026-04-22

### Fixed
- **P1-D1 (a2a-review-12)** — A2A sidecar ports were bound to `0.0.0.0` on the host, so any machine on the LAN could POST `/a2a/send`, `/a2a/outbound`, or `/mcp` with no auth, drain the upstream LLM quota, or inject adversarial messages into a native agent's session. The sidecar has no authentication layer of its own — it trusts that only the clawcu host can reach it. `docker.run_container` now publishes additional_ports through `resolve_a2a_bind_interface()`: on Darwin it prepends `127.0.0.1:` so Docker Desktop's userland proxy still forwards `host.docker.internal` (container→container stays working) while LAN traffic hits a closed port. On Linux the default stays `0.0.0.0` because the docker bridge reaches the host via its gateway IP — loopback-only binding there would break container→container; operators can opt in via `CLAWCU_A2A_BIND_INTERFACE` once they've set up a firewall. The env var also accepts a specific LAN IP for multi-interface hosts.
- **P2-D2 (a2a-review-12)** — `/agents` on the registry federated `status="starting"` records (iter-10 P2-A4) even when the optimistic sidecar probe failed, so peers briefly discovered placeholder cards pointing at endpoints that 504'd under load. Registry now distinguishes by status: a probe miss on a `running` record still publishes a placeholder (the instance has passed ≥1 healthcheck, so the miss is almost certainly transient and `forward_to_peer`'s error handling is better than hiding the instance), but a probe miss on a `starting` record is skipped with an INFO log and picked up on the 5 s cache TTL once the sidecar binds.

## [0.3.11] - 2026-04-22

### Fixed
- **P1-B1 (a2a-review-11)** — Hermes's `/a2a/send` had no inbound per-peer rate limit, so any single peer could flood the sidecar and drain the upstream LLM quota. OpenClaw's sidecar has enforced a 30/min sliding-window limit (`ratelimit.js`) since iter 2. The Python hermes sidecar now implements the same behavior inline (`PeerRateLimiter` in `sidecar.py`): 30 requests per 60 s per `from` field, bucket eviction at 1024 peers, tunable via `A2A_RATE_LIMIT_PER_MINUTE` (set `0` to disable). On breach returns HTTP 429 with `Retry-After` and `resetMs` so peers can back off deterministically.
- **P1-C1 (a2a-review-11)** — `clawcu a2a send` from the host failed against any instance registered with a container-visible hostname. The 0.3.9 P1-A3 fix correctly resolved `A2A_SIDECAR_ADVERTISE_HOST=host.docker.internal` on Darwin so container→container peer calls work, but `host.docker.internal` is a docker-only name that doesn't resolve from the host loopback — and that's the exact path the CLI takes when it calls `send_via_registry`. `client.py` now rewrites `host.docker.internal` / `gateway.docker.internal` to `127.0.0.1` (override via `CLAWCU_A2A_HOST_HOSTNAME`) for the CLI path only; the registry's federated view still advertises the container-visible endpoint so in-mesh traffic is unaffected.
- **P2-C2 (a2a-review-11)** — `post_message`'s failure message rendered as `"send failed (502): None"` when an upstream returned a non-dict or empty body, which was useless to a CLI operator. It now includes the full endpoint URL and a parsed hint derived from the body (preferring the `error` / `detail` / `message` field when present, `"empty body"` for an empty 5xx, a truncated repr otherwise).

## [0.3.10] - 2026-04-22

### Fixed
- **P0-A4 (a2a-review-10)** — The 0.3.9 P0-A3 fix copied `bootstrap.py` into the hermes plugin image, but the hermes base image ships `python3` without `pip` and without `PyYAML`, so `bootstrap.py` still tripped its PyYAML-unavailable guard and skipped MCP auto-wire (`WARNING a2a-sidecar: PyYAML unavailable (No module named 'yaml') — cannot handle YAML MCP config`). Hermes Dockerfile now `apt-get install -y --no-install-recommends python3-yaml` (with `rm -rf /var/lib/apt/lists/*`). Using the Debian system package avoids pulling in pip just for one dependency. OpenClaw is unaffected — its bootstrap.js has no external deps.
- **P2-A4 (a2a-review-10)** — Registry federation (`_build_cards` in `a2a/registry.py`) filtered records with `running_only=True`, which maps to `status == "running"`. But `container_status` collapses docker healthcheck phase `starting` → `status="starting"`, so a freshly-started instance was invisible to the registry for up to a full healthcheck interval (180s on stock openclaw). The A2A sidecar binds independently of gateway readiness, so we now consider both `running` and `starting` records federatable and probe them optimistically.

## [0.3.9] - 2026-04-22

### Fixed
- **P0-A3 (a2a-review-9)** — Hermes plugin Dockerfile was shipping only `sidecar.py` into the baked image, leaving the runtime-loaded `bootstrap.py` (MCP auto-wire) and `outbound_limit.py` (shared outbound rate-limit) missing from `/opt/a2a/`. The sidecar tolerated the absence via `spec_from_file_location` + try/except so the instance came up, but Hermes-backed agents silently (a) never got the `a2a_call_peer` MCP tool injected into `config.yaml` and (b) ran with outbound throttling disabled. Dockerfile now `COPY`s both helpers alongside `sidecar.py`. OpenClaw was unaffected (it already `COPY`s the whole `sidecar/` dir).
- **P1-A3 (a2a-review-9)** — `A2A_SIDECAR_ADVERTISE_HOST` (openclaw) / `A2A_ADVERTISE_HOST` (hermes) was hardcoded to `127.0.0.1` at instance-create time. On macOS Docker Desktop that broke cross-container peer calls: the registry handed out a `127.0.0.1:<port>` endpoint which resolves to the caller's own loopback inside another container, so MCP `tools/call a2a_call_peer` (and `/a2a/outbound` by extension) failed with `ECONNREFUSED` even when the peer sidecar was reachable via `host.docker.internal`. Both adapters now call `clawcu.a2a.sidecar_plugin.resolve_advertise_host(record)`, which picks `host.docker.internal` on Darwin/Windows and `127.0.0.1` on Linux, respects `$CLAWCU_A2A_ADVERTISE_HOST` as a site-wide override, and honors a new per-record override set at create time.
- **P2-A3 (a2a-review-9)** — `InstanceRecord.from_dict` and `ProviderRecord.from_dict` passed raw JSON keys straight to the dataclass constructor, so any clawcu reading a state file written by a newer clawcu blew up with `TypeError: __init__() got an unexpected keyword argument …`. Both constructors now project the payload onto the declared fields so unknown keys are silently ignored, making the schema forward-compat for future additions (including `a2a_advertise_host` from P1-A3).

### Added
- `clawcu create [service] --a2a-advertise-host HOST` — explicit per-instance override for the hostname peers should use to reach this sidecar. Composes with the Darwin/Windows auto-detect and `$CLAWCU_A2A_ADVERTISE_HOST`. Required shape once multi-host or named-docker-network deployments land; for single-host Docker Desktop the auto-detect is enough and the flag is optional.
- New `clawcu.a2a.sidecar_plugin.resolve_advertise_host` / `default_advertise_host` helpers (used by both adapters) so the resolution rule lives in one place alongside the other plugin-build helpers (`plugin_source_sha`, `plugin_fingerprint`).
- `InstanceSpec.a2a_advertise_host: str | None = None` field — persisted to the on-disk record so `clawcu recreate` keeps using the same advertise host even if the host environment changes.

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

## [0.2.12] - 2026-04-22

### Added
- `clawcu list --no-cache` now forces a fresh Available Versions registry refresh for the footer while still updating the on-disk daily cache after a successful fetch.

### Fixed
- `clawcu list` now tolerates legacy instance-record fields such as `a2a_enabled` instead of failing to deserialize older managed-instance JSON files.

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
[0.3.0]: https://github.com/heguangzhu/clawcu/compare/v0.2.12...v0.3.0
[0.2.12]: https://github.com/heguangzhu/clawcu/compare/v0.2.11...v0.2.12
[0.2.11]: https://github.com/heguangzhu/clawcu/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/heguangzhu/clawcu/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/heguangzhu/clawcu/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/heguangzhu/clawcu/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/heguangzhu/clawcu/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/heguangzhu/clawcu/compare/v0.2.0...v0.2.6
[0.2.0]: https://github.com/heguangzhu/clawcu/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/heguangzhu/clawcu/releases/tag/v0.1.0
