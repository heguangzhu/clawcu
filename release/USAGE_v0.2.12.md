# ClawCU Usage v0.2.12

🌐 Language:
[English](USAGE_v0.2.12.md) | [中文](USAGE_v0.2.12.zh-CN.md)

Release Scope: `v0.2.12`

Command reference for `ClawCU v0.2.12`. Covers the shared command surface, OpenClaw / Hermes service differences, the orphan-instance lifecycle, the runtime-image override flow from `v0.2.11`, and the `v0.2.12` addition of `clawcu list --no-cache` for a one-shot fresh Available Versions refresh.

## 1. Setup and Artifact Preparation

### `clawcu --version`

```bash
clawcu --version
```

Show the installed ClawCU version.

### `clawcu setup`

```bash
clawcu setup [--completion]
```

Check Docker CLI access, Docker daemon reachability, ClawCU home, and runtime directories. Interactively configures the default ClawCU home, the OpenClaw image repo, and the Hermes image repo.

### `clawcu pull openclaw`

```bash
clawcu pull openclaw --version <version>
```

Prepare the official OpenClaw image reference for the requested version. If the image is missing locally, Docker pulls it when a later `create` / `start` / `recreate` needs it.

### `clawcu pull hermes`

```bash
clawcu pull hermes --version <tag>
```

Pull the prebuilt Hermes image for the requested tag from the configured Hermes image repo.

## 2. Instance Creation

### `clawcu create openclaw`

```bash
clawcu create openclaw --name <name> --version <version>
                       [--image <ref>]
                       [--datadir <path>] [--port <port>]
                       [--cpu 1] [--memory 2g]
```

- `--version <version>` — required logical version label stored on the instance record
- `--image <ref>` — optional runtime image override. Docker starts this image while `--version` remains the recorded OpenClaw version
- `datadir` defaults to `~/.clawcu/<name>`
- managed host port defaults to `18799`; probes by `+10` on conflict
- writes `.clawcu-instance.json` into the datadir so the instance is recoverable from its datadir alone

### `clawcu create hermes`

```bash
clawcu create hermes --name <name> --version <ref>
                     [--image <ref>]
                     [--datadir <path>] [--port <port>]
                     [--cpu 1] [--memory 2g]
```

- `--version <ref>` — required logical version / ref stored on the instance record
- `--image <ref>` — optional runtime image override. Docker starts this image while `--version` remains the recorded Hermes version
- `datadir` defaults to `~/.clawcu/<name>`
- managed API port defaults to `8652`; managed dashboard port starts from `9129`
- both probe by `+10` on conflict
- writes the same `.clawcu-instance.json` sidecar

## 3. Shared Lifecycle Commands

### `clawcu list` _(alias: `ls`)_

```bash
clawcu list [--source managed|local|removed|all]
            [--local] [--managed] [--all] [--removed]
            [--service X] [--status X] [--running]
            [--agents] [--wide] [--reveal]
            [--remote/--no-remote] [--no-cache] [--json]
```

List instance summaries or per-agent rows. Default source is `managed`.

- `--local` / `--managed` / `--all` / `--removed` — source shortcuts; conflicting combinations are rejected with a one-line error
- `--removed` — list orphan datadirs under `CLAWCU_HOME` whose records no longer exist
- `--agents` — one row per agent instead of per instance
- `--wide` — adds SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT columns on top of the narrow 6-column view
- `--reveal` — unmasks the dashboard token fragment
- `--remote` / `--no-remote` — toggle the human-only "Available versions" footer registry fetch (default on)
- `--no-cache` — bypass today's `<clawcu_home>/cache/available_versions.json` entry and force a fresh footer fetch; successful results still refresh the cache on disk
- footer behavior:
  - shows the top 10 stable releases per service (OpenClaw, Hermes), newest first
  - prereleases (`-beta`, `-rc`, `-alpha`) are filtered
  - omitted in `--json` / `--agents` / `--removed` views
  - falls back to local Docker images when the registry is unreachable or `--no-remote` is set
- `--json` — script-friendly instance array (versions footer is text-mode only)

### `clawcu inspect`

```bash
clawcu inspect <name> [--show-history] [--reveal]
```

Compact readable view of instance state (summary / access / snapshots / container / history). History is folded by default.

### `clawcu start`

```bash
clawcu start <name>
```

Start a stopped managed instance.

### `clawcu stop`

```bash
clawcu stop <name> [--time N | -t N]
```

Stop a running managed instance. `--time` is the graceful shutdown window in seconds (default `5`), passed to `docker stop --time`.

### `clawcu restart`

```bash
clawcu restart <name> [--no-recreate-if-config-changed]
```

Restart a managed instance. Default behavior promotes the action to a full `recreate` if env drift is detected or the container is missing.

### `clawcu recreate`

```bash
clawcu recreate <name> [--fresh] [--timeout N]
                       [--version <v>] [--yes]
```

Recreate an instance from saved configuration, or recover a removed instance from its leftover datadir. Auto-retries instances in `create_failed`.

- `--fresh` — wipes the instance datadir before recreating
- `--timeout N` — graceful stop window before force-remove
- `--version <v>` — used when recovering a pre-metadata orphan whose datadir does not carry `.clawcu-instance.json`
- existing managed instances always reuse their saved runtime image

### `clawcu upgrade`

```bash
clawcu upgrade <name> [--version <v>] [--list-versions]
                      [--image <ref>]
                      [--remote/--no-remote] [--all-versions]
                      [--dry-run] [--yes] [--json]
```

Upgrade to a new service version. Snapshots the instance datadir and the matching env path before replacing the container.

- `--version <v>` — required target version unless `--list-versions` is passed
- `--image <ref>` — optional runtime image override
- `--list-versions` — show candidate versions from history, local Docker images, and optionally remote registries
- `--no-remote` — skip the registry fetch entirely
- `--all-versions` — show the full remote tag list
- `--dry-run` / `--yes` / `--json` — planning, non-interactive confirm, and machine output paths
- the selected runtime image is persisted, so later `recreate`, orphan recovery, and `rollback` continue using the recorded image chain

### `clawcu rollback`

```bash
clawcu rollback <name> [--to <version>] [--list]
                       [--dry-run] [--yes] [--json]
```

Roll an instance back to an earlier snapshot. Without `--to`, restores the most recent reversible transition.

### `clawcu clone`

```bash
clawcu clone <source> --name <name>
                      [--datadir <path>] [--port <port>]
                      [--version <v>]
                      [--include-secrets/--exclude-secrets]
```

Copy a source instance into a new isolated experiment instance.

### `clawcu logs`

```bash
clawcu logs <name> [--follow] [--tail N] [--since DURATION]
```

Show instance logs. Defaults to the last 200 lines.

### `clawcu remove` _(alias: `rm`)_

```bash
clawcu remove <name> [--keep-data|--delete-data]
                     [--removed] [--yes]
```

Remove a managed instance. Default keeps the datadir.

- `--delete-data` — also delete the datadir
- `--removed` — permanently delete an orphan datadir listed by `clawcu list --removed`

## 4. Orphan Instance Lifecycle

When an instance record is lost, its datadir becomes an orphan — still on disk under `CLAWCU_HOME`, but no longer tracked.

| Step | Command | Description |
|------|---------|-------------|
| Discover | `clawcu list --removed` | Enumerate orphan datadirs and the service / version / port recovered from `.clawcu-instance.json`. |
| Recover | `clawcu recreate <orphan>` | Rebuild the managed instance from the orphan datadir. |
| Recover (pre-metadata) | `clawcu recreate <orphan> --version <v>` | Rebuild a datadir that predates `.clawcu-instance.json`. |
| Permanently delete | `clawcu remove <orphan> --removed [--yes]` | Wipe the orphan datadir. |

## 5. Interactive Access and Native Commands

### `clawcu config`

```bash
clawcu config <name> [-- args...]
```

Run the service-native configuration flow inside the managed container. OpenClaw maps to `openclaw configure`; Hermes maps to `hermes setup`.

### `clawcu exec`

```bash
clawcu exec <name> <command...>
```

Run an arbitrary command inside the managed container with the instance env injected.

### `clawcu tui`

```bash
clawcu tui <name> [--agent <agent>]
```

Launch the native interactive flow. OpenClaw uses its TUI; Hermes uses its interactive chat flow.

## 6. Service-Specific Access Commands

### `clawcu token` _(OpenClaw only)_

```bash
clawcu token <name> [--copy] [--url-only|--token-only] [--json]
```

Print the OpenClaw dashboard token. Hermes instances fail with a hint pointing at `clawcu config <name>`.

### `clawcu approve` _(OpenClaw only)_

```bash
clawcu approve <name> [requestId]
```

Approve a pending OpenClaw browser pairing request. Hermes instances fail with an unsupported message.

## 7. Environment Variable Management

### `clawcu setenv`

```bash
clawcu setenv <name> KEY=VALUE [KEY=VALUE ...]
                     [--from-file <path>]
                     [--dry-run] [--reveal] [--apply]
```

Write environment variables into the instance env file.

### `clawcu getenv`

```bash
clawcu getenv <name> [--reveal] [--json]
```

Print the current environment variables configured for the instance.

### `clawcu unsetenv`

```bash
clawcu unsetenv <name> KEY [KEY ...]
                       [--dry-run] [--reveal] [--apply]
```

Remove environment variables.

## 8. Model Configuration Collection and Reuse

### `clawcu provider collect`

```bash
clawcu provider collect (--all | --instance <name> | --path <home>)
```

Collect model configuration assets.

### `clawcu provider list`

```bash
clawcu provider list
```

List collected model configuration assets with service identity and masked API key summaries.

### `clawcu provider show`

```bash
clawcu provider show <name>
```

Show the stored payload for one collected asset. Use `openclaw:<name>` or `hermes:<name>` to disambiguate cross-service collisions.

### `clawcu provider remove`

```bash
clawcu provider remove <name>
```

Remove a collected asset.

### `clawcu provider models list`

```bash
clawcu provider models list <name>
```

List the models stored in a collected asset.

### `clawcu provider apply`

```bash
clawcu provider apply <provider> <instance>
                      [--agent <agent>]
                      [--primary <model>]
                      [--fallbacks <m1,m2>]
                      [--persist]
```

Apply a collected asset to the selected instance.

## 9. Default Behavior Conventions

- Port defaults:
  - OpenClaw managed instances start from `18799`
  - Hermes managed API ports start from `8652`
  - Hermes managed dashboard ports start from `9129`
  - all probe by `+10` on conflict
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
  - Hermes displays its dashboard port and dashboard URL
- Env location:
  - OpenClaw uses `~/.clawcu/instances/<instance>.env`
  - Hermes uses `<datadir>/.env`
- Snapshot behavior:
  - `upgrade` and `rollback` snapshot and restore both the instance home and the matching env path
- Record compatibility:
  - saved instance records tolerate unknown legacy keys during load

## 10. Notes

- This usage guide describes the command surface for `v0.2.12`.
- For release context, see [RELEASE_v0.2.12.md](RELEASE_v0.2.12.md).
- Shortcut: [USAGE_latest.md](USAGE_latest.md) always points at the current release.
