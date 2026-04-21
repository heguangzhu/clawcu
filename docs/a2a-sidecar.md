# A2A Sidecar Guide

🌐 Language:
[English](a2a-sidecar.md) | [中文](a2a-sidecar.zh-CN.md)

> This guide covers ClawCU's A2A sidecar: what it is, why it's a separate process, how to turn it on, and how to operate it. For command-line surface details, see [USAGE_latest.md](../release/USAGE_latest.md) §11. For release context, see [RELEASE_v0.3.0.md](../release/RELEASE_v0.3.0.md).

* * *
## TL;DR

- `clawcu create openclaw|hermes --a2a ...` bakes an **A2A v0 sidecar** into the instance's image.
- The sidecar runs as a second process next to the native gateway and publishes two endpoints on a neighbor port: `GET /.well-known/agent-card.json` (discovery) and `POST /a2a/send` (messaging).
- Stock instances (no `--a2a`) are unchanged. A2A is strictly opt-in and additive.
- `clawcu a2a up` one-shots the whole setup: probe running instances, bridge the ones without sidecars, serve an aggregate registry.
- `clawcu a2a send --to <name> --message "..."` is the smoke test.

* * *
## Table of Contents

- [What the sidecar is](#what-the-sidecar-is)
- [Why a sidecar, not a gateway plugin](#why-a-sidecar-not-a-gateway-plugin)
- [Architecture at a glance](#architecture-at-a-glance)
- [Opt-in: baking a sidecar into an instance](#opt-in-baking-a-sidecar-into-an-instance)
- [Protocol surface (v0)](#protocol-surface-v0)
- [Optional: thread_id for multi-turn context](#optional-thread_id-for-multi-turn-context)
- [Operational internals](#operational-internals)
- [Image lifecycle and the source-sha fingerprint](#image-lifecycle-and-the-source-sha-fingerprint)
- [Two-instance walkthrough](#two-instance-walkthrough)
- [Enabling A2A on an existing instance](#enabling-a2a-on-an-existing-instance)
- [`a2a up` vs `registry serve` vs `bridge serve`](#a2a-up-vs-registry-serve-vs-bridge-serve)
- [Troubleshooting](#troubleshooting)
- [Current limits](#current-limits)
- [FAQ](#faq)

* * *
## What the sidecar is

A **sidecar** here means: a second process that ships inside the same container image as the native service, bound to a **different port**, speaking a different protocol. It is not a plugin loaded into the service; it is not a reverse proxy in front of the service; it is not a separate container.

For ClawCU, the sidecar exists to expose **A2A v0** — a tiny agent-to-agent messaging protocol — on top of any managed instance, without asking the service author to understand A2A or cooperate with it.

What the sidecar does:

1. **Publishes an AgentCard** at `GET /.well-known/agent-card.json` so peers can discover what this agent is and where to send messages.
2. **Accepts A2A messages** at `POST /a2a/send`, translates them into the native service's chat/completion API, and returns the reply.
3. **Stays out of the way**: the native gateway keeps serving its normal traffic on its normal port. Existing users of the instance see nothing new.

What the sidecar is **not**:

- It is not a general API gateway. The only POST it accepts is `/a2a/send`.
- It is not a platform for streaming, multi-recipient fan-out, auth negotiation, or RPC. v0 is one message in, one message out.
- It is not "optional startup code" inside the native service — if the native service is fine, the sidecar starts; if the native service dies, the sidecar reports that via `/healthz`.

* * *
## Why a sidecar, not a gateway plugin

Both OpenClaw and Hermes have their own plugin systems. The obvious move would be to ship A2A as a first-class plugin inside each service. ClawCU deliberately doesn't, for three reasons:

**1. ClawCU targets users, not service authors.** Asking a user to install a plugin in a specific location, wire it into the service config, and upgrade it in lockstep with the service is a lot of ceremony for "I want these agents to talk to each other." A sidecar is just a port next to an existing port. The service internals are untouched.

**2. Version independence.** The sidecar speaks A2A v0. It does not speak OpenClaw-internal or Hermes-internal APIs. An OpenClaw upgrade does **not** force an A2A rebake unless the *sidecar* source itself changed. An A2A protocol bump does **not** force an OpenClaw or Hermes upgrade.

**3. Service immutability.** A bake-time Dockerfile layer is auditable and reproducible from the clawcu source tree alone. There is no runtime install, no first-boot `pip install` inside the container, no "did the plugin load this time?" ambiguity. What `docker image inspect` shows is what actually runs.

The cost: a second port. On a single-host dev setup this is essentially free — the sidecar binds 127.0.0.1 by default, and `clawcu create --a2a` surfaces port conflicts at **create time**, not at first chat.

* * *
## Architecture at a glance

```
┌──────────────────────── managed container ────────────────────────┐
│                                                                   │
│   ┌────────────────────┐        ┌────────────────────────────┐    │
│   │ native gateway     │        │ A2A sidecar                │    │
│   │   (OpenClaw /      │◀────── │  stdlib-only server        │    │
│   │    Hermes)         │  LLM   │  port 18790 / 9119         │    │
│   │   port 18789/8642  │  call  │  ┌──────────────────────┐  │    │
│   └────────────────────┘        │  │ GET                  │  │    │
│          ▲                      │  │   /.well-known/      │  │    │
│          │                      │  │   agent-card.json    │  │    │
│          │                      │  ├──────────────────────┤  │    │
│   (existing users)              │  │ POST /a2a/send       │  │    │
│                                 │  ├──────────────────────┤  │    │
│                                 │  │ GET /healthz         │  │    │
│                                 │  └──────────────────────┘  │    │
│                                 │  per-peer rate limit       │    │
│                                 │  log tee → a2a-sidecar.log │    │
│                                 │  thread store (optional)   │    │
│                                 └────────────────────────────┘    │
│                                                                   │
└──────────────────────┬────────────────────┬───────────────────────┘
                       │ 18819 (gateway)    │ 18820 (A2A)
                       ▼                    ▼
                    host network (127.0.0.1 by default)
```

**Key point**: the gateway path and the A2A path are independent in the container. When an A2A peer sends a message, the sidecar dials the gateway's `POST /v1/chat/completions` on `127.0.0.1:<internal>` — that's localhost inside the container, no extra network hop.

**Per-service defaults**:

| Service | Gateway port (internal) | Sidecar port (internal) | Readiness path |
|---|---|---|---|
| OpenClaw | 18789 | 18790 | `/healthz` |
| Hermes | 8642 | 9119 | `/health` |

ClawCU publishes both to the host. The host-side A2A port is whatever ClawCU picked (visible in `clawcu inspect <name>` access info) — the internal defaults above are rarely user-facing.

* * *
## Opt-in: baking a sidecar into an instance

At `clawcu create` time, `--a2a` flips the image selection:

```bash
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a
```

What happens:

1. ClawCU computes a **plugin fingerprint** `<clawcu_version>.<sha10>`, where `sha10` is a SHA-256 over the on-disk sidecar sources for that service.
2. It checks for a local image tagged `clawcu/{service}-a2a:{base}-plugin{fingerprint}`.
3. If missing, it bakes one: `FROM {base-image} + COPY sidecar + COPY entrypoint.sh + ENTRYPOINT supervisor`.
4. The instance starts from the baked image. Both native gateway and sidecar are spawned by the supervisor entrypoint and run under the same PID 1.
5. `.clawcu-instance.json` in the datadir records `a2a_enabled: true` so `recreate` / `inspect` know this is an A2A instance.

That's it. There's no post-create step.

To verify:

```bash
curl -s http://127.0.0.1:<a2a_port>/.well-known/agent-card.json | jq .
# {
#   "name": "writer",
#   "role": "OpenClaw-backed assistant",
#   "skills": ["chat", "a2a.bridge"],
#   "endpoint": "http://127.0.0.1:18820/a2a/send"
# }
```

* * *
## Protocol surface (v0)

### `GET /.well-known/agent-card.json`

Returns a JSON object. Schema:

```json
{
  "name": "writer",
  "role": "OpenClaw-backed assistant",
  "skills": ["chat", "a2a.bridge"],
  "endpoint": "http://127.0.0.1:18820/a2a/send"
}
```

- `name` — agent identity. Defaults to the instance name.
- `role` — human-readable purpose string.
- `skills` — free-form tag list. No enforcement in v0; used by callers to decide whether to route a message here.
- `endpoint` — the fully-qualified URL a peer should POST to. Note this is the **advertised** URL; it may differ from the bind host/port when there's a reverse proxy between host and peer.

### `POST /a2a/send`

Request:

```json
{
  "from": "analyst",
  "to": "writer",
  "message": "summarize yesterday's standup",
  "thread_id": "0192a3b4-..."         // optional — see next section
}
```

Response:

```json
{
  "from": "writer",
  "message": "Yesterday's standup focused on...",
  "thread_id": "0192a3b4-..."         // echoed only if the request carried one
}
```

Error responses are `{"error": "..."}` with appropriate HTTP status (400 for bad input, 429 for rate-limited, 503 if the gateway hasn't become ready yet).

### `GET /healthz`

Returns plain JSON with `status`, `gateway_ready`, `plugin_version`. Used by `clawcu a2a up`'s probe loop; also useful for your own liveness checks.

```json
{
  "status": "ok",
  "gateway_ready": true,
  "plugin_version": "0.3.0.d7226c2b58"
}
```

The `plugin_version` here is the same fingerprint stamped into the image tag — **if you're seeing unexpected behaviour, compare this value with what you expect from the installed clawcu version.** Mismatches mean an old image is still running.

* * *
## Optional: thread_id for multi-turn context

A v0 `POST /a2a/send` is stateless by default. If you want a conversation to accumulate across calls, pass a **`thread_id`** (uuid v7) in the request. On each call with the same `thread_id`, the sidecar:

1. Appends `{peer, message, timestamp}` to `<datadir>/threads/<peer>.jsonl`.
2. On the next turn with the same `thread_id`, prepends the prior messages as context before dialing the native gateway.

Storage format is JSONL (one message per line, append-only). One file per peer, so `writer`'s conversation with `analyst` and its conversation with `planner` are independent threads, even if they share a `thread_id` namespace on the caller side.

Security: `thread_id` is enforced to be a valid uuid v7. No `..`, no `/`, no path-traversal via the thread identifier. Peers that forget to pass one are fine — they just get stateless single-shot behaviour.

* * *
## Operational internals

These are baked into the sidecar; there are no user-tuneable flags today. Called out here so you know what to expect:

- **Per-peer rate limit** — a token-bucket keyed on the `from` field. Default 30 messages/minute/peer. One chatty peer cannot starve the native gateway for others. Over-limit returns `429`.
- **Readiness probe** — on container start the sidecar polls the native gateway at `/healthz` (OpenClaw) or `/health` (Hermes) with backoff. `/healthz` on the sidecar only returns `"ok"` once the backend replied at least once. Prevents "sidecar alive, gateway not yet alive" races.
- **Log tee** — everything the sidecar writes to stdout/stderr is also written to `<datadir>/a2a-sidecar.log`. You don't need `docker logs` to debug A2A issues; `tail -f ~/.clawcu/<instance>/a2a-sidecar.log` works.
- **Optional thread store** — see above. Path-traversal hardened.

Nothing here is configurable via CLI today. The relevant knobs live as env vars inside the container (e.g. `A2A_RATE_LIMIT_PER_MINUTE`, `A2A_BIND_PORT`); override via `clawcu setenv <instance> ...` + `clawcu restart <instance>` if you need to experiment.

* * *
## Image lifecycle and the source-sha fingerprint

The baked image tag is:

```
clawcu/{service}-a2a:{base_version}-plugin{clawcu_version}.{sha10}
```

Example: `clawcu/openclaw-a2a:2026.4.12-plugin0.3.0.d7226c2b58`.

The **`sha10`** is a 10-char prefix of SHA-256 computed over every file under `src/clawcu/a2a/sidecar_plugin/<service>/` on disk. Excluded: `__pycache__`, `.pyc`, `.pyo`, `__init__.py` (the last is packaging metadata, not runtime code).

Why include a sha at all? Two reasons:

1. **Editable dev installs.** If you `pip install -e .` the clawcu source and edit `sidecar/server.js`, the clawcu *version* hasn't changed, but the sidecar sources have. Without a fingerprint, `A2AImageBuilder` would happily re-use the stale cached image. You'd spend an hour debugging a ghost.
2. **Auditability.** `clawcu/openclaw-a2a:...plugin0.3.0.abc123` vs `plugin0.3.0.def456` is a visible signal that the baked sidecar differs. `docker image inspect` is enough to tell which build you're on.

When a rebake is triggered:

- Any file under `sidecar_plugin/<service>/` changes (Dockerfile, entrypoint, *.js, *.py).
- The clawcu package version changes.
- The base image version (OpenClaw / Hermes upstream) changes.

When rebakes do **not** happen:

- `__pycache__` / `.pyc` changes (pytest, imports).
- `__init__.py` edits (packaging only).
- Changes to Python code outside `sidecar_plugin/<service>/`.

If you ever need to force a rebake for debugging, delete the tag: `docker image rm clawcu/openclaw-a2a:...` and re-run `clawcu create --a2a` on a clone.

* * *
## Two-instance walkthrough

The canonical smoke test: two A2A instances talking via the registry.

```bash
# 1. Create two A2A-enabled instances (bakes images on first run).
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a

# 2. Start the A2A topology (registry + any missing bridges, foreground).
clawcu a2a up
# [green]OK[/green] writer  (plugin-backed on :18820)
# [green]OK[/green] analyst (plugin-backed on :9129)
# [bold]A2A registry[/bold] listening on http://127.0.0.1:8765 (Ctrl+C to stop)

# 3. From another terminal: send.
clawcu a2a send --to analyst --message "summarize yesterday"
# {
#   "from": "analyst",
#   "message": "Yesterday's discussion covered..."
# }
```

Direct-to-sidecar (skipping the registry) is also fine — it's just an HTTP POST:

```bash
curl -s -X POST http://127.0.0.1:9129/a2a/send \
     -H 'content-type: application/json' \
     -d '{"from":"writer","to":"analyst","message":"hi"}' | jq .
```

* * *
## Enabling A2A on an existing instance

There is **no in-place upgrade** from a stock instance to an A2A instance today. The contract is: `--a2a` at create time. For an existing instance, use the clone-first workflow:

```bash
clawcu clone writer --name writer-a2a
clawcu remove writer-a2a                     # remove the clone's container
clawcu create openclaw --name writer-a2a \
       --version 2026.4.12 --a2a             # recreate from the cloned datadir
```

The datadir (models, history, env) is preserved across the clone + create. The image tag changes from stock to A2A-baked.

Why no in-place path yet? The image change is a rebuild, not a mutation, and we don't want a flag on `upgrade` that silently re-bakes. Explicit is better than clever. If this becomes a pain point in practice, it can be added later as a dedicated `clawcu enable-a2a <name>` verb.

* * *
## `a2a up` vs `registry serve` vs `bridge serve`

Three related commands; pick the one that matches your situation:

- **`clawcu a2a up`** — the common case. Probes every running managed instance, starts echo bridges for instances without a sidecar, serves the aggregate registry in the foreground. One command.
- **`clawcu a2a registry serve`** — just the registry, no probing or bridging. Use when every instance already has a sidecar baked and you don't need the auto-bridge fallback.
- **`clawcu a2a bridge serve --instance <name>`** — just a bridge for one instance, no registry. Demo / offline / CI-testing. If a real sidecar is already serving on the instance's port, the bridge won't be needed; it exists so an un-baked instance can still show up on the A2A surface for a demo.

A mental model:

- **Sidecar** = what runs inside the container. Baked in once, permanent.
- **Bridge** = out-of-container stand-in for an instance without a sidecar. Per-instance, short-lived.
- **Registry** = aggregator, cross-instance. Tells callers "here are the cards of everyone on this host."

* * *
## Troubleshooting

**`clawcu a2a send` returns 503 "gateway not ready".**
The sidecar came up before the native gateway did, and is still waiting for the backend's `/healthz` / `/health`. Wait 10–30 seconds and retry. If it persists, `clawcu logs <instance>` to see why the native gateway is stuck.

**`clawcu a2a send` returns 429.**
Per-peer rate limit. Default 30/minute/peer. Space out calls or override `A2A_RATE_LIMIT_PER_MINUTE` via `clawcu setenv <instance> A2A_RATE_LIMIT_PER_MINUTE=120` → `clawcu restart <instance>`.

**Peer returns an OpenClaw / Hermes error inside a 200 A2A reply.**
The sidecar forwards whatever the native gateway returned. Check `clawcu logs <instance>` for the underlying provider error (auth, model, quota).

**`curl :<port>/.well-known/agent-card.json` works, but `POST /a2a/send` hangs.**
Usually a model-provider timeout. `--timeout 120` on `clawcu a2a send` gives more headroom; long LLM calls can exceed the 60-second default.

**Baked image tag doesn't match what I expect.**
Check `/healthz` on the sidecar — its `plugin_version` field is authoritative. If it disagrees with `clawcu --version`, you're running a stale image; `docker image ls clawcu/*-a2a` to find the culprit, `docker image rm` it, and recreate.

**Changes to `sidecar/server.js` didn't take effect.**
You're on an editable install and `A2AImageBuilder` sees the new sha but the old image is still cached? `clawcu inspect <instance>` shows the current image tag. If it matches the new fingerprint but behaviour is old, the container wasn't restarted — `clawcu restart <instance>`.

**Port conflict on create.**
`clawcu create --a2a` probes the A2A port at create time. If the chosen port is taken, create fails with a clear error. Pick a different `--port` or free the port on the host.

**Logs: where do I look?**
- `clawcu logs <instance>` — native gateway logs (docker logs under the hood).
- `tail -f ~/.clawcu/<instance>/a2a-sidecar.log` — sidecar-specific log (the tee, mentioned above).

* * *
## Current limits

- **Protocol version: v0.** The contract may extend (streaming, auth, multi-recipient, richer error taxonomy) before `v1`. Pin your client to the v0 request/response shape and expect backward-compatible additions, not breaking changes, over the `0.3.x` line.
- **No built-in auth.** `/a2a/send` accepts any request from any peer that can reach the port. The sidecar binds 127.0.0.1 by default, so on a single-host setup this is fine. For multi-host, put it behind a reverse proxy that does auth, or wait for the protocol extension.
- **Local-only registry.** The registry aggregates cards for **this host's** managed instances. Cross-host federation isn't in v0.
- **Stock / A2A is a hard switch at create time.** No in-place enable; use clone-first.
- **Sidecar has no streaming.** v0 is request/response. If the native service streams, the sidecar waits for the full reply and returns it as one JSON.

* * *
## FAQ

**Do stock instances cost anything when I'm not using `--a2a`?**
No. The sidecar only runs in A2A-baked images. Non-A2A instances use the stock image tag, byte-identical to what `v0.2.x` shipped.

**Can I disable the sidecar at runtime without recreating?**
Not cleanly. The supervisor spawns the sidecar at container start. You can `docker exec <container> kill $(pgrep -f sidecar)` to kill the sidecar process, but that's a hack — next restart it comes back.

**What's the overhead?**
One long-lived stdlib-only HTTP process. Memory: <30 MB idle for both services. CPU: zero at rest; per-request cost is dominated by the downstream LLM call.

**Does A2A work with `clawcu exec` / `clawcu tui`?**
Yes. The sidecar runs alongside, so `exec` / `tui` / `token` / `config` behave exactly as on a non-A2A instance.

**Can I run two sidecars on the same instance (e.g. A2A v0 and v1 side-by-side)?**
Not supported today. One sidecar per instance, one port. Future protocol versions are expected to be additive on the same port.

**Where does the `plugin_version` in `/healthz` come from?**
It's the full `<clawcu_version>.<sha10>` fingerprint stamped into the image at bake time via the `CLAWCU_PLUGIN_VERSION` build-arg. Same value that's in the image tag.

**How do I upgrade the sidecar code independently of the service?**
`pip install --upgrade clawcu`, then `clawcu clone <name> --name <name>-new` + `clawcu create ... --a2a --version <same-service-version>`. The `service` base doesn't move; only the sidecar layer does, because the fingerprint changed.

* * *

See also:

- [USAGE_latest.md](../release/USAGE_latest.md) — `clawcu a2a` command reference
- [RELEASE_v0.3.0.md](../release/RELEASE_v0.3.0.md) — why A2A, compat notes, roadmap
- [CHANGELOG.md](../CHANGELOG.md) — full version history
