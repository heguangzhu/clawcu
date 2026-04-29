# A2A Async Tasks

状态枚举：`未开始` / `进行中` / `已完成` / `测试通过` / `已提交`

Rapha loop 恢复点（2026-04-30）：

- Round 1 已完成：TaskStore、worker、HTTP async/task routes、MCP async tools、Docker companion orchestration 已实现。
- Round 2 已完成：inspect/getenv/operator surface、SSE heartbeat、docs/release notes、Redis smoke、CLI 测试已补齐。
- 已验证：`uv run --extra a2a pytest`，结果 `509 passed`。
- Redis smoke：`docker exec clawcu-a2a-redis redis-cli ping` 返回 `PONG`；真实 Redis TaskStore + worker smoke 完成 `submitted -> working -> progress -> completed`。
- 提交：`Add Redis-backed A2A async tasks`。

## Phase 0: 准备工作

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T00 | 测试通过 | 启动本地 Redis 测试服务 | `clawcu-a2a-redis` 容器，`redis://127.0.0.1:6379/0` 可用 | `docker exec clawcu-a2a-redis redis-cli ping` 返回 `PONG` |
| T01 | 测试通过 | 确认 arq 依赖策略 | `pyproject.toml` 的 `a2a` optional dependency 增加 `arq` | `uv run --extra a2a pytest tests/a2a_adapter` 通过 |
| T02 | 测试通过 | 确认 Redis 连接配置入口 | 统一解析 `A2A_REDIS_URL`、默认值、错误提示 | `tests/a2a_adapter/test_tasks.py` 覆盖默认值、显式值、非法值 |
| T03 | 测试通过 | 明确 async feature flag | `A2A_ASYNC_ENABLED` 与 `A2A_DEFAULT_MODE` 行为写入代码和文档 | 代码、测试、文档均已完成 |

## Phase 1: Redis Task Facade

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T10 | 测试通过 | 新增 task facade 模块 | `src/clawcu/a2a/adapter/tasks.py` | `tests/a2a_adapter/test_tasks.py` 通过 |
| T11 | 测试通过 | 定义 task snapshot schema | `task_id`、`state`、`input`、`result`、`error`、时间戳字段 | snapshot 序列化/反序列化测试通过 |
| T12 | 测试通过 | 实现 task id 生成 | `task_` + uuid hex | id 格式测试通过 |
| T13 | 测试通过 | 实现 Redis key 约定 | `a2a:task:<task_id>`、`a2a:task:<task_id>:events` | key 构造测试通过 |
| T14 | 测试通过 | 实现 task 创建 | 创建 `submitted` snapshot 并写入 first event | fake Redis 测试通过 |
| T15 | 测试通过 | 实现状态转换校验 | `submitted/working/completed/failed/canceled` 状态机 | 非法转换测试通过 |
| T16 | 测试通过 | 实现 progress 写入 | 更新 `last_progress_at`、`last_progress_message`，追加 Redis Stream event | progress snapshot 与 event 测试通过 |
| T17 | 测试通过 | 实现 terminal 写入 | `completed` 写 result，`failed/canceled` 写 error | 结果与错误结构测试通过 |
| T18 | 测试通过 | 实现 TTL/retention | snapshot 和 events 根据 `A2A_TASK_RETAIN_S` 设置过期 | TTL 测试通过 |
| T19 | 测试通过 | 实现 arq 状态 fallback 映射 | arq queued/running/result 映射到 A2A state | 映射单元测试通过 |

## Phase 2: arq Worker

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T20 | 测试通过 | 新增 worker 模块 | `src/clawcu/a2a/adapter/worker.py` | `tests/a2a_adapter/test_worker.py` 通过 |
| T21 | 测试通过 | 定义 arq WorkerSettings | queue name、Redis settings、timeouts、worker count、abort 配置 | WorkerSettings 单元测试通过 |
| T22 | 测试通过 | 实现 instance 专属 queue name | `clawcu:a2a:<instance-name>` | queue name 测试通过 |
| T23 | 测试通过 | 实现 `run_gateway_turn` | worker 从 job payload 调 gateway 并写 task state | 成功路径测试通过 |
| T24 | 测试通过 | 复用/抽取 gateway 调用逻辑 | HTTP adapter 和 worker 共用 `_call_gateway` 逻辑 | executor 与 worker 测试通过 |
| T25 | 测试通过 | 实现 worker progress 事件 | waiting/calling/streaming/completed 等阶段事件 | progress event 测试通过 |
| T26 | 测试通过 | 实现 worker 错误处理 | gateway HTTP error、timeout、empty reply 写入 `failed` | 失败路径测试通过 |
| T27 | 测试通过 | 实现取消协作 | worker 检查 terminal/canceled snapshot，避免覆盖结果 | terminal/cancel race 测试通过 |
| T28 | 测试通过 | 限制 arq 重试行为 | `max_tries=1`、`retry_jobs=False` 或等价配置 | worker 配置测试通过 |

## Phase 3: HTTP / A2A Adapter

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T30 | 测试通过 | 扩展 JSON-RPC `message/send` mode 解析 | 支持 sync/async mode，默认 sync | sync 回归测试通过 |
| T31 | 测试通过 | 实现 async enqueue 路径 | 创建 task，enqueue arq job，返回 submitted task | async dispatch 测试通过 |
| T32 | 测试通过 | 保持 sync 路径兼容 | `a2a_call_peer` 和默认 JSON-RPC 仍等完整 reply | server/MCP JSON-RPC 测试通过 |
| T33 | 测试通过 | 新增 task get route | `GET /tasks/{task_id}` | completed/result shape 查询测试通过 |
| T34 | 测试通过 | 新增 task cancel route | `POST /tasks/{task_id}/cancel` | cancel route 测试通过 |
| T35 | 测试通过 | 新增 task events route | `GET /tasks/{task_id}/events` SSE | replay、heartbeat、terminal end 测试通过 |
| T36 | 测试通过 | 接入 `sse-starlette` | 使用 Redis Stream 输出 SSE | `sse-starlette` extra 依赖与 route 测试通过 |
| T37 | 测试通过 | 补充 async disabled 错误面 | Redis 不可用或未启用时返回明确错误，不影响 sync | disabled path 测试通过 |

## Phase 4: MCP Bridge

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T40 | 测试通过 | 新增 `a2a_call_peer_async` 工具描述 | MCP `tools/list` 暴露 async dispatch 工具 | enabled/disabled tools/list 测试通过 |
| T41 | 测试通过 | 新增 `a2a_get_task` 工具描述 | MCP `tools/list` 暴露 task poll 工具 | enabled tools/list 测试通过 |
| T42 | 测试通过 | 新增 `a2a_cancel_task` 工具描述 | MCP `tools/list` 暴露 cancel 工具 | enabled tools/list 测试通过 |
| T43 | 测试通过 | 实现 async peer call | registry lookup 后向 peer 发送 async `message/send` | MCP async call 测试通过 |
| T44 | 测试通过 | 实现 task polling | registry lookup 后调用 peer task get route | task poll routing 测试通过 |
| T45 | 测试通过 | 实现 task cancel | registry lookup 后调用 peer cancel route | cancel structuredContent 测试通过 |
| T46 | 测试通过 | sync 工具强制 sync mode | `A2A_DEFAULT_MODE=async` 不改变 `a2a_call_peer` 行为 | sync tool blocking 回归测试通过 |
| T47 | 测试通过 | 更新 MCP 错误结构 | registry/peer/task 错误返回可读 JSON-RPC error | 错误路径测试通过 |

## Phase 5: Docker / Companion Orchestration

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T50 | 测试通过 | 扩展 companion spec | adapter/worker/redis 所需 env 和配置字段 | compose 单元测试通过 |
| T51 | 测试通过 | 增加 Redis ensure/start helper | 复用或创建 `clawcu-a2a-redis` | service lifecycle 测试通过 |
| T52 | 测试通过 | 增加 worker companion name | `clawcu-a2a-worker-<instance>` | name 测试通过 |
| T53 | 测试通过 | 实现 worker companion start | worker 容器共享主服务 network namespace | docker run 命令测试通过 |
| T54 | 测试通过 | 实现 worker companion stop/remove | 删除实例时清理 worker companion | stop/remove 测试通过 |
| T55 | 测试通过 | 实现 restart 顺序 | main service -> Redis -> adapter -> worker | restart 命令顺序测试通过 |
| T56 | 测试通过 | 注入 Redis 和 queue env | `A2A_REDIS_URL`、`A2A_QUEUE_NAME`、worker settings env | compose/service env 测试通过 |
| T57 | 测试通过 | 更新 adapter Dockerfile | 安装 arq，支持 adapter/worker 两种入口 | Dockerfile/compose 测试通过 |

## Phase 6: CLI / Inspect / Operator Surface

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T60 | 测试通过 | 更新 inspect 输出 | 显示 async enabled、Redis URL、queue、worker 状态 | service + CLI inspect 测试通过 |
| T61 | 测试通过 | 更新 getenv 分组 | 将 async/Redis env 放入 A2A 分组 | getenv table 分组测试通过 |
| T62 | 测试通过 | 明确 Redis 不可用提示 | create/restart/inspect 给出可操作错误或 warning | service + CLI 错误提示测试通过 |
| T63 | 已完成 | 评估是否增加 CLI task 命令 | 暂不新增 CLI task 子命令；HTTP endpoints 与 MCP tools 已覆盖 get/cancel/events，避免重复 surface | 决策记录于本表 |

## Phase 7: Tests

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T70 | 测试通过 | task facade 单元测试 | `tests/a2a_adapter/test_tasks.py` | `uv run --extra a2a pytest tests/a2a_adapter tests/test_service.py` 通过 |
| T71 | 测试通过 | worker 单元测试 | `tests/a2a_adapter/test_worker.py` | `uv run --extra a2a pytest tests/a2a_adapter tests/test_service.py` 通过 |
| T72 | 测试通过 | server async JSON-RPC 测试 | async dispatch、sync fallback、disabled path | `uv run --extra a2a pytest tests/a2a_adapter tests/test_service.py` 通过 |
| T73 | 测试通过 | MCP async 工具测试 | async call/get/cancel 三工具 | `uv run --extra a2a pytest tests/a2a_adapter tests/test_service.py` 通过 |
| T74 | 测试通过 | SSE route 测试 | replay、heartbeat、terminal close | `tests/a2a_adapter/test_server_jsonrpc.py` 通过 |
| T75 | 测试通过 | Docker orchestration 测试 | Redis、adapter、worker start/stop/restart | service + compose 测试通过 |
| T76 | 测试通过 | Redis integration smoke test | 使用 `clawcu-a2a-redis` 跑最小 async job | Redis smoke pass |
| T77 | 测试通过 | 全量相关测试 | `pytest tests/a2a_adapter tests/test_service.py tests/test_cli.py` | `uv run --extra a2a pytest` 全量 509 passed |

## Phase 8: Docs

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T80 | 已完成 | 更新 A2A 协议文档 | async dispatch、task get、cancel、events | `docs/a2a-protocol*.md` 已更新 |
| T81 | 已完成 | 更新 env 文档 | Redis/arq/task retention/env defaults | `docs/a2a-envs.md` 已更新 |
| T82 | 已完成 | 更新 release notes 草稿 | 说明 Redis 依赖、默认 sync、async opt-in | `release/RELEASE_latest*.md` 已更新 |
| T83 | 已完成 | 更新 troubleshooting | Redis 不可用、worker missing、task stuck、cancel best-effort | protocol/env/release docs 已覆盖 |

## Phase 9: Rollout / Commit Tracking

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T90 | 已提交 | Phase 1 hidden plumbing commit | task facade + worker 基础，不默认暴露 async | 合并提交覆盖 |
| T91 | 已提交 | Phase 2 opt-in async commit | feature flag 下暴露 async MCP/API | 合并提交覆盖 |
| T92 | 已提交 | Phase 3 orchestration commit | Redis/worker companion 管理 | 合并提交覆盖 |
| T93 | 已提交 | Phase 4 docs/tests commit | 文档和测试补齐 | 合并提交覆盖 |
