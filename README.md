# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

`ClawCU` is a local-first lifecycle manager for running multiple AI agent runtimes on one machine. It currently supports: [OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## Why ClawCU

Running agent runtimes by hand is powerful, but it gets fragile quickly:

- upgrades can break a previously working setup
- experiments are risky when they share the same live instance
- rollback is painful without a clean snapshot boundary
- local configuration becomes harder to reason about as you add more instances and more than one runtime

ClawCU is built to solve those problems with a Docker-based workflow that favors:

- explicit versioning
- isolated local instances
- clone-first experimentation
- safe upgrade and rollback paths
- practical observability for services, access URLs, models, env files, and snapshots

## Highlights

- One lifecycle tool for two services:
  - `openclaw`
  - `hermes`
- Shared command surface:
  - lifecycle: `pull`, `create`, `list`, `inspect`, `start`, `stop`, `restart`, `recreate`, `upgrade`, `rollback`, `clone`, `logs`, `remove`
  - access: `exec`, `config`, `tui`, `token`, `approve`
  - env: `setenv`, `getenv`, `unsetenv`
  - model configuration: `provider collect/list/show/apply/remove/models`
- Safe upgrades with automatic snapshot protection:
  - snapshots cover both the instance data directory and the matching env location for that service
- Clone-first experimentation:
  - copy a working instance before testing a new version
- Orphan instance lifecycle:
  - `clawcu list --removed` surfaces datadirs whose instance records were lost
  - `clawcu recreate <orphan>` rebuilds them with full port / version / metadata recovery from `.clawcu-instance.json`
  - `clawcu remove <orphan> --removed` permanently deletes an orphan datadir
- Service-aware model configuration collection and reuse:
  - collect from managed instances or local homes such as `~/.openclaw` and `~/.hermes`
- Better operational visibility:
  - readiness waits
  - progress output
  - access URLs in `create`, `list`, and `inspect`
  - snapshot summaries in `list` and `inspect`

## Supported Services

### OpenClaw

- Artifact source:
  - official image registry
- Access model:
  - dashboard URL
  - token
  - browser pairing approval
- Native commands exposed through ClawCU:
  - `config`
  - `tui`
  - `token`
  - `approve`
- Env location:
  - `~/.clawcu/instances/<instance>.env`

### Hermes

- Artifact source:
  - prebuilt Hermes images pulled from `clawcu/hermes-agent:<tag>`
- Access model:
  - local Web Dashboard URL
  - CLI/chat access through `tui`, `config`, and `exec`
- Native commands exposed through ClawCU:
  - `config`
  - `tui`
- Env location:
  - `<datadir>/.env`

ClawCU intentionally does not force OpenClaw and Hermes into the same auth or env model. Lifecycle is unified; service internals remain native.

## Install

```bash
uv tool install .
```

or

```bash
pipx install .
```

## Quick Start

Check Docker access, runtime directories, and configure defaults:

```bash
clawcu setup
```

Show shell completion guidance only when you want it:

```bash
clawcu setup --completion
```

In an interactive terminal, `clawcu setup` prompts for:

- the default `ClawCU home`
- the default OpenClaw image repo
- the default Hermes image repo

### OpenClaw

Prepare an OpenClaw version for use:

```bash
clawcu pull openclaw --version 2026.4.1
```

This records the OpenClaw image ClawCU should use. If it is missing locally, Docker pulls it when the instance starts.

Create and start an instance:

```bash
clawcu create openclaw --name writer --version 2026.4.1
```

Verify that the instance is really usable:

```bash
clawcu tui writer
```

Get the dashboard token:

```bash
clawcu token writer
```

### Hermes

Pull a prebuilt Hermes image:

```bash
clawcu pull hermes --version v2026.4.13
```

Create and start a Hermes instance:

```bash
clawcu create hermes --name analyst --version v2026.4.13
```

Enter the Hermes interactive flow:

```bash
clawcu tui analyst
```

### Shared day-to-day commands

List what is running:

```bash
clawcu list
clawcu list --managed
clawcu list --agents
clawcu list --removed     # orphan datadirs whose records were lost
```

Inspect one managed instance:

```bash
clawcu inspect writer
clawcu inspect analyst
```

Run a native command inside the container:

```bash
clawcu exec writer pwd
clawcu exec analyst hermes version
```

For the full command reference for `v0.2.8`, see [USAGE_v0.2.8.md](release/USAGE_v0.2.8.md).

## Safe Upgrade Workflow

The recommended ClawCU workflow is:

1. clone a working instance
2. upgrade the clone
3. validate the clone
4. rollback the clone if needed
5. only then decide whether to upgrade the original instance

Example:

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test
```

The same pattern also works for Hermes:

```bash
clawcu clone analyst --name analyst-upgrade-test
clawcu upgrade analyst-upgrade-test --version v0.9.1
clawcu rollback analyst-upgrade-test
```

Why this works well:

- your primary instance stays untouched
- compatibility problems are isolated
- repairs happen in the cloned instance
- rollback has a real snapshot to restore from

### What upgrade protects

Before `upgrade`, ClawCU snapshots:

- the instance data directory
- the matching service env file

That means:

- OpenClaw snapshots:
  - `datadir`
  - `~/.clawcu/instances/<instance>.env`
- Hermes snapshots:
  - `datadir`
  - `<datadir>/.env`

If the upgrade fails, ClawCU attempts to restore the previous version and the matching env snapshot automatically.

## Orphan Recovery Workflow

If an instance's record is lost — registry corruption, a restored backup, an aborted `create` that left state behind — its datadir becomes an **orphan**: still on disk under `~/.clawcu`, but no longer tracked.

Discover orphans:

```bash
clawcu list --removed
```

Each row shows the service / version / port recovered from the datadir's `.clawcu-instance.json` sidecar.

Recover:

```bash
clawcu recreate <orphan>                     # auto-restore from metadata
clawcu recreate <orphan> --version 2026.4.9  # when metadata is missing
```

Permanently delete:

```bash
clawcu remove <orphan> --removed --yes
```

In `--removed` mode, `--keep-data` / `--delete-data` are rejected — the flag already implies permanent deletion.

## Model Configuration Collection and Reuse

ClawCU keeps the existing `provider` command family for compatibility, but it is now best read as model-configuration collection and reuse across services.

Collect from all managed instances plus local homes:

```bash
clawcu provider collect --all
```

Collect from one managed instance:

```bash
clawcu provider collect --instance writer
clawcu provider collect --instance analyst
```

Collect from a local home:

```bash
clawcu provider collect --path ~/.openclaw
clawcu provider collect --path ~/.hermes
```

Inspect what has been collected:

```bash
clawcu provider list
clawcu provider show openclaw:minimax
clawcu provider show hermes:openrouter
```

Apply a collected model configuration:

```bash
clawcu provider apply openclaw:minimax writer --agent main --primary minimax/MiniMax-M2.7
clawcu provider apply hermes:openrouter analyst --persist
```

Service identity is stored with each collected bundle so OpenClaw and Hermes configs with the same logical name do not silently collide.

## Environment Variables

ClawCU manages env files per service instead of forcing a single storage model.

OpenClaw:

- env path:
  - `~/.clawcu/instances/<instance>.env`

Hermes:

- env path:
  - `<datadir>/.env`

Shared commands:

```bash
clawcu setenv <instance> KEY=VALUE
clawcu getenv <instance>
clawcu unsetenv <instance> KEY
```

Optional immediate apply:

```bash
clawcu setenv <instance> KEY=VALUE --apply
clawcu unsetenv <instance> KEY --apply
```

`--apply` recreates the instance so Docker reloads the env file for the running container.

## Access and Service-Specific Commands

ClawCU keeps the common command surface broad, but some access commands remain service-specific.

OpenClaw-only:

- `clawcu token <instance>`
- `clawcu approve <instance> [requestId]`

These work because OpenClaw has a matching dashboard token and pairing model.

Hermes:

- has a dashboard URL surfaced by `create`, `list`, and `inspect`
- uses `tui`, `config`, and `exec` as the main operational entrypoints
- does not map to the same `token` or `approve` concepts

## Release Notes

- Release notes for `v0.2.8`: [RELEASE_v0.2.8.md](release/RELEASE_v0.2.8.md)
- Archived release notes for `v0.2.0`: [RELEASE_v0.2.0.md](release/RELEASE_v0.2.0.md)
- Archived release notes for `v0.1.0`: [RELEASE_v0.1.0.md](release/RELEASE_v0.1.0.md)
