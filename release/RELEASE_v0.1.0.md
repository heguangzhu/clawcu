# ClawCU v0.1.0

🌐 Language:
[English](RELEASE_v0.1.0.md) | [中文](RELEASE_v0.1.0.zh-CN.md)

Release Date: April 15, 2026

> `v0.1.0` is the first release where ClawCU feels like a complete local OpenClaw lifecycle tool instead of a thin helper script. The focus of this release is operational safety: easier instance creation, safer upgrades, clearer rollback behavior, reusable provider collection, and better day-to-day debugging ergonomics.

* * *
## Highlights

- Local-First OpenClaw Lifecycle
  - `clawcu` now covers the full core lifecycle for managed OpenClaw instances: `pull`, `create`, `list`, `inspect`, `start`, `stop`, `restart`, `retry`, `recreate`, `upgrade`, `rollback`, `clone`, `logs`, and `remove`.

- Safe Versioned Upgrades
  - `upgrade` creates a pre-upgrade safety snapshot before replacing the container.
  - `rollback` restores both the instance data directory and the matching instance env snapshot.
  - Failed upgrades automatically attempt to restore the previous version and snapshot.

- Provider Collection and Reuse
  - Provider assets can be collected from:
    - all managed instances plus local `~/.openclaw`
    - one managed instance
    - an arbitrary OpenClaw data directory via `--path`
  - Collected providers are normalized, deduplicated, and stored for reuse.

- Better Operational UX
  - `create`, `clone`, `upgrade`, and `rollback` now show step-by-step progress instead of long silent waits.
  - Lifecycle commands wait for readiness and surface health-check progress instead of reporting success too early.
  - `list` and `inspect` now expose more operational state, including provider/model summaries and snapshot context.

- 150+ Tests
  - The repository test suite is now comprehensive across lifecycle operations, rollback behavior, provider collection, env handling, and CLI workflows.

* * *
## Core Lifecycle

### Instance Creation & Recovery

- `clawcu create openclaw --name <name> --version <version>`
  - defaults `datadir` to `~/.clawcu/<name>`
  - defaults host port to `18789`
  - probes ports by `+10` on conflict
  - configures Gateway defaults automatically
  - waits until the instance is actually ready

- `clawcu retry <name>`
  - retries instances stuck in `create_failed`
  - preserves failure history for debugging

- `clawcu recreate <name>`
  - rebuilds the container from the saved instance configuration
  - reuses the same data directory, version, resources, and env file

### Cloning

- `clawcu clone <source> --name <name>`
  - copies the source data directory
  - copies the source instance env file when present
  - inherits version, CPU, and memory
  - auto-selects a port with the same `+10` retry logic used by create
  - rolls back partial state if clone setup fails

* * *
## Safe Upgrades & Rollback

### Upgrade Flow

`clawcu upgrade <name> --version <target>`

- snapshots the current data directory
- snapshots `~/.clawcu/instances/<name>.env`
- prepares the target image
- replaces the container using the same instance configuration
- waits for OpenClaw readiness before returning success
- records snapshot location in instance history

If the new version fails to start cleanly:

- the failed container is removed
- the previous snapshot is restored
- the old version is started again
- the failure is written into instance history

### Rollback Flow

`clawcu rollback <name>`

- resolves the latest reversible transition from history
- prepares the old image if needed
- creates a rollback safety snapshot of the current state
- restores the prior snapshot
- restores the matching env snapshot
- starts the old version and waits for readiness

### Snapshot Visibility

Operational visibility was improved in two places:

- `clawcu list --managed`
  - now includes a `SNAPSHOT` summary column such as:
    - `upgrade -> 2026.4.10`
    - `rollback -> 2026.4.1`

- `clawcu inspect <instance>`
  - now includes a `snapshots` block with:
    - `latest_upgrade_snapshot`
    - `latest_rollback_snapshot`
    - `latest_restored_snapshot`

* * *
## Provider Workflow

### Collect

`clawcu provider collect` was redesigned around OpenClaw’s existing configuration files instead of inventing a parallel provider-definition format.

Supported collection modes:

- `clawcu provider collect --all`
  - scans all ClawCU-managed instances
  - also includes local `~/.openclaw`

- `clawcu provider collect --instance <instance>`
  - collects from one managed instance

- `clawcu provider collect --path <openclaw-home>`
  - collects from a non-managed OpenClaw data directory

### Collection Rules

- root `openclaw.json` is the primary source of truth for which providers are considered active
- provider payloads are split and stored independently
- deduplication is based on:
  - provider name
  - API style
  - endpoint
  - API key
- if those match, models are merged into one collected provider
- if the same provider name appears with a different API key or endpoint, a numbered variant such as `-2` is created

### Provider Apply

`clawcu provider apply <provider> <instance> --agent <agent>`

- defaults `--agent` to `main`
- can set:
  - `--primary`
  - `--fallbacks`
  - `--persist`

Behavior:

- writes provider config to the selected agent runtime files
- can update `primary` and `fallbacks` for that agent
- with `--persist`, stores API keys in the instance env file and writes env references into root config

* * *
## Environment Variable Management

Environment-variable handling is now a first-class part of instance operations.

### Commands

- `clawcu setenv <instance> KEY=VALUE [KEY=VALUE ...]`
- `clawcu getenv <instance>`
- `clawcu unsetenv <instance> KEY [KEY ...]`

Optional apply-now behavior:

- `clawcu setenv ... --apply`
- `clawcu unsetenv ... --apply`

This updates the instance env file and immediately recreates the container so Docker can reload the env file.

### Storage Location

Instance env files live here:

- `~/.clawcu/instances/<instance>.env`

This keeps env configuration separated from the OpenClaw home directory while still allowing `recreate`, `upgrade`, `rollback`, and `clone` to work consistently.

* * *
## Listing, Inspection, and Debugging

### List Views

- `clawcu list`
  - defaults to an `--all` style view that includes:
    - local `~/.openclaw`
    - managed instances

- `clawcu list --managed`
  - shows managed-instance summaries

- `clawcu list --local`
  - shows local `~/.openclaw`

- `clawcu list --agents`
  - expands into agent-level rows

### Instance-Level Visibility

Instance summaries now include:

- source
- name
- home
- version
- port
- status
- providers
- models
- snapshot summary

### Agent-Level Visibility

Agent rows now surface:

- instance
- agent
- primary
- fallbacks

Agent naming is derived from actual agent directory names instead of falling back to placeholder values like `defaults`.

* * *
## Access, Pairing, and Configuration

- `clawcu token <instance>`
  - prints the dashboard token for a managed instance

- `clawcu approve <instance> [request-id]`
  - approves pending browser pairing requests for local Docker-based setups

- `clawcu config <instance>`
  - runs `openclaw configure` inside the managed instance container

- `clawcu exec <instance> <command...>`
  - runs arbitrary commands inside the container

- `clawcu tui <instance> [--agent <agent>]`
  - launches the OpenClaw TUI
  - auto-handles the common local approve flow before entering TUI

These commands make it easier to bridge ClawCU-managed lifecycle operations with OpenClaw’s own built-in setup flows.

* * *
## Setup & Runtime Layout

`clawcu setup` now acts as a real prerequisite check for local environments.

It verifies:

- Docker CLI availability
- Docker daemon availability
- ClawCU home directory readiness
- runtime directory layout
- OpenClaw image repo configuration
- shell-completion guidance when explicitly requested

State layout remains external to the repository:

- `~/.clawcu/instances/`
- `~/.clawcu/providers/`
- `~/.clawcu/logs/`
- `~/.clawcu/snapshots/`

* * *
## Recommended Upgrade Strategy

The recommended workflow for risky OpenClaw upgrades is:

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test
```

This keeps the original instance untouched while you validate the upgrade in a cloned branch.

* * *
## Known Constraints

- ClawCU is still intentionally focused on local Docker-based OpenClaw management.
- `restart` does not reload env files, because Docker only applies `--env-file` at container creation time.
- For env changes to fully enter the process environment, use `recreate` or `setenv/unsetenv --apply`.
- Provider persistence and runtime application are intentionally conservative to avoid breaking a working OpenClaw instance unexpectedly.

* * *
## Closing Note

`v0.1.0` is about making OpenClaw safer to run as infrastructure on a single machine:

- safer creation
- safer cloning
- safer provider reuse
- safer upgrades
- safer rollbacks
- clearer inspection when something goes wrong

That operational trust is the real goal of this release.
