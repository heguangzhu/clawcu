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

Check local prerequisites before creating instances:

```bash
clawcu setup
```

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

ClawCU keeps OpenClaw in `token` auth mode. This is required for the current `lan` binding model.

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

Recreate an existing instance with updated gateway settings:

```bash
clawcu recreate writer
```

If the browser shows `pairing required`, approve the latest pending browser request:

```bash
clawcu approve writer
```

Run the official per-instance OpenClaw setup flows:

```bash
clawcu config writer
```

To pass flags through to the underlying OpenClaw command, use `--`:

```bash
clawcu config writer -- --help
```

Run any command inside the instance container:

```bash
clawcu exec writer openclaw config
clawcu exec writer pwd
clawcu exec writer ls
```

Collect and reuse provider assets:

```bash
clawcu provider collect --all
clawcu provider collect --instance writer
clawcu provider collect --path ~/.openclaw
clawcu provider list
clawcu provider show openai
clawcu provider apply openai writer
clawcu provider apply openai writer --agent chat
clawcu provider apply openai writer --agent chat --primary openai/gpt-5 --fallbacks anthropic/claude-sonnet-4.5,openai/gpt-4.1
clawcu provider models list openai
```

## Runtime layout

ClawCU stores state outside the project repository:

- `~/.clawcu/instances/` for instance metadata
- `~/.clawcu/providers/` for collected provider assets
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
