# ClawCU Usage v0.2.0

🌐 Language:
[English](USAGE_v0.2.0.md) | [中文](USAGE_v0.2.0.zh-CN.md)

Release Scope: `v0.2.0`

This document is the command reference for `ClawCU v0.2.0`.

It describes the shared command surface, the service-specific differences between OpenClaw and Hermes, and the operational defaults ClawCU applies.

## 1. Setup and Artifact Preparation

| Command | Description |
|------|------|
| `clawcu --version` | Show the installed ClawCU version. |
| `clawcu setup [--completion]` | Check Docker, ClawCU home, runtime directories, and interactively configure the default ClawCU home, the OpenClaw image repo, the Hermes source repo, and an optional Hermes build proxy. |
| `clawcu pull openclaw --version <version>` | Pull the official OpenClaw image for the requested version. If the image is missing or cannot be pulled, ClawCU reports the error directly. |
| `clawcu pull hermes --version <ref>` | Fetch the official Hermes repository at the requested git ref and build a managed Docker image from its Dockerfile. |

## 2. Instance Creation

| Command | Description |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | Create and start an OpenClaw instance. `datadir` defaults to `~/.clawcu/<name>`. Host port defaults to `18789` and probes by `+10` on conflict. |
| `clawcu create hermes --name <name> --version <ref> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | Create and start a Hermes instance. `datadir` defaults to `~/.clawcu/<name>`. Host port defaults to `8642` and probes by `+10` on conflict. |

## 3. Shared Lifecycle Commands

| Command | Description |
|------|------|
| `clawcu list [--running] [--managed\|--local\|--all] [--agents]` | List instance summaries or per-agent rows. By default, `list` shows both managed instances and local homes discovered by the adapters. |
| `clawcu inspect <name>` | Show detailed instance state, including Docker container information, access info, history, and snapshot summaries. |
| `clawcu start <name>` | Start a stopped managed instance. |
| `clawcu stop <name>` | Stop a running managed instance. |
| `clawcu restart <name>` | Restart a managed instance. |
| `clawcu retry <name>` | Retry an instance that is stuck in `create_failed`. |
| `clawcu recreate <name>` | Recreate an instance container from saved configuration while keeping the same instance settings. |
| `clawcu upgrade <name> --version <version-or-ref>` | Upgrade an instance to a new service version or git ref. ClawCU snapshots the instance home plus the matching env path before replacing the container. |
| `clawcu rollback <name>` | Roll back an instance to the previous reversible version transition by restoring the matching snapshot and env snapshot. |
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>]` | Copy a source instance into a new isolated experiment instance. |
| `clawcu logs <name> [--follow]` | Show instance logs. `--follow` keeps streaming them. |
| `clawcu remove <name> [--keep-data\|--delete-data]` | Remove the instance and container, with a choice to keep or delete the data directory. |

## 4. Interactive Access and Native Commands

| Command | Description |
|------|------|
| `clawcu config <name> [-- args...]` | Run the service-native configuration flow inside the managed container. OpenClaw maps to `openclaw configure`; Hermes maps to its native config flow. |
| `clawcu exec <name> <command...>` | Run an arbitrary command inside the managed container with the instance env injected. |
| `clawcu tui <name> [--agent <agent>]` | Launch the native interactive flow for the instance. OpenClaw uses its TUI flow; Hermes uses its interactive chat flow. |

## 5. Service-Specific Access Commands

| Command | Description |
|------|------|
| `clawcu token <name>` | Print the OpenClaw dashboard token. This is currently OpenClaw-only. Hermes instances fail with a clear unsupported message. |
| `clawcu approve <name> [requestId]` | Approve a pending OpenClaw browser pairing request. This is currently OpenClaw-only. Hermes instances fail with a clear unsupported message. |

## 6. Environment Variable Management

| Command | Description |
|------|------|
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE ...] [--apply]` | Write environment variables into the instance env file. `--apply` immediately recreates the instance so Docker reloads the env file. |
| `clawcu getenv <name>` | Print the current environment variables configured for the instance. |
| `clawcu unsetenv <name> KEY [KEY ...] [--apply]` | Remove environment variables from the instance env file. `--apply` immediately recreates the instance. |

## 7. Model Configuration Collection and Reuse

| Command | Description |
|------|------|
| `clawcu provider collect --all` | Collect model configuration assets from all ClawCU-managed instances plus local `~/.openclaw` and `~/.hermes` when present. |
| `clawcu provider collect --instance <name>` | Collect model configuration from one managed instance. |
| `clawcu provider collect --path <home>` | Collect model configuration from any OpenClaw or Hermes home directory. |
| `clawcu provider list` | List collected model configuration assets, including service identity and masked API key summaries. |
| `clawcu provider show <name>` | Show the stored payload for one collected asset, with secrets masked in display output. If the name is ambiguous across services, use `openclaw:<name>` or `hermes:<name>`. |
| `clawcu provider remove <name>` | Remove a collected model configuration asset. |
| `clawcu provider models list <name>` | List the models stored in a collected asset. |
| `clawcu provider apply <provider> <instance> [--agent <agent>] [--primary <model>] [--fallbacks <m1,m2>] [--persist]` | Apply a collected model configuration asset to the selected instance. `--agent` defaults to `main`. The exact writeback behavior is service-native. |

## 8. Default Behavior Conventions

- Port defaults:
  - OpenClaw starts from `18789`
  - Hermes starts from `8642`
  - both probe by `+10` on conflict
- Resources:
  - default is `1 CPU + 2GB RAM`
- Data directory:
  - default is `~/.clawcu/<instance-name>`
- Container naming:
  - `clawcu-<service>-<instance-name>`
- Access info:
  - both services expose an access URL in `create`, `list`, and `inspect`
- Env location:
  - OpenClaw uses `~/.clawcu/instances/<instance>.env`
  - Hermes uses `<datadir>/.env`
- Snapshot behavior:
  - `upgrade` and `rollback` snapshot and restore both the instance home and the matching env path
- Recommended upgrade strategy:
  - `clone` first, `upgrade` on the clone, validate, then `rollback` if needed

## 9. Notes

- This usage guide describes the command surface for `v0.2.0`.
- For release context, see [RELEASE_v0.2.0.md](RELEASE_v0.2.0.md).
- Archived `v0.1.0` usage remains available in [USAGE_v0.1.0.md](USAGE_v0.1.0.md).
