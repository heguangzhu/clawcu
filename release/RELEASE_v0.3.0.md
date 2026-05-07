# ClawCU v0.3.0

🌐 Language:
[English](RELEASE_v0.3.0.md) | [中文](RELEASE_v0.3.0.zh-CN.md)

Release Date: April 22, 2026

> `v0.3.0` brings **agent-to-agent messaging** to ClawCU. A single opt-in flag at create time — `--a2a` — bakes an A2A v0 sidecar into the managed service image, so any instance exposes `GET /.well-known/agent-card.json` + `POST /a2a/send` on a neighbor port alongside its native gateway. Stock service behaviour is unchanged; A2A is strictly additive.

* * *
## Highlights

- **Opt-in `--a2a` at instance creation**
  - `clawcu create openclaw --name ... --a2a` or `clawcu create hermes --name ... --a2a` bakes the A2A sidecar into a derived image.
  - Stock instances (no `--a2a`) look exactly the same as `v0.2.x`. The sidecar layer is invisible until you ask for it.
  - Both services: OpenClaw ships a Node sidecar (stdlib `node:http`); Hermes ships a Python sidecar (stdlib `http.server`). No new runtime dependencies.

- **Neighbor-port protocol, not a gateway hijack**
  - The sidecar binds a second port next to the native gateway. OpenClaw gateway 18789 stays at 18789; A2A lives at 18790. Hermes gateway stays on its configured API port; A2A is on `A2A_BIND_PORT` (default 9119).
  - `/.well-known/agent-card.json` serves the self-describing AgentCard.
  - `POST /a2a/send` accepts `{"from": "...", "to": "...", "message": "..."}` (plus optional `thread_id`), bridges to the native LLM backend, and returns `{"from": "...", "message": "..."}`.

- **Source-sha-pinned image tags**
  - Image tag: `clawcu/{service}-a2a:{base}-plugin{clawcu_version}.{sha}`, where `sha` is a SHA-256 over the on-disk sidecar sources (Dockerfile, entrypoint, *.js / *.py).
  - Editing the sidecar in an editable dev install (`pip install -e .`) changes the sha, so `A2AImageBuilder` transparently bakes a fresh tag. No stale images served from cache across code changes.

- **Sidecar hardening**
  - Per-peer rate limit (token bucket keyed on the `from` field) so one chatty peer can't starve the gateway.
  - Readiness probe: the sidecar polls the native gateway at startup and only flips `/healthz` to `ok` once the backend responds.
  - Log tee: sidecar stdout/stderr is mirrored to `<datadir>/a2a-sidecar.log` for post-hoc inspection without `docker logs`.
  - Optional `thread_id`: per-peer JSONL conversation history under `<datadir>/threads/`, path-traversal hardened (uuid v7 enforced, no `..` / `/`).

- **`clawcu hermes identity set <name> <path>`**
  - Installs a user-authored `SOUL.md` into a Hermes instance's datadir so `prompt_builder.load_soul_md` picks up the new persona on the next chat turn — no restart, no recreate.

- **Packaging fix for the wheel**
  - `v0.3.0` ships the sidecar assets (`Dockerfile`, `entrypoint.sh`, `*.js`, Hermes `sidecar.py`) as package-data. `pip install clawcu==0.3.0` → `clawcu create --a2a` now works out of the box from PyPI; no source checkout required.

- **479 tests** (450 pytest + 29 Node sidecar tests, up from 366 at `v0.2.10`), covering image fingerprint stability, card derivation, rate-limit bucket, readiness probe, thread store, and the Node / Python sidecar surfaces end-to-end.

* * *
## Why A2A

As soon as you run more than one agent on one machine, you want them to talk to each other. The naive path — "just point agent A at agent B's API" — couples every pair of agents to each other's auth schemes, request formats, streaming quirks, and whatever else. It does not scale beyond two.

A2A v0 is the smallest possible contract that lets N agents discover and message each other:

- **AgentCard** (`GET /.well-known/agent-card.json`) — `{name, role, skills, endpoint}`. One JSON file at a well-known URL.
- **send** (`POST /a2a/send`) — one message in, one reply out.

That's it. No streaming, no transport negotiation, no capability handshake. v0 is deliberately thin because the interop value is in convergence, not features.

ClawCU's job is to make this work for *any* managed instance without asking the service owner to understand A2A. You flip `--a2a`, ClawCU bakes the sidecar, and the instance is reachable. The service itself keeps running exactly as before.

* * *
## Why a sidecar, not a gateway plugin

Both OpenClaw and Hermes already have their own plugin systems. The obvious move would have been to ship A2A as a first-class plugin inside each service. We didn't, for three reasons.

1. **ClawCU targets users, not service authors.** Asking a user to install a plugin in a specific location, wire it up in the service config, and upgrade it in lockstep with the service is a lot of ceremony for "I want these agents to talk". A sidecar is just a port next to an existing port. You don't configure anything inside the service.

2. **Version independence.** The sidecar speaks A2A v0, not OpenClaw-internal or Hermes-internal APIs. An OpenClaw upgrade does not force an A2A rebake unless the *sidecar* sources changed. An A2A protocol bump does not force an OpenClaw upgrade. The two axes are genuinely orthogonal.

3. **Service immutability.** A bake-time Dockerfile layer is auditable and rebuildable from the clawcu source tree alone. There is no runtime install step, no `pip install` inside the container at first boot, no "did the plugin load?" ambiguity. What you see in `docker image inspect` is what's running.

The tradeoff: a second port. On a single-host setup this is free — the sidecar binds to 127.0.0.1 by default. In the rare case where a port is already bound, `clawcu create --a2a` surfaces the conflict at create time, not at first chat.

* * *
## Why opt-in

`--a2a` is off by default. No stock instance behaviour changes when you upgrade from `v0.2.10` to `v0.3.0`. This matters because:

- The sidecar is a second process. On a machine with ten instances, ten extra sidecars is not free.
- Some users do not want any ports exposed beyond their gateway. "We baked an A2A port for you" would be a surprise, and surprises in lifecycle tools are bad.
- The protocol is v0. Baking it into every instance would be an implicit promise that v0 is stable. It is not — the contract may extend (streaming, auth, multi-recipient) before `v1`. Opt-in keeps the early adopters and leaves the cautious alone.

To turn A2A on for an existing instance, `clawcu clone <name> --name <name>-a2a` and `clawcu create ... --a2a` the clone. No in-place upgrade path today; the clone-first workflow is there for exactly this kind of try-it-and-back-out change.

* * *
## Why source-sha fingerprints

The image tag for the baked variant is `clawcu/{service}-a2a:{base}-plugin{clawcu_version}.{sha10}`. The 10-char sha is computed over every file under the service's `sidecar_plugin/<service>/` subtree (excluding `__pycache__`, `.pyc`, `__init__.py` — packaging metadata).

- In a released install (`pip install clawcu==0.3.0`), the sha is fixed. Every machine with that install bakes the same tag.
- In an editable dev install (`pip install -e .`), editing `sidecar/server.js` changes the sha. The next `clawcu create --a2a` bakes a fresh tag; the old tag stays on disk but is no longer referenced.

Without this, an editable install would happily reuse a stale image after you edited the sidecar — and you'd debug ghost behaviour for an hour before noticing. The fingerprint closes that loophole.

* * *
## Compatibility

`v0.3.0` is a drop-in upgrade from `v0.2.10` for every instance that does not use `--a2a`.

- Existing managed instances created on `v0.2.x` keep running with the same image tag, same ports, same env.
- `clawcu list` / `inspect` / `upgrade` / `rollback` / `clone` / `provider` etc. are unchanged on the surface.
- `InstanceSpec` / `InstanceRecord` gain a boolean `a2a_enabled` field (default `False`), additive — old records load fine via `from_dict` defaults.
- `.clawcu-instance.json` now also carries `a2a_enabled`; pre-`v0.3.0` sidecars missing the field are treated as `False` on `recreate`.
- `list --json` payload gains an `a2a_enabled` key on each instance. Additive; existing consumers are unaffected.

Breaking nothing, adding one flag, one field, one subcommand tree.

* * *
## Recommended Workflow

For users with existing instances:

- **Try A2A on a clone first.** `clawcu clone writer --name writer-a2a` → `clawcu remove writer-a2a` → `clawcu create openclaw --name writer-a2a --version <v> --a2a`. Baking happens once; subsequent starts are just `docker start`.
- **Multi-agent setup.** `clawcu a2a up` probes every running instance for a plugin-served AgentCard, starts echo bridges for the ones without, and serves the aggregate registry in the foreground. One command.
- **Send a message.** `clawcu a2a send --to analyst --message "summarize yesterday"` routes via the registry.

For everyone else:

- `v0.3.0` costs you nothing unless you pass `--a2a`.

* * *
## Closing Note

`v0.2.x` shipped a solid single-agent lifecycle — pull, create, upgrade, rollback, clone, snapshots, orphan recovery. `v0.3.0` is the first step out of single-agent: the minimum protocol surface for N agents on one machine to find and message each other, baked in as a sidecar so existing service internals stay untouched.

Next up for `v0.3.x`: the unified `--output {table|json|yaml}` protocol carried over from the earlier roadmap, provider bundle provenance via `.clawcu-instance.json` metadata, and promoting active-provider to a first-class field. A2A v0 is expected to grow streaming and auth in a `v0.4.x`.
