# ClawCU v0.2.9

🌐 Language:
[English](RELEASE_v0.2.9.md) | [中文](RELEASE_v0.2.9.zh-CN.md)

Release Date: April 19, 2026

> `v0.2.9` is a focused UX patch over `v0.2.8`: `clawcu list` now tells you *what you could upgrade to*, and `clawcu <cmd>` without arguments finally behaves the way users expect — help output, not a cryptic one-line error.

* * *
## Highlights

- `clawcu list` — Available versions footer
  - Appends a compact "Available versions" block below the instance table.
  - Top 10 most recent **stable** releases per service (OpenClaw, Hermes), **newest first**.
  - Prereleases (`-beta`, `-rc`, `-alpha`) are filtered — the footer is an "install candidates" surface, not a tester surface. `upgrade --list-versions` still exposes every tag.
  - Fetched best-effort from each service's configured image registry. Failures are surfaced inline so the row is never silently empty.
  - Skipped for `--json` (scripts stay on the instance-array contract), `--agents`, and `--removed`.
  - New `--no-remote` flag for strictly offline rendering (CI, airgapped, slow networks).

- Bare invocation now prints help
  - `clawcu <cmd>` with no arguments prints full help and exits 0 when the command requires arguments.
  - Previously this produced a cryptic one-line `Usage: ... Try --help` and exited 2 — treating a "what does this take?" query as a failed invocation.
  - Partial invocations (some args, still missing a required one) keep POSIX exit 2, but now print the full help alongside the `Missing option` error so every flag is visible in one go.

- 360 Tests
  - Suite grew from 356 at `v0.2.8` release time to 360 passing tests, covering the available-versions footer path (remote success / remote-disabled / JSON-skip) and the bare-vs-partial invoke UX split.

* * *
## Why "Available versions" in `list`

Before `v0.2.9`, finding out what version to upgrade to required a per-instance command:

```
clawcu upgrade writer --list-versions
```

That works, but it asks the user to think in terms of *a specific instance*. The reality is that users scanning `clawcu list` often want the same answer as a shared, ambient question: *what's the newest OpenClaw? Hermes?*

`v0.2.9` answers that question in the place they're already looking:

```
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

### Design choices

- **Default on.** Most users run `list` a few times a day, not a few times a second — a 4-second best-effort fetch per registry is a reasonable cost for "never miss a release."
- **Stable only.** The footer is for everyday upgrade candidates. Beta testers who want prereleases still get them from `upgrade --list-versions`.
- **Newest first.** Matches how most people scan: the version you likely want is in the leftmost position, not at the end of the row.
- **`--no-remote` for CI.** When the registry is unreachable or you want a deterministic offline render, skip the fetch entirely.
- **JSON stays stable.** Scripts calling `clawcu list --json` continue to see exactly the instance array they always did.

* * *
## Why help-on-bare-invoke

The default behavior before `v0.2.9`:

```
$ clawcu create hermes
Usage: root create hermes [OPTIONS]
Try 'root create hermes --help' for help.

Error: Missing option '--name'.
```

This treats a perfectly reasonable user question (*"what does this command take?"*) as a failed invocation. It prints three lines of near-noise and makes the user type one more command to get the help they actually needed.

`v0.2.9` splits this into two paths:

### Bare invoke → show help, exit 0

```
$ clawcu create hermes
                                                                                
 Usage: clawcu create hermes [OPTIONS]                                          
                                                                                
 Create and start a Hermes instance.                                            
                                                                                
╭─ Options ──────────────────────────────────────────────────────────────────╮
│ *  --name          TEXT     Managed instance name. [required]              │
│ *  --version       TEXT     Hermes version or git ref. [required]          │
│    --port          INTEGER  Host port.                                     │
│    --datadir       TEXT     Data directory.                                │
│    --cpu           TEXT     CPU limit. Default 1.                          │
│    --memory        TEXT     Memory limit. Default 2g.                      │
│    --help                   Show this message and exit.                    │
╰────────────────────────────────────────────────────────────────────────────╯
```

No args means "tell me what this takes" — so we tell them. Exit 0. The `*` in the left gutter still marks required options (preserved from the native Typer behavior), so the user knows which flags are mandatory without reading the text.

### Partial invoke → help + targeted error, exit 2

```
$ clawcu create hermes --name demo
[full help]

Error: Missing option '--version'.
```

When the user *does* supply some args but forgets a required one, they clearly meant to invoke the command — so we keep POSIX exit 2 (scripts continue to detect the failure) but print the full help alongside the targeted error. The user sees *every flag they could have used*, not just "oh, `--version` was missing — but was that the only thing? is there anything else I need?".

* * *
## Compatibility

`v0.2.9` is a drop-in upgrade from `v0.2.8`.

- No breaking CLI changes.
- Existing managed instances keep working without migration.
- Scripts calling `clawcu list --json` see unchanged output (the versions footer is text-mode only).
- Scripts calling any command with required args get the same exit-2 behavior on partial invocation; only the **bare-invoke** case changed from exit 2 to exit 0.

* * *
## Recommended Workflow

Unchanged from `v0.2.8`:

- Upgrade on a clone first, promote only if the clone holds.
- Snapshots before every upgrade; `rollback` restores from real backups.
- `list --removed` → `recreate <orphan>` for orphan recovery.

The `v0.2.9` list footer simply makes the *first step* of that workflow — deciding which version to upgrade to — visible by default.

* * *
## Closing Note

`v0.2.8` closed the orphan lifecycle. `v0.2.9` is about getting out of the user's way: less typing to see version candidates, no more cryptic one-line errors when the user was obviously just exploring.

Next up for `0.3.0`: the unified `--output {table|json|yaml}` protocol, provider bundle provenance, and promoting active-provider to a first-class field.
