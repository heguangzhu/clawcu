# A2A Async Tasks

状态枚举：`未开始` / `进行中` / `已完成` / `测试通过` / `已提交`

## Phase 0: 准备工作

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T00 | 已完成 | 启动本地 Redis 测试服务 | `clawcu-a2a-redis` 容器，`redis://127.0.0.1:6379/0` 可用 | `docker exec clawcu-a2a-redis redis-cli ping` 返回 `PONG` |
| T01 | 未开始 | 确认 arq 依赖策略 | `pyproject.toml` 的 `a2a` optional dependency 增加 `arq` | `uv lock` 或等价依赖解析通过 |
| T02 | 未开始 | 确认 Redis 连接配置入口 | 统一解析 `A2A_REDIS_URL`、默认值、错误提示 | 单元测试覆盖默认值、显式值、非法值 |
| T03 | 未开始 | 明确 async feature flag | `A2A_ASYNC_ENABLED` 与 `A2A_DEFAULT_MODE` 行为写入代码和文档 | sync 默认行为不变 |

## Phase 1: Redis Task Facade

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T10 | 未开始 | 新增 task facade 模块 | `src/clawcu/a2a/adapter/tasks.py` | 模块可导入 |
| T11 | 未开始 | 定义 task snapshot schema | `task_id`、`state`、`input`、`result`、`error`、时间戳字段 | snapshot 序列化/反序列化测试 |
| T12 | 未开始 | 实现 task id 生成 | `task_` + uuid hex | id 格式测试 |
| T13 | 未开始 | 实现 Redis key 约定 | `a2a:task:<task_id>`、`a2a:task:<task_id>:events` | key 构造测试 |
| T14 | 未开始 | 实现 task 创建 | 创建 `submitted` snapshot 并写入 first event | Redis fake 或测试 Redis 验证 |
| T15 | 未开始 | 实现状态转换校验 | `submitted/working/completed/failed/canceled` 状态机 | 非法转换返回明确错误 |
| T16 | 未开始 | 实现 progress 写入 | 更新 `last_progress_at`、`last_progress_message`，追加 Redis Stream event | progress snapshot 与 event 测试 |
| T17 | 未开始 | 实现 terminal 写入 | `completed` 写 result，`failed/canceled` 写 error | 结果与错误结构测试 |
| T18 | 未开始 | 实现 TTL/retention | snapshot 和 events 根据 `A2A_TASK_RETAIN_S` 设置过期 | TTL 测试 |
| T19 | 未开始 | 实现 arq 状态 fallback 映射 | arq queued/running/result 映射到 A2A state | 映射单元测试 |

## Phase 2: arq Worker

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T20 | 未开始 | 新增 worker 模块 | `src/clawcu/a2a/adapter/worker.py` | `python -m clawcu.a2a.adapter.worker` 可启动或显示配置错误 |
| T21 | 未开始 | 定义 arq WorkerSettings | queue name、Redis settings、timeouts、worker count、abort 配置 | WorkerSettings 单元测试 |
| T22 | 未开始 | 实现 instance 专属 queue name | `clawcu:a2a:<instance-name>` | queue name 测试 |
| T23 | 未开始 | 实现 `run_gateway_turn` | worker 从 job payload 调 gateway 并写 task state | 成功路径测试 |
| T24 | 未开始 | 复用/抽取 gateway 调用逻辑 | HTTP adapter 和 worker 共用 `_call_gateway` 逻辑 | 现有 executor 测试仍通过 |
| T25 | 未开始 | 实现 worker progress 事件 | waiting/calling/streaming/completed 等阶段事件 | progress event 测试 |
| T26 | 未开始 | 实现 worker 错误处理 | gateway HTTP error、timeout、empty reply 写入 `failed` | 失败路径测试 |
| T27 | 未开始 | 实现取消协作 | worker 检查 terminal/canceled snapshot，避免覆盖结果 | cancel race 测试 |
| T28 | 未开始 | 限制 arq 重试行为 | `max_tries=1`、`retry_jobs=False` 或等价配置 | worker 配置测试 |

## Phase 3: HTTP / A2A Adapter

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T30 | 未开始 | 扩展 JSON-RPC `message/send` mode 解析 | 支持 sync/async mode，默认 sync | sync 现有测试不回归 |
| T31 | 未开始 | 实现 async enqueue 路径 | 创建 task，enqueue arq job，返回 submitted task | async dispatch 测试 |
| T32 | 未开始 | 保持 sync 路径兼容 | `a2a_call_peer` 和默认 JSON-RPC 仍等完整 reply | 现有 server JSON-RPC 测试通过 |
| T33 | 未开始 | 新增 task get route | `GET /tasks/{task_id}` | queued/working/completed/failed/canceled 查询测试 |
| T34 | 未开始 | 新增 task cancel route | `POST /tasks/{task_id}/cancel` | queued cancel 和 running cancel 测试 |
| T35 | 未开始 | 新增 task events route | `GET /tasks/{task_id}/events` SSE | replay、heartbeat、terminal end 测试 |
| T36 | 未开始 | 接入 `sse-starlette` | 使用 Redis Stream 输出 SSE | 浏览器/HTTP 客户端测试 |
| T37 | 未开始 | 补充 async disabled 错误面 | Redis 不可用或未启用时返回明确错误，不影响 sync | 错误路径测试 |

## Phase 4: MCP Bridge

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T40 | 未开始 | 新增 `a2a_call_peer_async` 工具描述 | MCP `tools/list` 暴露 async dispatch 工具 | tools/list 测试 |
| T41 | 未开始 | 新增 `a2a_get_task` 工具描述 | MCP `tools/list` 暴露 task poll 工具 | tools/list 测试 |
| T42 | 未开始 | 新增 `a2a_cancel_task` 工具描述 | MCP `tools/list` 暴露 cancel 工具 | tools/list 测试 |
| T43 | 未开始 | 实现 async peer call | registry lookup 后向 peer 发送 async `message/send` | MCP async call 测试 |
| T44 | 未开始 | 实现 task polling | registry lookup 后调用 peer task get route | completed 时 text content 包含 reply |
| T45 | 未开始 | 实现 task cancel | registry lookup 后调用 peer cancel route | cancel structuredContent 测试 |
| T46 | 未开始 | sync 工具强制 sync mode | `A2A_DEFAULT_MODE=async` 不改变 `a2a_call_peer` 行为 | 回归测试 |
| T47 | 未开始 | 更新 MCP 错误结构 | registry/peer/task 错误返回可读 JSON-RPC error | 错误路径测试 |

## Phase 5: Docker / Companion Orchestration

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T50 | 未开始 | 扩展 companion spec | adapter/worker/redis 所需 env 和配置字段 | compose 单元测试 |
| T51 | 未开始 | 增加 Redis ensure/start helper | 复用或创建 `clawcu-a2a-redis` | Docker 命令测试 |
| T52 | 未开始 | 增加 worker companion name | `clawcu-a2a-worker-<instance>` | name 测试 |
| T53 | 未开始 | 实现 worker companion start | worker 容器共享主服务 network namespace | docker run 命令测试 |
| T54 | 未开始 | 实现 worker companion stop/remove | 删除实例时清理 worker companion | stop/remove 测试 |
| T55 | 未开始 | 实现 restart 顺序 | main service -> adapter -> worker | restart 命令顺序测试 |
| T56 | 未开始 | 注入 Redis 和 queue env | `A2A_REDIS_URL`、`A2A_QUEUE_NAME`、worker settings env | inspect/env 测试 |
| T57 | 未开始 | 更新 adapter Dockerfile | 安装 arq，支持 adapter/worker 两种入口 | image build 测试 |

## Phase 6: CLI / Inspect / Operator Surface

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T60 | 未开始 | 更新 inspect 输出 | 显示 async enabled、Redis URL、queue、worker 状态 | inspect 单元测试 |
| T61 | 未开始 | 更新 getenv 分组 | 将 async/Redis env 放入 A2A 分组 | getenv 渲染测试 |
| T62 | 未开始 | 明确 Redis 不可用提示 | create/restart/inspect 给出可操作错误或 warning | CLI 测试 |
| T63 | 未开始 | 评估是否增加 CLI task 命令 | 可选 `clawcu a2a task get/cancel/events` | 决策记录或实现测试 |

## Phase 7: Tests

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T70 | 未开始 | task facade 单元测试 | `tests/a2a_adapter/test_tasks.py` | pytest pass |
| T71 | 未开始 | worker 单元测试 | `tests/a2a_adapter/test_worker.py` | pytest pass |
| T72 | 未开始 | server async JSON-RPC 测试 | async dispatch、sync fallback、disabled path | pytest pass |
| T73 | 未开始 | MCP async 工具测试 | async call/get/cancel 三工具 | pytest pass |
| T74 | 未开始 | SSE route 测试 | replay、heartbeat、terminal close | pytest pass |
| T75 | 未开始 | Docker orchestration 测试 | Redis、adapter、worker start/stop/restart | pytest pass |
| T76 | 未开始 | Redis integration smoke test | 使用 `clawcu-a2a-redis` 跑最小 async job | smoke pass |
| T77 | 未开始 | 全量相关测试 | `pytest tests/a2a_adapter tests/test_service.py tests/test_cli.py` | 测试通过 |

## Phase 8: Docs

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T80 | 未开始 | 更新 A2A 协议文档 | async dispatch、task get、cancel、events | 文档审阅 |
| T81 | 未开始 | 更新 env 文档 | Redis/arq/task retention/env defaults | 文档审阅 |
| T82 | 未开始 | 更新 release notes 草稿 | 说明 Redis 依赖、默认 sync、async opt-in | 文档审阅 |
| T83 | 未开始 | 更新 troubleshooting | Redis 不可用、worker missing、task stuck、cancel best-effort | 文档审阅 |

## Phase 9: Rollout / Commit Tracking

| ID | 状态 | 任务 | 产出 | 验证 |
| --- | --- | --- | --- | --- |
| T90 | 未开始 | Phase 1 hidden plumbing commit | task facade + worker 基础，不默认暴露 async | 已提交 |
| T91 | 未开始 | Phase 2 opt-in async commit | feature flag 下暴露 async MCP/API | 已提交 |
| T92 | 未开始 | Phase 3 orchestration commit | Redis/worker companion 管理 | 已提交 |
| T93 | 未开始 | Phase 4 docs/tests commit | 文档和测试补齐 | 已提交 |

