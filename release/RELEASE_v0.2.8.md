# ClawCU v0.2.8

🌐 Language:
[English](RELEASE_v0.2.8.md) | [中文](RELEASE_v0.2.8.zh-CN.md)

Release Date: April 19, 2026

> `v0.2.8` closes the orphan-instance lifecycle loop that `v0.2.0` could not express. When a managed instance's record is lost or deleted, its datadir is no longer a dead artifact on disk — ClawCU can now list it, rebuild it, or permanently clean it up, with the same safety story as normal instances.

* * *
## Highlights

- Orphan Instance Lifecycle
  - `clawcu list --removed` surfaces datadirs under `CLAWCU_HOME` whose instance records no longer exist.
  - `clawcu recreate <orphan> [--version <v>]` rebuilds a managed instance from leftover state — with full port/env/metadata recovery when a `.clawcu-instance.json` is present.
  - `clawcu remove <orphan> --removed` permanently deletes an orphan datadir from `list --removed`.

- Self-Describing Datadirs
  - Each instance now writes a `.clawcu-instance.json` into its datadir alongside runtime state.
  - On `list --removed`, ClawCU reads this metadata to recover the instance's service, version, and port — so orphan entries stop showing as `-` columns when the data is actually there.
  - Pre-metadata orphans (created on versions older than `v0.2.6`) still list correctly — they just show `-` for the fields the old layout could not persist.

- Safer Flag Semantics on `list` and `remove`
  - `list` now explicitly rejects conflicting flag combinations (`--local --removed`, `--source managed --removed`, `--source all --removed`, etc.) with a one-line error, instead of silently picking a winner.
  - `remove --removed` rejects `--delete-data` / `--keep-data` (which would be nonsensical under permanent deletion), instead of silently ignoring them.

- Better Error Hints
  - `clawcu remove <unknown> --removed` now hints "Run `clawcu list --removed` to see recoverable leftovers" instead of the generic "instance not found" suggestion.

- 355 Tests
  - Suite grew from 170+ at `v0.2.0` to 355 passing tests, covering orphan lifecycle edge cases, conflict-flag matrix, and metadata-backed recovery paths.

* * *
## The Orphan Lifecycle Problem

Before `v0.2.8`, if an instance record was lost — directly editing the registry, restoring a backup, or a failed `create` that left data behind — the datadir under `~/.clawcu/<name>` stayed on disk but was effectively invisible to ClawCU:

- it didn't show in `clawcu list`
- it couldn't be targeted by `clawcu recreate` / `upgrade` / `rollback`
- the only way to deal with it was `rm -rf`, which discarded any recoverable state

`v0.2.8` treats these datadirs as a first-class concept: orphans. They are not managed, but they are known.

### Discovery

```
clawcu list --removed
```

lists every datadir under `CLAWCU_HOME` whose name does not match a live record. Each entry reports its service (inferred from `.clawcu-instance.json` when present, or from the datadir layout), its persisted version, and its persisted port.

### Recovery

```
clawcu recreate <orphan>
```

rebuilds a managed instance from the orphan datadir. When `.clawcu-instance.json` is present (introduced in `v0.2.6`), ClawCU recovers the service, version, and port with zero user input — the restored instance picks up the same port it was using before the record was lost.

For pre-metadata orphans, the service/version inference is best-effort, and `--version <v>` is available to pin the target explicitly:

```
clawcu recreate <orphan> --version 2026.4.9
```

### Permanent Deletion

```
clawcu remove <orphan> --removed [--yes]
```

wipes the orphan datadir. `--removed` is the only path for permanently deleting a datadir that is not currently tracked, and it intentionally forbids combining with `--keep-data` / `--delete-data` — the flag's whole job is permanent deletion, so those qualifiers would be contradictory.

* * *
## `.clawcu-instance.json` — Self-Describing Datadirs

ClawCU now persists a small metadata sidecar inside each instance's datadir:

```
~/.clawcu/<instance>/.clawcu-instance.json
```

The file captures everything needed to reconstruct the instance reference later:

- service (`openclaw` / `hermes`)
- version / tag
- port
- created-at timestamp

This is the reason `v0.2.8` orphan recovery does not lose the port. The lifecycle layer writes the sidecar on `create` / `clone` / `upgrade` / `recreate`, so any datadir born on or after `v0.2.6` is self-describing.

Older datadirs (created before `v0.2.6`) do not have the sidecar. `list --removed` still shows them, just with `-` in the columns that could not be recovered, and `recreate` asks for `--version` explicitly.

* * *
## Incremental UX Polishing Since v0.2.0

`v0.2.8` ships a few small but load-bearing UX fixes that accumulated across the `v0.2.x` cycle:

### Explicit Flag Conflict Errors on `list`

`clawcu list` accepts several ways to pick a source: `--source`, `--local`, `--managed`, `--all`, `--removed`. Combining them incoherently used to resolve silently (with the last-written flag winning). It now prints a one-line error like:

> Error: --removed cannot be combined with --local/--managed/--all; drop one of them.

and exits non-zero.

### Explicit Rejection of Redundant Flags on `remove --removed`

Because `--removed` means "permanently delete this orphan datadir," pairing it with `--keep-data` (preserve data) or even `--delete-data` (also delete, but for a tracked instance) is meaningless. `v0.2.8` rejects these combinations with an error instead of silently ignoring them.

### Hint Routing for Missing Instances

The generic "Instance 'X' was not found" hint previously suggested `clawcu list` even when the user was targeting an orphan. It now recognizes the "Removed instance 'X' was not found" shape and directs the user to `clawcu list --removed` instead.

* * *
## Compatibility

`v0.2.8` is a drop-in upgrade from `v0.2.0`, `v0.2.6`, and `v0.2.7`.

- Existing managed instances keep working without migration.
- Running `create` / `clone` / `upgrade` / `recreate` at `v0.2.8` writes `.clawcu-instance.json` into the datadir — subsequent orphan recovery benefits automatically.
- Older datadirs without metadata still recover; they just need `--version <v>` on `recreate`.

There are no breaking CLI changes.

* * *
## Recommended Workflow

The recommendation from `v0.2.0` still stands — clone, upgrade on clone, validate, rollback if needed. `v0.2.8` adds one more reachable state to the lifecycle diagram:

- if an instance record gets lost: `list --removed` → `recreate` to bring it back, or `remove --removed` to let it go.

Either path is now explicit, scripted, and snapshot-safe.

* * *
## Closing Note

`v0.2.0` was about turning ClawCU into a multi-service platform. The `v0.2.x` cycle that ends with `v0.2.8` is about making that platform recover from partial-state situations without forcing users to shell out to `rm -rf` and hope for the best.

Next up for `0.3.0`: a unified `--output {table|json|yaml}` protocol across all read commands, provider bundle provenance, and promoting active-provider to a first-class field.
