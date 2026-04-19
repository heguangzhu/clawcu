# ClawCU Usage v0.2.8

🌐 Language:
[English](USAGE_v0.2.8.md) | [中文](USAGE_v0.2.8.zh-CN.md)

Release Scope: `v0.2.8`

This document is the command reference for `ClawCU v0.2.8`.

It describes the shared command surface, the service-specific differences between OpenClaw and Hermes, the orphan-instance lifecycle introduced in `v0.2.6`, and the operational defaults ClawCU applies.

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
| `clawcu create openclaw --name <name> --version <version> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | Create and start an OpenClaw instance. `datadir` defaults to `~/.clawcu/<name>`. Managed host port defaults to `18799` and probes by `+10` on conflict. A `.clawcu-instance.json` metadata sidecar is written into the datadir so the instance is recoverable from its datadir alone. |
| `clawcu create hermes --name <name> --version <ref> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | Create and start a Hermes instance. `datadir` defaults to `~/.clawcu/<name>`. Managed API port defaults to `8652`; ClawCU also allocates a managed dashboard port starting from `9129`. Both probe by `+10` on conflict. The same `.clawcu-instance.json` sidecar is written. |

## 3. Shared Lifecycle Commands

| Command | Description |
|------|------|
| `clawcu list [--source managed\|local\|removed\|all] [--local] [--managed] [--all] [--removed] [--service X] [--status X] [--running] [--agents] [--wide] [--reveal] [--json]` | Alias `ls`. List instance summaries or per-agent rows. Default source is `managed`. `--local` / `--managed` / `--all` / `--removed` are source shortcuts; ClawCU rejects conflicting combinations with a one-line error. `--removed` lists orphaned datadirs under `CLAWCU_HOME` whose instance records no longer exist — each entry shows the persisted service / version / port recovered from `.clawcu-instance.json` when available (pre-`v0.2.6` orphans show `-` for fields the old layout could not persist). Narrow mode shows 6 columns; `--wide` adds SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT. `--reveal` unmasks the dashboard token fragment. |
| `clawcu inspect <name> [--show-history] [--reveal]` | Show detailed instance state as a compact readable view (summary / access / snapshots / container / history). History is folded by default — pass `--show-history` to expand, or `clawcu --json inspect <name>` for the full raw payload. `--reveal` shows the dashboard token unmasked. |
| `clawcu start <name>` | Start a stopped managed instance. |
| `clawcu stop <name> [--time N / -t N]` | Stop a running managed instance. `--time` is the graceful shutdown window (default 5s), passed to `docker stop --time`. |
| `clawcu restart <name> [--no-recreate-if-config-changed]` | Restart a managed instance. **Default ON**: if env drift is detected or the container is missing, ClawCU promotes the restart to a full `recreate`. Pass `--no-recreate-if-config-changed` to force a plain `docker restart`. |
| `clawcu recreate <name> [--fresh] [--timeout N] [--version <v>] [--yes]` | Recreate an instance from saved configuration, or recover a removed instance from its leftover datadir. Auto-retries instances in `create_failed`. `--fresh` wipes the instance datadir before recreating (destructive; prompts unless `--yes`). `--timeout` overrides the graceful stop window before force-remove. `--version <v>` is used when recovering a pre-metadata orphan whose datadir does not carry `.clawcu-instance.json`. |
| `clawcu upgrade <name> [--version <v>] [--list-versions] [--remote/--no-remote] [--all-versions] [--dry-run] [--yes] [--json]` | Upgrade to a new service version. Snapshots the instance home plus the matching env path before replacing the container. `--list-versions` shows candidate versions: instance history, local Docker images, and (with `--remote`, default on) registry release tags via the Docker Registry v2 API. Remote fetches are best-effort; failures fall back to local. `--no-remote` skips the registry. Remote section is truncated to the 10 most recent tags by default; pass `--all-versions` for the full list. `--json` always returns every tag. `--dry-run` prints the plan without touching Docker or disk. The normal path renders the plan then prompts — pass `--yes` / `-y` to skip (required non-interactively). |
| `clawcu rollback <name> [--to <version>] [--list] [--dry-run] [--yes] [--json]` | Roll an instance back to an earlier snapshot. Without `--to`, restores the most recent reversible transition. `--to <version>` picks the most recent history event whose "restores to" equals that version. `--list` enumerates every snapshot target without touching Docker. `--dry-run` / `--yes` / `--json` behave as in `upgrade`. |
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>] [--version <v>] [--include-secrets/--exclude-secrets]` | Copy a source instance into a new isolated experiment instance. The source's data directory is always copied. By default the source's env file (API keys / tokens / provider secrets) is ALSO copied — pass `--exclude-secrets` to start with an empty env. `--version <v>` switches the clone to a different service version at copy time (safe "clone then upgrade"). |
| `clawcu logs <name> [--follow] [--tail N] [--since DURATION]` | Show instance logs. Defaults to the last 200 lines. `--follow` keeps streaming; `--tail 0` streams the full history. |
| `clawcu remove <name> [--keep-data\|--delete-data] [--removed] [--yes]` | Alias `rm`. Remove a managed instance (default: keep datadir). Pass `--removed` to permanently delete an orphaned leftover listed by `clawcu list --removed` — in that mode `--keep-data` / `--delete-data` are rejected, since `--removed` always deletes. |

## 4. Orphan Instance Lifecycle

When an instance record is lost (registry corruption, restored backup, aborted `create` that left state behind), its datadir becomes an "orphan" — still on disk under `CLAWCU_HOME`, but no longer tracked. `v0.2.8` provides a complete recovery path:

| Step | Command | Description |
|------|---------|-------------|
| Discover | `clawcu list --removed` | Enumerate orphan datadirs and the service / version / port recovered from their `.clawcu-instance.json` sidecar. |
| Recover | `clawcu recreate <orphan>` | Rebuild the managed instance from the orphan datadir. Port / version / service are restored from metadata. |
| Recover (pre-metadata) | `clawcu recreate <orphan> --version <v>` | Rebuild a datadir that predates `.clawcu-instance.json` (created on `v0.2.5` or earlier). Use `--version` to pin the target service version explicitly. |
| Permanently delete | `clawcu remove <orphan> --removed [--yes]` | Wipe the orphan datadir. `--keep-data` / `--delete-data` are rejected in this mode. |

`clawcu recreate` and `clawcu upgrade` started in `v0.2.8` always refresh `.clawcu-instance.json`, so a recovered instance re-enters the "self-describing" state automatically.

## 5. Interactive Access and Native Commands

| Command | Description |
|------|------|
| `clawcu config <name> [-- args...]` | Run the service-native configuration flow inside the managed container. OpenClaw maps to `openclaw configure`; Hermes maps to `hermes setup`. |
| `clawcu exec <name> <command...>` | Run an arbitrary command inside the managed container with the instance env injected. |
| `clawcu tui <name> [--agent <agent>]` | Launch the native interactive flow. OpenClaw uses its TUI flow; Hermes uses its interactive chat flow. |

## 6. Service-Specific Access Commands

| Command | Description |
|------|------|
| `clawcu token <name> [--copy] [--url-only\|--token-only] [--json]` | Print the OpenClaw dashboard token. Default shows both the token and the access URL with the `#token=…` anchor. `--copy` pushes the token into the system clipboard (pbcopy/xclip/wl-copy/clip). `--url-only`/`--token-only` are scripting-friendly shortcuts. Hermes instances fail with a hint directing you to `clawcu config <name>` (native auth). |
| `clawcu approve <name> [requestId]` | Approve a pending OpenClaw browser pairing request. OpenClaw-only. Hermes instances fail with an unsupported message. |

## 7. Environment Variable Management

| Command | Description |
|------|------|
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE ...] [--from-file <path>] [--dry-run] [--reveal] [--apply]` | Write environment variables into the instance env file. Inline `KEY=VALUE` args and `--from-file <path>` are mutually exclusive. `--dry-run` prints a colored `+/-/~` diff; sensitive values (`KEY`/`TOKEN`/`SECRET`/`PASSWORD`) are masked unless `--reveal` is passed. `--apply` immediately recreates the instance so Docker reloads the env file. |
| `clawcu getenv <name> [--reveal] [--json]` | Print the current environment variables configured for the instance. Sensitive values are masked unless `--reveal` is passed. |
| `clawcu unsetenv <name> KEY [KEY ...] [--dry-run] [--reveal] [--apply]` | Remove environment variables. `--dry-run` previews which keys would be removed (and flags keys not present as no-ops). `--apply` immediately recreates the instance. |

## 8. Model Configuration Collection and Reuse

| Command | Description |
|------|------|
| `clawcu provider collect --all` | Collect model configuration assets from all ClawCU-managed instances plus local `~/.openclaw` and `~/.hermes` when present. |
| `clawcu provider collect --instance <name>` | Collect model configuration from one managed instance. |
| `clawcu provider collect --path <home>` | Collect model configuration from any OpenClaw or Hermes home directory. |
| `clawcu provider list` | List collected model configuration assets with service identity and masked API key summaries. |
| `clawcu provider show <name>` | Show the stored payload for one collected asset (secrets masked). Use `openclaw:<name>` or `hermes:<name>` to disambiguate. |
| `clawcu provider remove <name>` | Remove a collected asset. |
| `clawcu provider models list <name>` | List the models stored in a collected asset. |
| `clawcu provider apply <provider> <instance> [--agent <agent>] [--primary <model>] [--fallbacks <m1,m2>] [--persist]` | Apply a collected asset to the selected instance. `--agent` defaults to `main`. Writeback is service-native. |

## 9. Default Behavior Conventions

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
- Datadir metadata:
  - every instance carries `.clawcu-instance.json` with service / version / port / created-at
  - enables orphan recovery without user input
- Access info:
  - both services expose an access URL in `create`, `list`, and `inspect`
  - OpenClaw displays its main service port and dashboard URL
  - Hermes displays its dashboard port and dashboard URL; readiness may also use the API server
- Env location:
  - OpenClaw uses `~/.clawcu/instances/<instance>.env`
  - Hermes uses `<datadir>/.env`
- Snapshot behavior:
  - `upgrade` and `rollback` snapshot and restore both the instance home and the matching env path
- Recommended upgrade strategy:
  - `clone` first, `upgrade` on the clone, validate, then `rollback` if needed
- Orphan recovery:
  - `list --removed` → `recreate <orphan>` (port / version auto-restored from `.clawcu-instance.json`)

## 10. Notes

- This usage guide describes the command surface for `v0.2.8`.
- For release context, see [RELEASE_v0.2.8.md](RELEASE_v0.2.8.md).
- Archived `v0.2.0` usage remains available in [USAGE_v0.2.0.md](USAGE_v0.2.0.md); `v0.1.0` in [USAGE_v0.1.0.md](USAGE_v0.1.0.md).
