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
| `clawcu setup [--completion]` | Check Docker CLI access, Docker daemon reachability, ClawCU home, runtime directories, and interactively configure the default ClawCU home, the OpenClaw image repo, and the Hermes image repo. |
| `clawcu pull openclaw --version <version>` | Prepare the official OpenClaw image reference for the requested version. If the image is missing locally, Docker pulls it when a later `create`, `start`, or `recreate` needs it. |
| `clawcu pull hermes --version <tag>` | Pull the prebuilt Hermes image for the requested tag from the configured Hermes image repo. |

## 2. Instance Creation

| Command | Description |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | Create and start an OpenClaw instance. `datadir` defaults to `~/.clawcu/<name>`. Managed host port defaults to `18799` and probes by `+10` on conflict so it does not occupy the local OpenClaw default port. |
| `clawcu create hermes --name <name> --version <ref> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | Create and start a Hermes instance. `datadir` defaults to `~/.clawcu/<name>`. Managed API port defaults to `8652`; ClawCU also allocates a managed dashboard port starting from `9129`. Both probe by `+10` on conflict. |

## 3. Shared Lifecycle Commands

| Command | Description |
|------|------|
| `clawcu list [--source managed\|local\|all] [--service X] [--status X] [--running] [--agents] [--wide] [--reveal]` | List instance summaries or per-agent rows. Default source is `managed` (local pseudo-entries under ~/.openclaw / ~/.hermes are hidden unless `--source local` or `--source all` is passed). Narrow mode shows 6 columns (NAME / SERVICE / VERSION / PORT / STATUS / ACCESS host:port); `--wide` adds SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT and full access URLs. `--reveal` prints the dashboard token fragment. |
| `clawcu inspect <name> [--show-history] [--reveal]` | Show detailed instance state as a compact readable view (summary / access / snapshots / container / history). History is folded by default — pass `--show-history` to expand, or `clawcu --json inspect <name>` for the full raw payload. `--reveal` shows the dashboard token unmasked. |
| `clawcu start <name>` | Start a stopped managed instance. |
| `clawcu stop <name> [--time N / -t N]` | Stop a running managed instance. `--time` is the graceful shutdown window in seconds passed through to `docker stop --time` (default 5s). Raise it to let long OpenClaw/Hermes tasks finish before SIGKILL. |
| `clawcu restart <name> [--no-recreate-if-config-changed]` | Restart a managed instance. **Default ON**: ClawCU inspects the live container and, if env drift is detected (e.g. after `setenv` without `--apply`) or the container is missing, promotes the restart to a full `recreate` so the new env file takes effect — matching how `clawcu start` already behaves. Pass `--no-recreate-if-config-changed` to force a plain `docker restart` even when drift is detected. |
| `clawcu recreate <name>` | Recreate an instance container from saved configuration. Automatically retries instances stuck in `create_failed`. |
| `clawcu upgrade <name> [--version <v>] [--list-versions] [--remote/--no-remote] [--dry-run] [--yes] [--json]` | Upgrade an instance to a new service version or tag. ClawCU snapshots the instance home plus the matching env path before replacing the container, and the env file is preserved across the upgrade. `--list-versions` shows candidate versions without requiring `--version`: it always shows this instance's version history and the local Docker images for the service's image repo, and — with `--remote` (default on) — also queries the configured image registry (GHCR / Docker Hub) for release tags via the Docker Registry v2 API. Remote fetches are best-effort: network/auth failures fall back to the local view and print a warning line instead of crashing. Pass `--no-remote` to skip the registry query entirely (useful in CI or offline). `--dry-run` prints the upgrade plan (current → target, datadir, env carry-over summary, projected image tag, snapshot path) without touching Docker or disk. The normal path renders the plan first and then asks for confirmation — pass `--yes` / `-y` to skip the prompt (required in non-interactive shells). `--json` emits the plan / version list as JSON. |
| `clawcu rollback <name> [--to <version>] [--list] [--dry-run] [--yes] [--json]` | Roll an instance back to an earlier snapshot. Without `--to`, restores the most recent reversible transition. `--to <version>` picks the most recent history event whose "restores to" equals that version. `--list` enumerates every snapshot target (action, restore version, snapshot path, whether the snapshot still exists on disk) without touching Docker. `--dry-run` renders the rollback plan (current → target, datadir, env restore, source event, projected image, safety snapshot path) without touching Docker or disk. The normal path renders the plan first, then asks for confirmation — pass `--yes` / `-y` to skip the prompt (required in non-interactive shells). `--json` emits the plan / target list as JSON. |
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>]` | Copy a source instance into a new isolated experiment instance. |
| `clawcu logs <name> [--follow] [--tail N] [--since DURATION]` | Show instance logs. Defaults to the last 200 lines. `--follow` keeps streaming them; `--tail 0` streams the full history. |
| `clawcu remove <name> [--keep-data\|--delete-data]` | Remove the instance and container, with a choice to keep or delete the data directory. |

## 4. Interactive Access and Native Commands

| Command | Description |
|------|------|
| `clawcu config <name> [-- args...]` | Run the service-native configuration flow inside the managed container. OpenClaw maps to `openclaw configure`; Hermes maps to `hermes setup`. |
| `clawcu exec <name> <command...>` | Run an arbitrary command inside the managed container with the instance env injected. |
| `clawcu tui <name> [--agent <agent>]` | Launch the native interactive flow for the instance. OpenClaw uses its TUI flow; Hermes uses its interactive chat flow. |

## 5. Service-Specific Access Commands

| Command | Description |
|------|------|
| `clawcu token <name> [--copy] [--url-only\|--token-only] [--json]` | Print the OpenClaw dashboard token. Default shows both the token and the access URL with the `#token=…` anchor. `--copy` pushes the token into the system clipboard (pbcopy/xclip/wl-copy/clip). `--url-only`/`--token-only` are scripting-friendly shortcuts. Hermes instances fail with a hint directing you to `clawcu config <name>` (native auth). |
| `clawcu approve <name> [requestId]` | Approve a pending OpenClaw browser pairing request. This is currently OpenClaw-only. Hermes instances fail with a clear unsupported message. |

## 6. Environment Variable Management

| Command | Description |
|------|------|
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE ...] [--from-file <path>] [--dry-run] [--reveal] [--apply]` | Write environment variables into the instance env file. Inline `KEY=VALUE` args and `--from-file <path>` (a `.env`-style file with `KEY=VALUE` lines; `#` comments and blanks are skipped) are mutually exclusive. `--dry-run` prints a colored `+/-/~` diff against the current env without writing — sensitive values (`KEY`/`TOKEN`/`SECRET`/`PASSWORD`) are masked unless `--reveal` is passed. `--apply` immediately recreates the instance so Docker reloads the env file (cannot be combined with `--dry-run`). |
| `clawcu getenv <name> [--reveal] [--json]` | Print the current environment variables configured for the instance. Sensitive values are masked unless `--reveal` is passed. |
| `clawcu unsetenv <name> KEY [KEY ...] [--dry-run] [--reveal] [--apply]` | Remove environment variables from the instance env file. `--dry-run` previews which keys would be removed (and lists keys not present as no-ops) without writing. `--apply` immediately recreates the instance (cannot be combined with `--dry-run`). |

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
  - OpenClaw managed instances start from `18799`
  - Hermes managed API ports start from `8652`
  - Hermes managed dashboard ports start from `9129`
  - all probe by `+10` on conflict
- Resources:
  - default is `1 CPU + 2GB RAM`
- Data directory:
  - default is `~/.clawcu/<instance-name>`
- Container naming:
  - `clawcu-<service>-<instance-name>`
- Access info:
  - both services expose an access URL in `create`, `list`, and `inspect`
  - OpenClaw displays its main service port and dashboard URL
  - Hermes displays its dashboard port and dashboard URL, while readiness may also use the API server
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
