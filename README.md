# ClawCU

`ClawCU` is a local-first lifecycle manager for `OpenClaw`.

`v0.0.1` focuses on one thing: letting AI enthusiasts run OpenClaw on a single machine with a versioned Docker workflow that is easier to create, inspect, upgrade, roll back, and clone for experiments.

## Install

```bash
uv tool install .
```

or

```bash
pipx install .
```

## Quick start

Pull and build an OpenClaw image:

```bash
clawcu pull openclaw --version 2026.4.1
```

Create and start an instance:

```bash
clawcu create openclaw \
  --name writer \
  --version 2026.4.1 \
  --datadir ~/clawcu-data/writer \
  --port 18789 \
  --cpu 1 \
  --memory 2g
```

List instances:

```bash
clawcu list
```

Upgrade an instance:

```bash
clawcu upgrade writer --version 2026.4.2
```

Clone an experiment branch:

```bash
clawcu clone writer \
  --name writer-exp \
  --datadir ~/clawcu-data/writer-exp \
  --port 3001
```

## Runtime layout

ClawCU stores state outside the project repository:

- `~/.clawcu/instances/` for instance metadata
- `~/.clawcu/sources/openclaw/<version>/` for cached upstream source checkouts
- `~/.clawcu/logs/` for operation logs
- `~/.clawcu/snapshots/` for upgrade and rollback snapshots

You can override the home directory with `CLAWCU_HOME`.

## Current scope

`v0.0.1` intentionally does not include:

- a Web control plane
- cloud or multi-host orchestration
- agent-level business workflows inside OpenClaw
- client integration flags such as `web` or `feishu`

It is a local Docker-focused CLI first.
