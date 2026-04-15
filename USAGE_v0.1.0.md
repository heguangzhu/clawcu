# ClawCU Usage v0.1.0

🌐 Language:
[English](USAGE_v0.1.0.md) | [中文](USAGE_v0.1.0.zh-CN.md)

Release Scope: `v0.1.0`

This document is the command reference for `ClawCU v0.1.0`.

It focuses on what each command is for, how it is grouped, and what behavior users should expect in day-to-day operation.

## 1. Version and Image Commands

| Command | Description |
|------|------|
| `clawcu --version` | Show the installed ClawCU version. |
| `clawcu setup [--completion]` | Check Docker, ClawCU home, runtime directories, and interactively configure the default ClawCU home and OpenClaw image repo. Shell completion guidance is shown only when `--completion` is passed. |
| `clawcu pull openclaw --version <version>` | Pull the official OpenClaw image for the requested version. If the image is missing or cannot be pulled, ClawCU reports the error directly. |

## 2. Instance Lifecycle

| Command | Description |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--port <port>] [--cpu 1] [--memory 2g]` | Create and start an OpenClaw instance. `datadir` defaults to `~/.clawcu/<name>`. Host port defaults to `18789` and probes by `+10` on conflict. ClawCU automatically configures Gateway defaults, waits for health readiness, and prints the Dashboard URL. |
| `clawcu list [--running] [--managed\|--local] [--agents]` | List instances or agent views. By default, `list` shows both local `~/.openclaw` and managed instances. |
| `clawcu inspect <name>` | Show detailed instance state, including Docker container information, history, and snapshot summaries. |
| `clawcu token <name>` | Print the instance Dashboard token. |
| `clawcu start <name>` | Start a stopped instance. |
| `clawcu stop <name>` | Stop a running instance. |
| `clawcu restart <name>` | Restart an instance. |
| `clawcu remove <name> [--keep-data\|--delete-data]` | Remove the instance and container, with a choice to keep or delete the data directory. |

## 3. Failure Recovery

| Command | Description |
|------|------|
| `clawcu retry <name>` | Retry an instance that is stuck in `create_failed`. |
| `clawcu recreate <name>` | Recreate an instance container from saved configuration while keeping the original instance settings. |

## 4. Version Management

| Command | Description |
|------|------|
| `clawcu upgrade <name> --version <version>` | Upgrade an instance to a new version. ClawCU snapshots the data directory and env file before upgrading, waits for readiness, and attempts automatic rollback on failure. |
| `clawcu rollback <name>` | Roll back an instance to the previous version by restoring the matching snapshot and env snapshot. |

## 5. Experimentation and Cloning

| Command | Description |
|------|------|
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>]` | Copy the source instance data directory and env file into a new isolated experiment instance. |

## 6. Access and Configuration

| Command | Description |
|------|------|
| `clawcu approve <name> [requestId]` | Approve a pending browser pairing request. If no `requestId` is provided, ClawCU approves the latest pending request. |
| `clawcu config <name> [-- args...]` | Pass through to `openclaw configure` inside the managed container. Additional arguments are passed after `--`, for example: `clawcu config my-instance -- --section model`. |
| `clawcu exec <name> <command...>` | Run an arbitrary command inside the managed instance container. |
| `clawcu tui <name> [--agent <agent>]` | Launch OpenClaw TUI. ClawCU automatically handles the common local approve flow before entering TUI. |
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE...] [--apply]` | Write environment variables into the instance env file, with optional immediate `recreate`. |
| `clawcu getenv <name>` | Print the instance env file content. |
| `clawcu unsetenv <name> KEY [KEY...] [--apply]` | Remove environment variables from the instance env file, with optional immediate `recreate`. |
| `clawcu logs <name> [--follow]` | Show instance logs. `--follow` keeps streaming them. |

## 7. Model Configuration Collection and Reuse

| Command | Description |
|------|------|
| `clawcu provider collect --all` | Collect enabled model/provider configuration from all managed instances and local `~/.openclaw`. |
| `clawcu provider collect --instance <name>` | Collect model/provider configuration from one managed instance. |
| `clawcu provider collect --path <openclaw-home>` | Collect model/provider configuration from any OpenClaw data directory. |
| `clawcu provider list` | List collected providers, including masked API key summaries. |
| `clawcu provider show <name>` | Show the collected `auth-profiles.json` and `models.json` for one provider, with secrets masked in display output. |
| `clawcu provider remove <name>` | Remove a collected provider directory. |
| `clawcu provider models list <name>` | List the models stored in a collected provider. |
| `clawcu provider apply <provider> <instance> [--agent <agent>] [--primary <model>] [--fallbacks <m1,m2>] [--persist]` | Apply a collected provider to the selected instance agent. `--agent` defaults to `main`. |

## 8. Default Behavior Conventions

- Port:
  Default is `18789` for the OpenClaw Gateway host mapping. On conflict, ClawCU probes `18789 -> 18799 -> 18809 ...`.
- Resources:
  Default is `1 CPU + 2GB RAM`.
- Auth:
  Token mode is enforced because current OpenClaw `lan` binding requires authentication.
- Data directory:
  Default is `~/.clawcu/<instance-name>`.
- Container naming:
  `clawcu-openclaw-<instance-name>`.
- Local image tag:
  `clawcu/openclaw:<version>`.
- Gateway bootstrap:
  ClawCU automatically writes `bind=lan`, `auth.mode=token`, and `controlUi.allowedOrigins=["*"]`.
- Health waiting:
  ClawCU polls `http://127.0.0.1:<port>/healthz` until the instance is really ready or fails.
- Dashboard URL:
  After successful creation, upgrade, rollback, clone, or recreate, ClawCU prints `http://127.0.0.1:<port>/#token=<token>`.
- Environment variables:
  Instance env files live in `~/.clawcu/instances/<instance>.env`, and `recreate`, `upgrade`, `rollback`, and `clone` explicitly handle them.
- Upgrade strategy:
  The recommended pattern is: `clone` first, `upgrade` on the clone, validate, then `rollback` if needed.

## 9. Notes

- This usage guide describes the actual command surface for `v0.1.0`.
- For release context, see [RELEASE_v0.1.0.md](RELEASE_v0.1.0.md).
