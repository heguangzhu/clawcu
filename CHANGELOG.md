# Changelog

All notable changes to ClawCU are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned for 0.3.0
- `--output {table|json|yaml}` global protocol — unify JSON schema across list/inspect.
- Provider bundle provenance via `.clawcu-instance.json` metadata.
- Active provider as a first-class field.

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

[Unreleased]: https://github.com/heguangzhu/clawcu/compare/v0.2.10...HEAD
[0.2.10]: https://github.com/heguangzhu/clawcu/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/heguangzhu/clawcu/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/heguangzhu/clawcu/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/heguangzhu/clawcu/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/heguangzhu/clawcu/compare/v0.2.0...v0.2.6
[0.2.0]: https://github.com/heguangzhu/clawcu/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/heguangzhu/clawcu/releases/tag/v0.1.0
