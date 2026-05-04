# ClawCU v0.4.2

🌐 语言：
[English](RELEASE_v0.4.2.md) | [中文](RELEASE_v0.4.2.zh-CN.md)

发布日期：待定

## 亮点

- A2A async task 支持：JSON-RPC `message/send` 可通过 `configuration.blocking=false` 或 `metadata.mode=async` 非阻塞提交，并立即返回 submitted task。
- 新增任务端点：`GET /tasks/{task_id}`、`POST /tasks/{task_id}/cancel`、`GET /tasks/{task_id}/events`，SSE 支持 replay、heartbeat 和终态 `end` 事件。取消是 best-effort。
- MCP async 工具默认开启：`a2a_call_peer_async`、`a2a_get_task`、`a2a_cancel_task`。设置 `A2A_ASYNC_ENABLED=false` 可隐藏它们。现有 `a2a_call_peer` 保持同步，并强制 blocking send。
- Async 部署默认使用共享 Redis 容器 `clawcu-a2a-redis`、共享 Redis-backed registry 容器 `clawcu-a2a-registry`、每实例 adapter/worker 容器，以及队列 `clawcu:a2a:<instance>`。

## 配置

- 新增 async env：`A2A_ASYNC_ENABLED`、`A2A_DEFAULT_MODE`、`A2A_REDIS_URL`、`A2A_QUEUE_NAME`、`A2A_TASK_WORKERS`、`A2A_TASK_DEADLINE_S`、`A2A_TASK_RETAIN_S`、`A2A_TASK_PROGRESS_INTERVAL_S`、`A2A_TASK_EVENTS_IDLE_TIMEOUT_S`。
- `A2A_REDIS_URL` 默认值为 `redis://host.docker.internal:6379/0`。
- `A2A_ARQ_QUEUE_NAME` 只保留为 `A2A_QUEUE_NAME` 的旧兼容 alias。
- Registry peer discovery 默认使用 Redis：adapter 刷新 `a2a:registry:peer:<name>` snapshot，过期 peer 会在 TTL 后从 `/agents` 消失。
