# ClawCU v0.4.2

🌐 Language:
[English](RELEASE_v0.4.2.md) | [中文](RELEASE_v0.4.2.zh-CN.md)

Release Date: TBD

## Highlights

- A2A async task support: non-blocking JSON-RPC `message/send` can be requested with `configuration.blocking=false` or `metadata.mode=async`, returning a submitted task immediately.
- New task endpoints: `GET /tasks/{task_id}`, `POST /tasks/{task_id}/cancel`, and `GET /tasks/{task_id}/events` with SSE replay, heartbeat, and terminal `end` events. Cancellation is best-effort.
- MCP async tools are enabled by default: `a2a_call_peer_async`, `a2a_get_task`, and `a2a_cancel_task`. Set `A2A_ASYNC_ENABLED=false` to hide them. The existing `a2a_call_peer` remains synchronous and forces blocking sends.
- Async deployment uses shared Redis container `clawcu-a2a-redis`, shared Redis-backed registry container `clawcu-a2a-registry`, per-instance adapter/worker containers, and queue `clawcu:a2a:<instance>` by default.

## Configuration

- Added async envs: `A2A_ASYNC_ENABLED`, `A2A_DEFAULT_MODE`, `A2A_REDIS_URL`, `A2A_QUEUE_NAME`, `A2A_TASK_WORKERS`, `A2A_TASK_DEADLINE_S`, `A2A_TASK_RETAIN_S`, `A2A_TASK_PROGRESS_INTERVAL_S`, and `A2A_TASK_EVENTS_IDLE_TIMEOUT_S`.
- `A2A_REDIS_URL` defaults to `redis://host.docker.internal:6379/0`.
- `A2A_ARQ_QUEUE_NAME` is kept only as a legacy compatibility alias for `A2A_QUEUE_NAME`.
- Registry peer discovery is Redis-backed by default: adapters refresh `a2a:registry:peer:<name>` snapshots, and stale peers disappear from `/agents` after TTL expiry.
