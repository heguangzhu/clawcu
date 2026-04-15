# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

`ClawCU` is a local-first lifecycle manager for `OpenClaw`.

It gives you a safer way to run multiple OpenClaw instances on one machine, keep versions under control, clone working instances for experiments, collect provider configs from existing setups, and recover cleanly when an upgrade goes sideways.

> If `OpenClaw` is the agent runtime, `ClawCU` is the operational safety layer around it.

## Why ClawCU

Running OpenClaw manually is powerful, but it is also easy to drift into a fragile setup:

- upgrades can break a previously working instance
- experiments are risky when they share the same live instance
- rollback is hard if you do not already have a clean snapshot

`ClawCU` is built to solve those problems with a Docker-based workflow that favors:

- explicit versioning
- reproducible local instances
- safe upgrade and rollback paths
- clone-first experimentation
- practical observability for providers, models, agents, snapshots, and env

## Highlights

- Full local lifecycle management for OpenClaw instances:
  - `pull`, `create`, `list`, `inspect`, `token`, `start`, `stop`, `restart`, `retry`, `recreate`, `upgrade`, `rollback`, `clone`, `logs`, `remove`
- Safe upgrades with automatic snapshots:
  - snapshots cover both the instance data directory and `~/.clawcu/instances/<instance>.env`
- Clone-first experimentation:
  - clone a working instance before testing a new version
- Model configuration collection and reuse:
  - collect provider assets from managed instances or local `~/.openclaw`
- Environment variable management:
  - `setenv`, `getenv`, `unsetenv`, plus `--apply` for immediate recreation
- Better day-to-day operability:
  - readiness waits
  - progress output
  - dashboard token lookup
  - snapshot summaries in `list` and `inspect`

## What ClawCU Manages

ClawCU is intentionally focused on local operations for OpenClaw.

It manages:

- Docker image preparation from the official OpenClaw image registry
- OpenClaw instance creation and lifecycle
- gateway bootstrap defaults needed for local access
- version transitions with rollback protection
- provider collection from existing OpenClaw homes
- env files and instance metadata

It does not try to replace OpenClaw itself.

For model setup, plugin setup, and agent-specific OpenClaw flows, ClawCU works with the native tools:

- `clawcu config <instance>`
- `clawcu exec <instance> <command...>`

## Install

```bash
uv tool install .
```

or

```bash
pipx install .
```

## Quick Start

Check local prerequisites:

```bash
clawcu setup
```

Show shell completion guidance only when you want it:

```bash
clawcu setup --completion
```

In an interactive terminal, `clawcu setup` also prompts for:

- the default `ClawCU home`
- the default OpenClaw image repo

The default OpenClaw image repo is:

```bash
ghcr.io/openclaw/openclaw
```

If `clawcu setup` detects that your public IP is in China and you have not configured a repo yet, it will suggest this mirror by default:

```bash
ghcr.nju.edu.cn/openclaw/openclaw
```

The chosen image repo is saved in:

```bash
~/.clawcu/config.json
```

The chosen default `ClawCU home` is saved in:

```bash
~/.config/clawcu/bootstrap.json
```

If `CLAWCU_HOME` is explicitly exported in your shell, that environment variable still takes precedence for the current process.

Pull an OpenClaw version from GHCR:

```bash
clawcu pull openclaw --version 2026.4.1
```

Create and start an instance:

```bash
clawcu create openclaw --name writer --version 2026.4.1
```

Verify that the instance is really usable after creation:

```bash
clawcu tui writer
```

This is a practical end-to-end check. It confirms that:

- the instance is running
- pairing can be completed
- the gateway can be reached
- the selected agent can enter the OpenClaw TUI successfully

Get the dashboard token:

```bash
clawcu token writer
```

List what is running:

```bash
clawcu list
clawcu list --managed
clawcu list --agents
```

Inspect one managed instance:

```bash
clawcu inspect writer
```

OpenClaw runs in `token` auth mode under ClawCU. This is required for the current `lan` binding model.

For the full command reference for `v0.1.0`, see [USAGE_v0.1.0.md](USAGE_v0.1.0.md).

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

Why this works well:

- your primary instance stays untouched
- version compatibility problems are isolated
- provider or model repairs can happen in the cloned instance
- rollback has a real snapshot to restore from

### What Upgrade Protects

Before `upgrade`, ClawCU snapshots:

- the instance data directory
- `~/.clawcu/instances/<instance>.env`

If the upgrade fails, ClawCU attempts to restore the previous version and the matching env snapshot automatically.

### What Rollback Restores

`rollback` restores:

- the previous snapshot of the data directory
- the matching env snapshot
- the previous image version

This means env-backed provider credentials come back with the same rollback boundary as the OpenClaw home itself.

## Cloning for Experiments

Cloning is designed for safe experimentation:

```bash
clawcu clone writer --name writer-exp
```

The cloned instance inherits:

- the source data directory
- the source env file, if present
- version
- CPU
- memory

ClawCU will:

- choose a fresh host port
- retry host port allocation by `+10` when needed
- roll back partial clone state if setup fails

## Provider Collection and Reuse

ClawCU does not invent a separate provider-definition format from scratch. Instead, it collects provider assets from OpenClaw homes that already work.

### Collect Providers

Collect from all managed instances plus local `~/.openclaw`:

```bash
clawcu provider collect --all
```

Collect from one managed instance:

```bash
clawcu provider collect --instance writer
```

Collect from an arbitrary OpenClaw home:

```bash
clawcu provider collect --path ~/.openclaw
```

List and inspect collected providers:

```bash
clawcu provider list
clawcu provider show openrouter
clawcu provider models list openrouter
```

### Collection Rules

ClawCU treats root `openclaw.json` as the source of truth for which providers are active.

Collected providers are deduplicated using:

- provider name
- API style
- endpoint
- API key

If those match, models are merged into one collected provider.

If the provider name matches but the endpoint or API key is different, ClawCU keeps a numbered variant such as `-2`.

### Apply a Provider to an Instance

Apply a collected provider to one agent:

```bash
clawcu provider apply kimi-coding writer
clawcu provider apply kimi-coding writer --agent chat
clawcu provider apply kimi-coding writer --agent chat --primary kimi-coding/k2p5
clawcu provider apply kimi-coding writer --agent chat --fallbacks anthropic/claude-sonnet-4.5,openai/gpt-4.1
clawcu provider apply kimi-coding writer --agent chat --primary kimi-coding/k2p5 --persist
```

Behavior:

- defaults `--agent` to `main`
- adds provider runtime config to the selected agent
- can set `primary`
- can set `fallbacks`
- with `--persist`, writes env-backed references into root config and saves the real key into the instance env file

## Environment Variable Management

ClawCU treats instance env files as first-class operational state.

Set one or more env vars:

```bash
clawcu setenv writer OPENAI_API_KEY=sk-xxx
clawcu setenv writer OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://api.example.com/v1
```

Read env vars:

```bash
clawcu getenv writer
```

Delete env vars:

```bash
clawcu unsetenv writer OPENAI_API_KEY
```

Apply immediately by recreating the container:

```bash
clawcu setenv writer OPENAI_API_KEY=sk-xxx --apply
clawcu unsetenv writer OPENAI_API_KEY --apply
```

Important detail:

- `restart` does not reload Docker env files
- `recreate` does

That is why env changes become fully effective after `recreate`, not plain `restart`.

## Access, Pairing, and Native OpenClaw Flows

Run the native OpenClaw configure flow inside a managed instance:

```bash
clawcu config writer
clawcu config writer -- --help
```

Run arbitrary commands in the container:

```bash
clawcu exec writer openclaw config
clawcu exec writer pwd
clawcu exec writer ls
```

If the browser shows `pairing required`, approve the latest pending request:

```bash
clawcu approve writer
```

Start the OpenClaw TUI and auto-handle the common approve step:

```bash
clawcu tui writer
clawcu tui writer --agent chat
```

## List and Inspect

ClawCU has two useful levels of visibility:

### Instance View

```bash
clawcu list
clawcu list --managed
clawcu list --local
```

The default `list` view includes both:

- managed ClawCU instances
- local `~/.openclaw`

Managed instance summaries include:

- source
- name
- home
- version
- port
- status
- providers
- models
- snapshot summary

### Agent View

```bash
clawcu list --agents
clawcu list --managed --agents
```

Agent rows show:

- instance
- agent
- primary
- fallbacks

### Detailed Inspect

```bash
clawcu inspect writer
```

`inspect` includes:

- instance metadata
- Docker state
- recent history
- snapshot summary block:
  - `latest_upgrade_snapshot`
  - `latest_rollback_snapshot`
  - `latest_restored_snapshot`

## Logs and Recovery

Tail logs:

```bash
clawcu logs writer --follow
```

Recover from a failed first-time create:

```bash
clawcu retry writer
```

Rebuild an instance container from saved config:

```bash
clawcu recreate writer
```

Stop, start, and restart:

```bash
clawcu stop writer
clawcu start writer
clawcu restart writer
```

Remove an instance:

```bash
clawcu remove writer --keep-data
clawcu remove writer --delete-data
```

## Runtime Layout

ClawCU stores operational state outside the repository:

- `~/.clawcu/instances/`
  - instance metadata
  - instance env files
- `~/.clawcu/providers/`
  - collected provider assets
- `~/.clawcu/logs/`
  - lifecycle logs
- `~/.clawcu/snapshots/`
  - upgrade and rollback snapshots

You can override the default home with:

```bash
CLAWCU_HOME=/custom/path
```

## Current Scope

`v0.1.0` is intentionally focused.

Included:

- one-machine OpenClaw lifecycle management
- versioned Docker operations
- snapshots and rollback
- clone-based experimentation
- provider collection and reuse
- env management

Not included:

- a web control plane for ClawCU itself
- cloud orchestration
- multi-host fleet management
- non-OpenClaw services such as Hermes
- business workflow orchestration inside agents

## Release Notes

- Release notes: [RELEASE_v0.1.0.md](/Users/michael/workspaces/codex/hello/openclaw-docker/RELEASE_v0.1.0.md)

## License

MIT. See [LICENSE](/Users/michael/workspaces/codex/hello/openclaw-docker/LICENSE).
