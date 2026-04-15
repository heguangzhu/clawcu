# ClawCU v0.2.0

🌐 Language:
[English](RELEASE_v0.2.0.md) | [中文](RELEASE_v0.2.0.zh-CN.md)

Release Date: April 15, 2026

> `v0.2.0` is the first ClawCU release that treats local agent runtimes as a multi-service problem instead of an OpenClaw-only workflow. The focus of this release is architectural clarity: a shared lifecycle core, explicit service adapters, and a second first-class runtime in Hermes.

* * *
## Highlights

- Multi-Agent Lifecycle Core
  - ClawCU now has a shared lifecycle core for Docker orchestration, records, snapshots, env handling, logs, and CLI dispatch.
  - Service-specific behavior is now isolated behind adapters instead of being mixed into one OpenClaw-heavy service layer.

- Two First-Class Managed Services
  - `openclaw`
  - `hermes`
  - `clawcu create`, `pull`, `list`, `inspect`, `clone`, `upgrade`, `rollback`, `exec`, `config`, and `tui` are now service-aware.

- Hermes Support
  - ClawCU can now fetch Hermes from the official repository at a requested git ref and build a managed Docker image.
  - Hermes instances are managed as isolated homes with their own access URL, `.env`, snapshots, clone flow, upgrade flow, and rollback flow.

- Service-Aware Model Configuration Reuse
  - The existing `provider` command family is now treated internally as model-configuration collection and reuse across services.
  - Collection now covers managed instances plus local `~/.openclaw` and `~/.hermes` when present.
  - Stored assets include service identity so OpenClaw and Hermes configs do not silently collide.

- Better Access Visibility
  - `create`, `list`, and `inspect` now surface access information for both services.
  - OpenClaw keeps its dashboard token and pairing flow.
  - Hermes is treated as a Web-accessible managed instance without forcing it into the same auth model.

- 170+ Tests
  - The suite now covers the shared lifecycle core, OpenClaw regressions, and new Hermes lifecycle/model-config behavior.

* * *
## Architecture Changes

### Shared Core

ClawCU is now split into:

- `src/clawcu/core/`
  - shared models
  - shared storage
  - shared paths
  - Docker wrapper
  - subprocess helpers
  - lifecycle orchestration
  - snapshot helpers
  - adapter contract

- `src/clawcu/openclaw/`
  - OpenClaw-specific image management
  - readiness logic
  - dashboard/token/pairing/TUI integration
  - OpenClaw model-config collection and apply

- `src/clawcu/hermes/`
  - Hermes source/build management
  - Hermes home/config/env handling
  - readiness and access metadata
  - Hermes config/chat integration
  - Hermes model-config collection and apply

The key design change is simple:

- lifecycle is unified
- service internals stay native

### Access Metadata

The core no longer assumes that every service behaves like OpenClaw.

Instead, adapters now provide:

- access URL
- readiness strategy
- auth hint
- service-specific lifecycle summaries

That lets ClawCU answer:

- where should the user go?
- how do we know readiness is real?
- what auth model should the UI hint at?

without hardcoding OpenClaw-only semantics into the shared layer.

* * *
## Hermes in v0.2.0

Hermes is now a real managed service inside ClawCU.

### Artifact Preparation

`clawcu pull hermes --version <ref>`

does all of the following:

- fetches the official Hermes repo into the managed source cache
- checks out the requested git ref
- updates submodules when needed
- builds a managed Docker image from the official Dockerfile

### Instance Model

Each Hermes instance is:

- an isolated managed home
- mounted from the chosen `datadir`
- exposed through a managed host port
- backed by a Hermes-native env file at:
  - `<datadir>/.env`

### Lifecycle Support

Hermes now participates in the same operational flow as OpenClaw:

- `create`
- `clone`
- `upgrade`
- `rollback`
- `recreate`
- `logs`
- `exec`
- `config`
- `tui`

From the user’s point of view, Hermes gets the same lifecycle safety guarantees:

- isolated instance homes
- reproducible versions
- snapshot-backed upgrades
- rollback recovery
- visible access URLs

* * *
## OpenClaw Compatibility

OpenClaw remains fully supported and keeps the operational model introduced in `v0.1.0`.

Important compatibility choices:

- OpenClaw remains official-image-only
- token and browser pairing approval remain OpenClaw-specific
- OpenClaw env files remain at:
  - `~/.clawcu/instances/<instance>.env`

This means `v0.2.0` broadens ClawCU without regressing the established OpenClaw workflow.

* * *
## Model Configuration Collection and Reuse

The old `provider` commands are still present, but they now sit on top of a broader service-aware model configuration layer.

Supported collection sources now include:

- all ClawCU-managed instances
- one managed instance
- local `~/.openclaw`
- local `~/.hermes`
- explicit `--path <home>`

Behavior changes in `v0.2.0`:

- service identity is stored with the collected asset
- model-config bundles from different services do not collide silently
- apply dispatches through the target instance service adapter

This preserves command continuity while making the implementation fit a multi-agent world.

* * *
## Environment Variables and Snapshots

ClawCU deliberately does not force a single env storage model across all services in `v0.2.0`.

Instead:

- OpenClaw uses:
  - `~/.clawcu/instances/<instance>.env`
- Hermes uses:
  - `<datadir>/.env`

The lifecycle layer is adapter-aware, so clone/upgrade/rollback now snapshot and restore the correct env location for each service.

That means:

- OpenClaw keeps the established sidecar env model
- Hermes follows its native home layout
- snapshot safety still remains consistent

* * *
## Service-Specific Access Behavior

Not every user-facing concept is shared between services.

In `v0.2.0`:

- `clawcu token <instance>`
  - supported for OpenClaw
  - unsupported for Hermes

- `clawcu approve <instance>`
  - supported for OpenClaw
  - unsupported for Hermes

This is intentional. ClawCU now has a shared lifecycle surface, but it does not pretend that OpenClaw and Hermes expose the same dashboard auth model.

* * *
## Recommended Workflow

The safest pattern remains:

1. clone a working instance
2. upgrade the clone
3. validate the clone
4. rollback if needed
5. only then decide whether to change the primary instance

This now applies to both services:

- OpenClaw
- Hermes

That makes `v0.2.0` less about “adding one more runtime” and more about establishing ClawCU as a reusable local operations layer for multiple agent systems.

* * *
## Closing Note

`v0.2.0` is the release where ClawCU stops being “the OpenClaw helper” and becomes a cleaner local operations platform:

- one machine
- multiple managed agent runtimes
- explicit lifecycle control
- safer upgrades
- native service boundaries
- clearer architecture for what comes next
