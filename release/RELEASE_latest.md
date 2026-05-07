# ClawCU v0.4.1

🌐 Language:
[English](RELEASE_v0.4.1.md) | [中文](RELEASE_v0.4.1.zh-CN.md)

Release Date: 2026-04-29

## Highlights

- Dashboard now runs as a persistent Docker container, with `--stop`, `--restart`, `--status`, and `--rebuild` controls.
- Provider commands collect, list, inspect, apply, and remove cross-service auth/model bundles.
- `clawcu list --versions` shows available upgrade candidates explicitly, with a cache-aware `--no-cache` refresh path.
- Removed-instance recovery is part of the documented lifecycle through `list --removed`, `recreate`, and `remove --removed`.
- Lifecycle operations continue to snapshot datadirs and env files before risky changes such as `upgrade` and `rollback`.

## Notes

This `main` branch release line focuses on local lifecycle management for OpenClaw and Hermes. Experimental agent-to-agent features live on the separate `a2a` branch.
