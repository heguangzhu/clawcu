# A2A 协议使用指南

🌐 Language:
[English](a2a-protocol.md) | [中文](a2a-protocol.zh-CN.md)

> 本文聚焦 ClawCU 的 A2A adapter：是什么、怎么工作、怎么打开、怎么运维。命令行参考见 [USAGE_latest.zh-CN.md](../release/USAGE_latest.zh-CN.md)。版本历史见 [CHANGELOG.md](../CHANGELOG.md)。

* * *
## TL;DR

- `clawcu create openclaw|hermes --a2a ...` 启动一个**同伴容器**运行 A2A adapter。
- Adapter 说标准 **Google A2A 协议**（JSON-RPC 2.0），暴露 `GET /.well-known/agent-card.json`（发现）、`POST /`（JSON-RPC 消息）、`/tasks/{task_id}` 下的任务端点和 `POST /mcp`（MCP 工具）。
- 不加 `--a2a` 的普通实例一丝不变。A2A 严格 opt-in、纯加法。
- `clawcu a2a registry serve` 启动聚合 registry，让实例之间可以互相发现。
- `clawcu a2a send --to <name> --message "..."` 是冒烟测试命令。

* * *
## 目录

- [Adapter 是什么](#adapter-是什么)
- [架构一览](#架构一览)
- [Opt-in：启用 A2A](#opt-in启用-a2a)
- [Async 任务部署](#async-任务部署)
- [协议表面](#协议表面)
- [面向 LLM 的 MCP 工具](#面向-llm-的-mcp-工具)
- [A2A registry](#a2a-registry)
- [双实例完整演练](#双实例完整演练)
- [给已有实例开启 A2A](#给已有实例开启-a2a)
- [排障](#排障)
- [当前限制](#当前限制)
- [FAQ](#faq)

* * *
## Adapter 是什么

**A2A adapter** 是一个轻量级同伴容器，与受管服务容器一起运行，共享 Docker 网络命名空间。它把标准 Google A2A 协议（JSON-RPC 2.0）翻译成服务的 `/v1/chat/completions` API。

adapter 做的事：

1. 在 `GET /.well-known/agent-card.json` 发布标准 **AgentCard**，让对等方发现这个 agent。
2. 通过 `POST /` 接收 **JSON-RPC 2.0** A2A 消息，转发给同网络的服务网关。
3. 在 Redis 中记录 async task 状态，并暴露任务查询、取消和 SSE 事件流。
4. 通过健康检查报告网关就绪状态。

adapter **不做**的事：

- 不烘焙进服务镜像。服务用原始镜像，不修改。
- 不是反向代理。只在自有端口上说 A2A 协议。
- 不是服务里的插件。服务完全不知道 A2A 的存在。

* * *
## 架构一览

```
┌────────────── Docker 网络命名空间 ───────────────────────────┐
│                                                              │
│   ┌────────────────────┐       ┌────────────────────────┐   │
│   │ 服务容器           │       │ A2A adapter 容器       │   │
│   │  (OpenClaw /       │◀─────│  python:3.12-slim      │   │
│   │   Hermes)          │  LLM  │  a2a-sdk + httpx       │   │
│   │  端口 18789/8642   │  调用 │  端口 18790 / 9119     │   │
│   └────────────────────┘       └────────────────────────┘   │
│          ▲                              ▲                    │
│          │                              │                    │
│   （原有用户）                    A2A 对等方                │
│                                (JSON-RPC 2.0)               │
└──────────────────────────────────────────────────────────────┘
                   │ 18819 (服务)    │ 18820 (A2A)
                   ▼                 ▼
                宿主机网络（默认 127.0.0.1）
```

Adapter 容器通过 `--network container:<service>` 共享服务容器的网络栈。这意味着 adapter 可以在 `127.0.0.1:<service_port>` 直接访问服务，零额外网络跳数。

**各服务默认端口**：

| 服务 | 网关端口（容器内） | Adapter 端口（容器内） | 就绪路径 |
|---|---|---|---|
| OpenClaw | 18789 | 18790 | `/healthz` |
| Hermes | 8642 | 9119 | `/health` |

* * *
## Opt-in：启用 A2A

`clawcu create` 时加 `--a2a` 启用同伴容器：

```bash
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a
```

发生的事：

1. ClawCU 构建一个通用 adapter 镜像 `clawcu/a2a-adapter:<version>`（如已存在则跳过）。
2. 服务从**原始镜像**启动 — 不修改、不烘焙。
3. A2A 同伴栈启动：HTTP adapter、必要时共享 Redis，以及每实例 worker。
4. `.clawcu-instance.json` 写上 `a2a_enabled: true`。

Adapter 镜像在所有 A2A 实例间共享（OpenClaw 和 Hermes 共用）。构建一次，复用多次。

验证：

```bash
curl -s http://127.0.0.1:<adapter_port>/.well-known/agent-card.json | jq .
# 标准 Google A2A AgentCard，包含 supported_interfaces、capabilities、skills
```

* * *
## Async 任务部署

Async 任务执行使用同一个 adapter 镜像，加上 Redis-backed 任务存储：

- Redis 是共享容器，名字是 `clawcu-a2a-redis`。
- 每个 A2A 实例有自己的 worker 容器，名字是 `clawcu-a2a-worker-<instance>`。
- 每个实例默认使用自己的队列：`clawcu:a2a:<instance>`。

Async API 表面默认开启，相当于 `A2A_ASYNC_ENABLED=true`。如需强制只允许 blocking 调用，可设置 `A2A_ASYNC_ENABLED=false`，此时 async MCP 工具不会暴露。

* * *
## 协议表面

Adapter 实现了 **Google A2A 协议 v0.1**，使用 `a2a-sdk`。

### `GET /.well-known/agent-card.json`

返回标准 AgentCard：

```json
{
  "name": "writer",
  "description": "writer agent",
  "supported_interfaces": [{"url": "http://127.0.0.1:18820/", "protocol_version": "0.1"}],
  "version": "0.1.0",
  "capabilities": {"streaming": true},
  "skills": [
    {
      "id": "a2a-chat",
      "name": "chat",
      "description": "Send a message to writer",
      "tags": ["chat"]
    }
  ]
}
```

### `POST /`（JSON-RPC 2.0）

Adapter 接受标准 A2A JSON-RPC 方法：

#### `message/send`

默认情况下，`message/send` 是 blocking：adapter 等同网络的网关返回后，给出一个 completed A2A task。

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"type": "text", "text": "总结昨天的站会"}]
    }
  }
}
```

响应：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "id": "task-1",
    "status": {"state": "completed"},
    "artifacts": [
      {"parts": [{"type": "text", "text": "昨天讨论了..."}]}
    ],
    "message": {
      "role": "agent",
      "parts": [{"type": "text", "text": "昨天讨论了..."}]
    }
  }
}
```

不想等待时，设置 `configuration.blocking=false`：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"type": "text", "text": "总结昨天的站会"}]
    },
    "configuration": {"blocking": false}
  }
}
```

兼容形式是 `metadata.mode=async`；`metadata.mode=sync` 会强制 blocking。两者都没设置时，由 `A2A_DEFAULT_MODE` 决定（默认 `sync`）。

Async 响应：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "id": "task_8f0a0c2c2f17471e9c5f9bca02f4f6aa",
    "status": {"state": "submitted"},
    "metadata": {
      "task_id": "task_8f0a0c2c2f17471e9c5f9bca02f4f6aa",
      "request_id": "2"
    }
  }
}
```

Async 提交要求 `A2A_ASYNC_ENABLED` 未设置或为 true；如果显式禁用，adapter 会返回 JSON-RPC 错误，提示调用方重新启用 async A2A。

### 任务端点

Async task 通过 HTTP 端点管理：

| 端点 | 用途 |
|---|---|
| `GET /tasks/{task_id}` | 返回最新 task snapshot；完成后包含 `status.state`、`artifacts` 和 `message`。 |
| `POST /tasks/{task_id}/cancel` | 请求取消并返回更新后的 task snapshot。取消是 best-effort：排队任务会尽量 abort，正在执行的网关调用仍可能与取消请求竞态。 |
| `GET /tasks/{task_id}/events` | 以 Server-Sent Events 形式流式返回 Redis-backed task 事件。 |

`GET /tasks/{task_id}/events` 支持通过 `Last-Event-ID` 重放该事件之后的内容。事件流会发送 `submitted`、`working`、`progress`、`completed`、`failed`、`canceled` 等任务生命周期事件；空闲时发送 `heartbeat`；进入终态或 idle timeout 时用 `end` 事件结束。

### 健康检查

Adapter 在转发消息前先探服务的就绪路径（OpenClaw 的 `/healthz`，Hermes 的 `/health`）。网关未就绪时，任务会以明确的错误信息失败。

* * *
## 面向 LLM 的 MCP 工具

Adapter 也在 `POST /mcp` 上通过 JSON-RPC 提供 MCP。

始终暴露：

- `a2a_call_peer(to, message, registry_url?, timeout_seconds?)`
- `a2a_list_peers(registry_url?, timeout_seconds?)`

除非 `A2A_ASYNC_ENABLED=false`，否则暴露：

- `a2a_call_peer_async(to, message, registry_url?, timeout_seconds?)`
- `a2a_get_task(to, task_id, registry_url?, timeout_seconds?)`
- `a2a_cancel_task(to, task_id, registry_url?, timeout_seconds?)`

`a2a_call_peer` 会在 A2A registry 里查找 `to`，向对端发送标准 A2A `message/send` 请求，并返回文本内容与结构化 task 数据。同步工具始终发送 `configuration.blocking=true`，即使对端默认模式是 async。`a2a_call_peer_async` 发送 `configuration.blocking=false`，返回 submitted task id 和 task metadata；`a2a_get_task`、`a2a_cancel_task` 调对端 HTTP task 端点。

`a2a_list_peers` 列出 registry 里的名称、角色、技能与端点，让本地 agent 能发现对端实际可能叫 `a2a-smoke-analyst`，而不是 `analyst`。

实例用 `--a2a` 创建时，ClawCU 会把 `mcp.servers.a2a = {"url": "http://127.0.0.1:<adapter_port>/mcp", "transport": "streamable-http"}` 写入服务配置，让本地 agent 能在对话中调用其他 agent。

* * *
## A2A registry

Registry 聚合所有运行中受管实例的 AgentCard，通过 `GET /agents` 和 `GET /agents/{name}` 暴露。启动命令：

```bash
clawcu a2a registry serve
```

默认绑定 `127.0.0.1:9100`，前台运行（Ctrl+C 停止）。每个通过 `--a2a` 创建的受管实例发布自己的卡片；registry 负责收集它们，让实例之间可以互相发现。

* * *
## 双实例完整演练

典型冒烟测试：两个 A2A 实例通过 registry 对话。

```bash
# 1. 创建两个启用 A2A 的实例。
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a

# 2. 启动 A2A registry（前台运行）。
clawcu a2a registry serve

# 3. 另开一个终端：发消息。
clawcu a2a send --to analyst --message "总结昨天的站会"
```

* * *
## 给已有实例开启 A2A

当前没有原地升级通道。使用 clone-first：

```bash
clawcu clone writer --name writer-a2a
clawcu remove writer-a2a                     # 删 clone 的容器
clawcu create openclaw --name writer-a2a \
       --version 2026.4.12 --a2a             # 用克隆的 datadir 重建
```

datadir（模型、历史、env）在 clone + create 过程中保留。服务镜像保持原版；只是加了同伴容器。

* * *
## 排障

**`clawcu a2a send` 返回 "gateway not ready"。**
Adapter 先于服务网关起来，还在等后端就绪。等 10–30 秒重试。如果一直这样，`clawcu logs <instance>` 看看网关卡在哪。

**`clawcu a2a send` 返回服务错误。**
Adapter 原样转发网关返回的错误。去 `clawcu logs <instance>` 看底层 provider 错误（鉴权、模型、配额）。

**`curl :<port>/.well-known/agent-card.json` 正常，但消息挂住。**
通常是模型提供方超时。`clawcu a2a send --timeout 120` 放宽等待窗口。

**同伴容器没运行。**
用 `docker ps | grep clawcu-a2a-<name>` 检查。如果缺失，`clawcu restart <instance>` 会重启服务和同伴容器。

**Async task 一直停在 `submitted`。**
检查 `docker ps | grep clawcu-a2a-worker-<instance>`，并确认 Redis 以 `clawcu-a2a-redis` 运行。Adapter 和 worker 必须使用相同的 `A2A_QUEUE_NAME` 与 `A2A_REDIS_URL`。

**create 时端口冲突。**
`clawcu create --a2a` 在创建时就会探端口。被占用会立即报错。换 `--port` 或释放端口。

* * *
## 当前限制

- **无内建鉴权。** A2A 端点接受任何能触达端口的请求。Adapter 默认绑 127.0.0.1，单机 OK；跨机时请放到做鉴权的反向代理后面。
- **Registry 仅本地。** 聚合本机受管实例的卡片。跨机联邦不在当前范围。
- **普通 / A2A 创建时硬切换。** 无原地开启；用 clone-first。
- **同伴容器生命周期与服务绑定。** 服务停止时同伴容器也应停止（`clawcu start/stop/restart` 自动处理）。

* * *
## FAQ

**不用 `--a2a` 的普通实例会有额外开销吗？**
没有。同伴容器只存在于 A2A 实例。普通实例完全不受影响。

**开销多大？**
A2A 实例会有一个轻量 Python HTTP adapter、一个每实例 worker，以及一个共享 Redis 容器。CPU 静息接近 0；每次请求的成本主要来自下游 LLM 调用。

**和 `clawcu exec` / `clawcu tui` 会冲突吗？**
不会。Adapter 在旁边运行，`exec` / `tui` / `token` / `config` 与非 A2A 实例行为完全一致。

**Adapter 和旧的 sidecar 有什么区别？**
旧 sidecar（v0.3.x）在创建时烘焙进服务 Docker 镜像——修改镜像、注入 entrypoint 监督进程。新 adapter 是独立容器共享服务的网络，使用标准 Google A2A 协议（JSON-RPC 2.0）。更简单、更可维护、与第三方 A2A 客户端可互操作。

**怎么独立于服务升级 adapter？**
`pip install --upgrade clawcu`，然后 `clawcu recreate <instance>`。Adapter 镜像重建；服务镜像不变。

* * *

延伸阅读：

- [USAGE_latest.zh-CN.md](../release/USAGE_latest.zh-CN.md) —— `clawcu a2a` 命令参考
- [a2a-gateway.md](a2a-gateway.md) —— 0.5.x gateway/router 方案记录
- [CHANGELOG.md](../CHANGELOG.md) —— 完整版本历史
