# Changelog

All notable changes to ClawCU are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- `--output {table|json|yaml}` global protocol — unify JSON schema across list/inspect.
- Provider bundle provenance via `.clawcu-instance.json` metadata.
- Active provider as a first-class field.

## [0.5.1] - 2026-05-08

### Changed
- Agent-to-agent functionality has been split out of `main` and lives on the dedicated `a2a` branch. The `main` branch focuses on the OpenClaw / Hermes lifecycle manager surface.

## [0.4.1] - 2026-04-29

### Added
- **Dashboard Docker container** — `clawcu dashboard` now runs as a persistent Docker container instead of a local Python process:
  - Auto-builds `clawcu-dashboard:<version>` image on first run.
  - Container runs with `--restart unless-stopped` for always-on availability.
  - Mounts `~/.clawcu`, `~/.openclaw`, `~/.hermes`, and `/var/run/docker.sock`.
  - Publishes on `127.0.0.1:8765` by default (LAN-safe).
  - New flags: `--stop`, `--restart`, `--status`, `--rebuild`.
  - Dashboard actions (`open_cli`, `open_config`, `open_tui`) gracefully degrade inside the container with host-side command hints.
  - Health endpoint (`/health`) for Docker HEALTHCHECK and startup polling.
- **Provider commands** — `clawcu provider collect/list/show/apply/remove` for cross-service auth/model bundle management.
- `remove` auto-prompts to delete orphaned datadir when the instance record is already gone.
- `tui` checks instance status before launch; stopped instances get explicit `clawcu start <name>` guidance instead of auto-start.
- `remove` auto-stops running instances before removal (10s grace) when `--delete-data` is passed.
- `logs --follow` sends ANSI reset (`\x1b[0m`) on KeyboardInterrupt to prevent terminal color pollution.
- `getenv --table` renders grouped table output (Sensitive / General) with masking by default.
- `setenv --reload` sends SIGHUP to the running container for best-effort hot reload without recreation.
- `snapshots list` and `snapshots clean --keep-last N` subcommands for manual snapshot management.
- Automatic snapshot pruning after successful `upgrade` (keeps last 10 per instance; history-referenced snapshots are never deleted).
- `upgrade --list-versions` fallback message enhancement when remote registry is unreachable.
- `config --help` now includes service-specific examples for OpenClaw and Hermes.

### Changed
- **CLI redesign v1** — simplify surface for v0.4:
  - `list` default no longer shows version footer; `--versions` is explicit opt-in.
  - `token` / `approve` moved to `clawcu openclaw` subgroup (root hidden aliases with deprecation warning).
  - Removed `pull` command (`create` auto-pulls via service layer).
  - Removed `hermes identity set` (use `docker cp` / `exec` instead).

## [0.3.x] - 2026-04-22 to 2026-04-23

### Note
- The 0.3.x agent-to-agent experiment has been moved to the dedicated `a2a` branch and is no longer part of the main release line.

## [0.2.12] - 2026-04-22

### Added
- `clawcu list --no-cache` now forces a fresh Available Versions registry refresh for the footer while still updating the on-disk daily cache after a successful fetch.

### Fixed
- `clawcu list` now tolerates legacy instance-record fields instead of failing to deserialize older managed-instance JSON files.

## [0.2.11] - 2026-04-22

### Added
- `create` and `upgrade` now accept optional `--image` overrides while keeping `--version` as the required logical version label. The selected runtime image is persisted on the instance so later `recreate`, orphan recovery, and `rollback` keep following the same image chain.

## [0.2.10] - 2026-04-19

### Added
- `clawcu list` caches the "Available versions" registry fetch at `<clawcu_home>/cache/available_versions.json`, valid for the local calendar day. Subsequent runs on the same day are served from cache; a new day, a changed `image_repo`, or `--no-remote` triggers a refetch. Failures are never cached so transient outages do not linger.
- When the registry fetch fails (network down, DNS, auth) or `--no-remote` is set, the footer surfaces local Docker images as an offline fallback on a continuation line under the error, so the user sees actionable candidates instead of just a red error.

## [0.2.9] - 2026-04-19

### Added
- `clawcu list` appends an "Available versions" footer with the 10 most recent stable releases per service (OpenClaw, Hermes), newest first. Prereleases (`-beta`, `-rc`, `-alpha`) are filtered; `upgrade --list-versions` still exposes them for testers. Skipped in `--json` / `--agents` / `--removed` views; pass `--no-remote` for a strictly offline render.

### Fixed
- `clawcu <cmd>` with no args now prints full help and exits 0 when the command requires arguments — it's a "what does this take?" query, not a failed invocation. Partial invocations (some args, still missing a required one) keep POSIX exit 2 but now print the full help alongside the `Missing option` error so every flag is visible.

## [0.2.8] - 2026-04-19

### Added
- `list --removed` surfaces the `port` from `.clawcu-instance.json` metadata when available (falls back to `-` for pre-metadata orphans).

### Fixed
- `remove --removed` now explicitly rejects `--delete-data` / `--keep-data`; previously those flags were silently accepted despite having no effect.

## [0.2.7] - 2026-04-19

### Added
- `list` rejects conflicting `--source` / `--removed` / `--local` / `--managed` / `--all` combinations with explicit errors.

### Fixed
- Hint for "Removed instance 'X' was not found" now correctly points at `list --removed` instead of the generic "instance not found" suggestion.

## [0.2.6] - 2026-04-18

### Added
- Orphan instance lifecycle: `list --removed`, `recreate <orphan>`, `remove <orphan> --removed`.
- Self-describing datadir via `.clawcu-instance.json` metadata.

## [0.2.0] - 2026-04-17

### Added
- Multi-service support: `openclaw` and `hermes` as first-class services.
- Shared command surface across services.

## [0.1.0] - 2026-04-15

### Added
- Initial public release.
- OpenClaw lifecycle management: pull, create, list, inspect, start/stop, upgrade, rollback, clone, snapshots.

[Unreleased]: https://github.com/heguangzhu/clawcu/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/heguangzhu/clawcu/compare/v0.4.1...v0.5.1
[0.4.1]: https://github.com/heguangzhu/clawcu/compare/v0.2.12...v0.4.1
[0.3.x]: https://github.com/heguangzhu/clawcu/tree/a2a
[0.2.12]: https://github.com/heguangzhu/clawcu/compare/v0.2.11...v0.2.12
[0.2.11]: https://github.com/heguangzhu/clawcu/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/heguangzhu/clawcu/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/heguangzhu/clawcu/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/heguangzhu/clawcu/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/heguangzhu/clawcu/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/heguangzhu/clawcu/compare/v0.2.0...v0.2.6
[0.2.0]: https://github.com/heguangzhu/clawcu/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/heguangzhu/clawcu/releases/tag/v0.1.0
