# ClawCU Usage v0.4.1

🌐 Language:
[English](USAGE_v0.4.1.md) | [中文](USAGE_v0.4.1.zh-CN.md)

Release Scope: `v0.4.1`

Command reference for `ClawCU v0.4.1`. Covers the shared command surface, OpenClaw / Hermes service differences, the orphan-instance lifecycle (introduced in `v0.2.6`), the `v0.2.9`–`v0.2.10` list-footer polish, the **`v0.4.2` A2A v0 surface** (`--a2a` opt-in at create time, `clawcu a2a` subcommand tree), the **`v0.4.1` provider commands** (`collect` / `list` / `show` / `apply` / `remove`), the **`v0.4.1` Dashboard** (persistent Docker container), and the operational defaults.

## 1. Setup and Artifact Preparation

### `clawcu --version`

```
clawcu --version
```

Show the installed ClawCU version.

### `clawcu setup`

```
clawcu setup [--completion]
```

Check Docker CLI access, Docker daemon reachability, ClawCU home, and runtime directories. Interactively configures the default ClawCU home, the OpenClaw image repo, and the Hermes image repo.

## 2. Instance Creation

### `clawcu create openclaw`

```
clawcu create openclaw --name <name> --version <version>
                       [--datadir <path>] [--port <port>]
                       [--cpu 1] [--memory 2g]
                       [--a2a]
```

Create and start an OpenClaw instance.

- `datadir` defaults to `~/.clawcu/<name>`
- managed host port defaults to `18799`; probes by `+10` on conflict
- writes `.clawcu-instance.json` into the datadir so the instance is recoverable from its datadir alone
- `--a2a` — bake the A2A sidecar into the image (see §11). Publishes an extra neighbor port for `GET /.well-known/agent-card.json` + `POST /a2a/send`. Stock behaviour unchanged; sidecar speaks A2A v0 via stdlib Node `http`

### `clawcu create hermes`

```
clawcu create hermes --name <name> --version <ref>
                     [--datadir <path>] [--port <port>]
                     [--cpu 1] [--memory 2g]
                     [--a2a]
```

Create and start a Hermes instance.

- `datadir` defaults to `~/.clawcu/<name>`
- managed API port defaults to `8652`; managed dashboard port starts from `9129`
- both probe by `+10` on conflict
- writes the same `.clawcu-instance.json` sidecar
- `--a2a` — bake the A2A sidecar into the image (see §11). Python stdlib sidecar on `A2A_BIND_PORT` (default 9119)

## 3. Shared Lifecycle Commands

### `clawcu list` _(alias: `ls`)_

```
clawcu list [--source managed|local|removed|all]
            [--local] [--managed] [--all] [--removed]
            [--service X] [--status X] [--running]
            [--agents] [--wide] [--reveal]
            [--versions] [--remote/--no-remote] [--no-cache]
            [--json]
```

List instance summaries or per-agent rows. Default source is `managed`.

- `--local` / `--managed` / `--all` / `--removed` — source shortcuts; conflicting combinations are rejected with a one-line error
- `--removed` — list orphan datadirs under `CLAWCU_HOME` whose records no longer exist. Each entry shows persisted service / version / port from `.clawcu-instance.json` (pre-`v0.2.6` orphans show `-` for fields the old layout could not persist)
- `--agents` — one row per agent instead of per instance
- `--wide` — adds SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT columns on top of the narrow 6-column view
- `--reveal` — unmasks the dashboard token fragment
- `--versions` — append a top-10 "Available versions" footer per service (OpenClaw, Hermes). Fetched from configured registries and cached per calendar day at `<clawcu_home>/cache/available_versions.json`
- `--remote` / `--no-remote` — when used with `--versions`, toggle the registry fetch (default on). Pass `--no-remote` for an offline view (CI, airgapped, slow networks)
- `--no-cache` — when used with `--versions`, bypass the local cache and fetch fresh remote tags
- `--json` — script-friendly instance array (contract unchanged; versions footer is text-mode only)

### `clawcu inspect`

```
clawcu inspect <name> [--show-history] [--reveal]
```

Compact readable view of instance state (summary / access / snapshots / container / history). History is folded by default.

- `--show-history` — expand the folded history section
- `clawcu --json inspect <name>` — full raw JSON payload
- `--reveal` — unmasks the dashboard token

### `clawcu start`

```
clawcu start <name>
```

Start a stopped managed instance.

### `clawcu stop`

```
clawcu stop <name> [--time N | -t N]
```

Stop a running managed instance. `--time` is the graceful shutdown window in seconds (default `5`), passed to `docker stop --time`.

### `clawcu restart`

```
clawcu restart <name> [--no-recreate-if-config-changed]
```

Restart a managed instance. **Default ON**: if env drift is detected or the container is missing, the restart is promoted to a full `recreate`.

- `--no-recreate-if-config-changed` — force a plain `docker restart`

### `clawcu recreate`

```
clawcu recreate <name> [--fresh] [--timeout N]
                       [--version <v>] [--yes]
```

Recreate an instance from saved configuration, or recover a removed instance from its leftover datadir. Auto-retries instances in `create_failed`.

- `--fresh` — wipes the instance datadir before recreating (destructive; prompts unless `--yes`)
- `--timeout N` — graceful stop window before force-remove
- `--version <v>` — used when recovering a pre-metadata orphan whose datadir does not carry `.clawcu-instance.json`

### `clawcu upgrade`

```
clawcu upgrade <name> [--version <v>] [--list-versions]
                      [--remote/--no-remote] [--all-versions]
                      [--dry-run] [--yes] [--json]
```

Upgrade to a new service version. Snapshots the instance datadir and the matching env path before replacing the container.

- `--list-versions` — show candidate versions: instance history, local Docker images, and (with `--remote`, default on) registry release tags via the Docker Registry v2 API. Best-effort; failures fall back to local
- `--no-remote` — skip the registry fetch entirely
- `--all-versions` — show the full remote tag list (default is truncated to the 10 most recent)
- `--json` — always returns every tag
- `--dry-run` — print the plan without touching Docker or disk
- `--yes` / `-y` — skip the plan confirmation prompt (required non-interactively)

### `clawcu rollback`

```
clawcu rollback <name> [--to <version>] [--list]
                       [--dry-run] [--yes] [--json]
```

Roll an instance back to an earlier snapshot. Without `--to`, restores the most recent reversible transition.

- `--to <version>` — pick the most recent history event whose "restores to" equals that version
- `--list` — enumerate every snapshot target without touching Docker
- `--dry-run` / `--yes` / `--json` — as in `upgrade`

### `clawcu clone`

```
clawcu clone <source> --name <name>
                      [--datadir <path>] [--port <port>]
                      [--version <v>]
                      [--include-secrets/--exclude-secrets]
```

Copy a source instance into a new isolated experiment instance. The source's datadir is always copied.

- `--include-secrets` (default) / `--exclude-secrets` — whether to copy the source env file (API keys / tokens / provider secrets). Default copies; pass `--exclude-secrets` to start with an empty env
- `--version <v>` — switch the clone to a different service version at copy time (safe "clone then upgrade")

### `clawcu logs`

```
clawcu logs <name> [--follow] [--tail N] [--since DURATION]
```

Show instance logs. Defaults to the last 200 lines.

- `--follow` — keep streaming
- `--tail 0` — stream full history
- `--since DURATION` — skip log entries older than DURATION

### `clawcu remove` _(alias: `rm`)_

```
clawcu remove <name> [--keep-data|--delete-data]
                     [--removed] [--yes]
```

Remove a managed instance. Default keeps the datadir.

- `--delete-data` — also delete the datadir
- `--removed` — permanently delete an orphan datadir listed by `clawcu list --removed`. In this mode `--keep-data` / `--delete-data` are rejected, since `--removed` always deletes
- Auto-prompt: when `remove <name>` is called on an instance whose record is already gone but whose datadir still exists, the CLI prompts to delete the orphan in one shot (no need to re-run with `--removed`)

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

### `clawcu config`

```
clawcu config <name> [-- args...]
```

Run the service-native configuration flow inside the managed container. OpenClaw maps to `openclaw configure`; Hermes maps to `hermes setup`.

### `clawcu exec`

```
clawcu exec <name> <command...>
```

Run an arbitrary command inside the managed container with the instance env injected.

### `clawcu tui`

```
clawcu tui <name> [--agent <agent>]
```

Launch the native interactive flow. OpenClaw uses its TUI; Hermes uses its interactive chat flow.

## 6. Dashboard

### `clawcu dashboard`

```
clawcu dashboard [--host HOST] [--port PORT]
                 [--open/--no-open]
                 [--stop] [--restart] [--status] [--rebuild]
```

Manage the ClawCU dashboard as a Docker container that stays running in the background.

- Default (no flags) — ensures the dashboard image exists (builds automatically on first run), starts the container if it is not running, then opens the browser
- `--stop` — stops and removes the dashboard container
- `--restart` — stops then starts the container again (useful after config changes)
- `--status` — prints container state, image tag, URL, and health
- `--rebuild` — forces a rebuild of the dashboard Docker image (use after upgrading ClawCU)
- `--host` / `--port` — control which local interface and port the dashboard is published on (default `127.0.0.1:8765`)

The dashboard container mounts the following host paths:
- `~/.clawcu` → container StateStore data
- `~/.openclaw` / `~/.hermes` → local instance detection
- `/var/run/docker.sock` → container introspection and logs

## 7. Service-Specific Access Commands

### `clawcu token` _(OpenClaw only)_

```
clawcu token <name> [--copy] [--url-only|--token-only] [--json]
```

Print the OpenClaw dashboard token. Default shows both the token and the access URL with the `#token=…` anchor. Hermes instances fail with a hint pointing at `clawcu config <name>` (native auth).

- `--copy` — push the token into the system clipboard (pbcopy / xclip / wl-copy / clip)
- `--url-only` / `--token-only` — scripting-friendly shortcuts

### `clawcu approve` _(OpenClaw only)_

```
clawcu approve <name> [requestId]
```

Approve a pending OpenClaw browser pairing request. Hermes instances fail with an unsupported message.

## 7. Environment Variable Management

### `clawcu setenv`

```
clawcu setenv <name> KEY=VALUE [KEY=VALUE ...]
                     [--from-file <path>]
                     [--dry-run] [--reveal] [--apply]
```

Write environment variables into the instance env file. Inline `KEY=VALUE` args and `--from-file <path>` are mutually exclusive.

- `--dry-run` — colored `+/-/~` diff preview
- `--reveal` — show sensitive values (default masks `KEY` / `TOKEN` / `SECRET` / `PASSWORD`)
- `--apply` — immediately recreate the instance so Docker reloads the env file

### `clawcu getenv`

```
clawcu getenv <name> [--reveal] [--json]
```

Print the current environment variables configured for the instance. Sensitive values are masked unless `--reveal` is passed.

### `clawcu unsetenv`

```
clawcu unsetenv <name> KEY [KEY ...]
                       [--dry-run] [--reveal] [--apply]
```

Remove environment variables.

- `--dry-run` — preview which keys would be removed (absent keys are flagged as no-ops)
- `--apply` — immediately recreate the instance

## 8. Model Configuration Collection and Reuse

### `clawcu provider collect`

```
clawcu provider collect (--all | --instance <name> | --path <home>)
```

Collect model configuration assets.

- `--all` — from every ClawCU-managed instance plus local `~/.openclaw` and `~/.hermes` when present
- `--instance <name>` — from one managed instance
- `--path <home>` — from any OpenClaw or Hermes home directory

### `clawcu provider list`

```
clawcu provider list
```

List collected model configuration assets with service identity and masked API key summaries.

### `clawcu provider show`

```
clawcu provider show <name>
```

Show the stored payload for one collected asset (secrets masked). Use `openclaw:<name>` or `hermes:<name>` to disambiguate cross-service collisions.

### `clawcu provider remove`

```
clawcu provider remove <name>
```

Remove a collected asset.

### `clawcu provider models list`

```
clawcu provider models list <name>
```

List the models stored in a collected asset.

### `clawcu provider apply`

```
clawcu provider apply <provider> <instance>
                      [--agent <agent>]
                      [--primary <model>]
                      [--fallbacks <m1,m2>]
                      [--persist]
```

Apply a collected asset to the selected instance. Writeback is service-native.

- `--agent` — target agent; defaults to `main`
- `--primary <model>` — set the primary model
- `--fallbacks <m1,m2>` — set the fallback model chain
- `--persist` — write the change to disk immediately

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

## 11. Agent-to-Agent Messaging (`v0.4.2`)

`v0.4.2` introduces an A2A v0 surface. All A2A behaviour is opt-in via `--a2a` at create time; existing instances are unaffected.

### Opt-in at create time

Pass `--a2a` to `clawcu create openclaw` or `clawcu create hermes` to bake the sidecar into the instance's image. The baked image is tagged `clawcu/{service}-a2a:{base}-plugin{clawcu_version}.{sha}`, where `sha` fingerprints the on-disk sidecar sources so editable installs transparently rebake when the sidecar code changes.

A running A2A-enabled instance exposes, on a neighbor port next to its native gateway:

- `GET /.well-known/agent-card.json` — `{name, role, skills, endpoint}`
- `POST /a2a/send` — `{"from": ..., "to": ..., "message": ...}` → `{"from": ..., "message": ...}`

Optional request field: `thread_id` (uuid v7). When present, the sidecar persists `{peer, message, timestamp}` as JSONL under `<datadir>/threads/<peer>.jsonl`; subsequent turns with the same `thread_id` get prior context prepended.

Operational knobs baked into the sidecar (no user-tuneable flags today):

- Per-peer token-bucket rate limit (keyed on the `from` field)
- Readiness probe — `/healthz` only flips to `ok` after the native gateway responds
- Log tee — sidecar stdout/stderr also written to `<datadir>/a2a-sidecar.log`

### `clawcu a2a card`

```
clawcu a2a card [--name <instance>] [--host 127.0.0.1]
```

Print the AgentCard JSON for a local clawcu instance (derived from its record). Omit `--name` to dump cards for every managed instance as a JSON array.

### `clawcu a2a registry serve`

```
clawcu a2a registry serve [--port 8765] [--host 127.0.0.1]
```

Run the aggregator: serves `GET /agents` (array) and `GET /agents/<name>` (single card) over HTTP, stdlib-only. One process for N instances.

### `clawcu a2a send`

```
clawcu a2a send --to <target> --message <text>
                [--registry http://127.0.0.1:8765]
                [--from clawcu-cli] [--timeout 60]
```

Look up `TARGET` in the registry and POST a message to its endpoint. Prints the reply JSON. `--timeout` is the LLM reply wait window.

### `clawcu hermes identity set`

```
clawcu hermes identity set <instance> <soul-path>
```

Install a user-authored `SOUL.md` into a Hermes instance's datadir. `prompt_builder.load_soul_md` picks up the new persona on the next chat turn — no restart, no recreate.

## 12. Notes

- This usage guide describes the command surface for `v0.4.1`.
- For release context, see [RELEASE_v0.4.1.md](RELEASE_v0.4.1.md).
- Shortcut: [USAGE_latest.md](USAGE_latest.md) always points at the current release.
