# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/clawcu.svg)](https://pypi.org/project/clawcu/)
[![Python](https://img.shields.io/pypi/pyversions/clawcu.svg)](https://pypi.org/project/clawcu/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/heguangzhu/clawcu/actions/workflows/ci.yml/badge.svg)](https://github.com/heguangzhu/clawcu/actions/workflows/ci.yml)

`ClawCU` is a local-first lifecycle manager for running multiple AI agent runtimes on one machine. It currently supports [OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/NousResearch/hermes-agent).

<details>
<summary>Contents</summary>

- [Highlights](#highlights)
- [Install](#install)
- [Quick Start](#quick-start)
- [Safe Upgrade Workflow](#safe-upgrade-workflow)
- [Model Configuration](#model-configuration)
- [Environment and Access](#environment-and-access)
- [Changelog](#changelog)
- [Contributing](#contributing)
- [Uninstall](#uninstall)
- [License](#license)

</details>

## Highlights

- **One CLI, two runtimes** — OpenClaw and Hermes through the same lifecycle commands
- **Snapshots before every upgrade** — datadir and env both captured; `rollback` restores from real backups
- **Clone-first experiments** — copy an instance, upgrade the copy, leave the original running
- **Available versions with cache-aware refresh** — `clawcu list` is fast by default, and `clawcu list --no-cache` forces a fresh registry read when you want to see the latest tags now

```text
$ clawcu list
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ NAME            ┃ SERVICE  ┃ VERSION   ┃ PORT  ┃ STATUS  ┃ ACCESS          ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ writer          │ openclaw │ 2026.4.1  │ 18799 │ running │ 127.0.0.1:18799 │
│ analyst         │ hermes   │ 2026.4.13 │ 9129  │ running │ 127.0.0.1:9129  │
└─────────────────┴──────────┴───────────┴───────┴─────────┴─────────────────┘

Available versions (top 10 by semver, newest first)
  openclaw  2026.4.15, 2026.4.14, 2026.4.12, 2026.4.11, 2026.4.10, 2026.4.9,
            2026.4.8, 2026.4.7, 2026.4.5, 2026.4.2
  hermes    2026.4.16, 2026.4.13, 2026.4.8, 2026.4.3, 2026.3.30
```

## Install

Requires Python 3.11+ and a running Docker daemon.

```bash
pip install clawcu
```

or, for isolated CLI installs:

```bash
pipx install clawcu
# or
uv tool install clawcu
```

## Quick Start

First-time setup — checks Docker access and configures defaults:

```bash
clawcu setup
```

Spin up an OpenClaw instance and open the TUI:

```bash
clawcu pull openclaw --version 2026.4.1
clawcu create openclaw --name writer --version 2026.4.1
clawcu tui writer
```

If you maintain a custom runtime image, keep the logical service version explicit and override only the runtime image:

```bash
clawcu create openclaw \
  --name writer-tools \
  --version 2026.4.1 \
  --image registry.example.com/openclaw:2026.4.1-tools
```

Or spin up a Hermes instance with the same shape:

```bash
clawcu pull hermes --version 2026.4.13
clawcu create hermes --name analyst --version 2026.4.13
clawcu tui analyst
```

Configure providers / models inside an instance via its service-native flow (OpenClaw's `configure`, Hermes's `setup`):

```bash
clawcu config writer
```

Need the versions footer to ignore today's cache and fetch fresh tags immediately?

```bash
clawcu list --no-cache
```

For the full command reference (`list` / `inspect` / `exec` / `upgrade` / `provider` …), see [USAGE_latest.md](release/USAGE_latest.md).

## Safe Upgrade Workflow

Upgrade on a clone first; promote only if the clone holds:

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test    # if the new version misbehaves
```

`upgrade` also accepts `--image` when you want to keep the target version label explicit but run a custom image:

```bash
clawcu upgrade writer-upgrade-test \
  --version 2026.4.10 \
  --image registry.example.com/openclaw:2026.4.10-tools
```

Every `upgrade` snapshots the instance datadir and the matching env file (`~/.clawcu/instances/<instance>.env` for OpenClaw, `<datadir>/.env` for Hermes) before replacing the container. If the upgrade fails, ClawCU restores both automatically. When `--image` is used, the chosen runtime image is persisted as part of the instance state, so later `recreate`, orphan recovery, and `rollback` continue using the same recorded image chain.

## Model Configuration

Collect API keys and model lists from any managed instance or local home, and apply them elsewhere:

```bash
clawcu provider collect --all
clawcu provider list
clawcu provider apply openclaw:minimax writer --agent main --primary minimax/MiniMax-M2.7
```

Service identity is stored with each collected bundle, so OpenClaw and Hermes configs with the same logical name do not silently collide.

## Environment and Access

Env files live per service (not unified):

- OpenClaw: `~/.clawcu/instances/<instance>.env`
- Hermes: `<datadir>/.env`

Manage them with `clawcu setenv` / `getenv` / `unsetenv` (pass `--apply` to recreate the container immediately). Service-specific access: `clawcu token <instance>` and `clawcu approve <instance>` are OpenClaw-only (dashboard token + pairing model); Hermes uses `clawcu tui` / `config` / `exec` as its operational entrypoints. Full command reference: [USAGE_latest.md](release/USAGE_latest.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history. Latest release notes: [RELEASE_latest.md](release/RELEASE_latest.md).

## Contributing

Issues and PRs welcome at [github.com/heguangzhu/clawcu/issues](https://github.com/heguangzhu/clawcu/issues).

Local development:

```bash
git clone https://github.com/heguangzhu/clawcu.git
cd clawcu
uv sync --all-extras
uv run pytest -q
```

## Uninstall

```bash
pip uninstall clawcu
# or
pipx uninstall clawcu
# or
uv tool uninstall clawcu
```

Uninstalling the CLI leaves every datadir under `~/.clawcu` intact. Remove instance data explicitly with `clawcu remove <name> --delete-data` before uninstalling if you want it gone.

## License

MIT — see [LICENSE](LICENSE).
