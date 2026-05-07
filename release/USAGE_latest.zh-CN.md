# ClawCU Usage Latest

🌐 Language:
[English](USAGE_latest.md) | [中文](USAGE_latest.zh-CN.md)

发布范围：当前版本

当前 `ClawCU` 命令界面参考。覆盖 OpenClaw 与 Hermes 共享生命周期命令、provider 管理、Dashboard 常驻容器、孤儿实例恢复、A2A companion adapter，以及运行时默认值。

## 1. 初始化与镜像准备

### `clawcu --version`

```
clawcu --version
```

显示已安装的 ClawCU 版本。

### `clawcu setup`

```
clawcu setup [--completion]
```

检查 Docker CLI、Docker daemon 可达性、ClawCU home 与运行目录，并交互式配置默认 ClawCU home、OpenClaw image repo、Hermes image repo。

## 2. 实例创建

### `clawcu create openclaw`

```
clawcu create openclaw --name <name> --version <version>
                       [--datadir <path>] [--port <port>]
                       [--cpu 1] [--memory 2g]
                       [--a2a]
```

创建并启动 OpenClaw 实例。

- `datadir` 默认 `~/.clawcu/<name>`
- 默认端口 `18799`，冲突时以 `+10` 步长探测
- 写入 `.clawcu-instance.json` 元数据 sidecar，仅凭 datadir 即可恢复
- `--a2a` —— 启动 companion A2A adapter 容器（见 §11）。发布额外本地端口，暴露 `GET /.well-known/agent-card.json`、JSON-RPC `message/send` 和 `POST /mcp`。原生行为不变。

### `clawcu create hermes`

```
clawcu create hermes --name <name> --version <ref>
                     [--datadir <path>] [--port <port>]
                     [--cpu 1] [--memory 2g]
                     [--a2a]
```

创建并启动 Hermes 实例。

- `datadir` 默认 `~/.clawcu/<name>`
- API 端口默认 `8652`，仪表盘端口起点 `9129`
- 两者均以 `+10` 步长探测
- 同样写入 `.clawcu-instance.json` sidecar
- `--a2a` —— 启动 companion A2A adapter 容器（见 §11）。adapter 共享服务容器网络，暴露 AgentCard、JSON-RPC 消息和 `POST /mcp`。

## 3. 共享生命周期命令

### `clawcu list` _(别名：`ls`)_

```
clawcu list [--source managed|local|removed|all]
            [--local] [--managed] [--all] [--removed]
            [--service X] [--status X] [--running]
            [--agents] [--wide] [--reveal]
            [--versions] [--remote/--no-remote] [--no-cache]
            [--json]
```

列出实例摘要或逐 agent 行。默认 source 为 `managed`。

- `--local` / `--managed` / `--all` / `--removed` —— source 快捷别名；冲突组合会以一行错误拒绝
- `--removed` —— 列出 `CLAWCU_HOME` 下记录已消失的 datadir；每条尝试从 `.clawcu-instance.json` 还原 service / version / port（`v0.2.6` 之前的老 datadir 无 sidecar，缺失字段显示 `-`）
- `--agents` —— 逐 agent 一行，而非逐实例
- `--wide` —— 在窄模式 6 列之上追加 SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT
- `--reveal` —— 显示完整 dashboard token
- `--versions` —— 追加每个服务（OpenClaw、Hermes）最多 10 个 "Available versions" 页脚。从配置的 registry 拉取，按天缓存到 `<clawcu_home>/cache/available_versions.json`
- `--remote` / `--no-remote` —— 与 `--versions` 联用时控制 registry 拉取（默认开）。传 `--no-remote` 获得离线视图（CI、气隙网络、慢网络）
- `--no-cache` —— 与 `--versions` 联用时跳过本地缓存，强制重新拉取远程 tag
- `--json` —— 脚本友好的实例数组（契约不变；版本页脚只在文本模式下渲染）

### `clawcu inspect`

```
clawcu inspect <name> [--show-history] [--reveal]
```

紧凑可读的实例状态视图（摘要 / access / 快照 / 容器 / 历史）。历史默认折叠。

- `--show-history` —— 展开历史段
- `clawcu --json inspect <name>` —— 完整原始 JSON
- `--reveal` —— 明文显示 token

### `clawcu start`

```
clawcu start <name>
```

启动处于 stopped 状态的受管实例。

### `clawcu stop`

```
clawcu stop <name> [--time N | -t N]
```

停止运行中实例。`--time` 为优雅关机秒数（默认 `5`），传给 `docker stop --time`。

### `clawcu restart`

```
clawcu restart <name> [--no-recreate-if-config-changed]
```

重启实例。**默认**：若检测到环境变量漂移或容器缺失，会升级为完整 `recreate`。

- `--no-recreate-if-config-changed` —— 强制走 `docker restart`

### `clawcu recreate`

```
clawcu recreate <name> [--fresh] [--timeout N]
                       [--version <v>] [--yes]
```

按保存配置重建容器，或从遗留 datadir 恢复一个已删除的实例。自动重试 `create_failed` 状态。

- `--fresh` —— 重建前清空 datadir（破坏性，非 `--yes` 时会询问）
- `--timeout N` —— 强制删除前的优雅 stop 窗口
- `--version <v>` —— 恢复不带 `.clawcu-instance.json` 的老 datadir 时显式钉住版本

### `clawcu upgrade`

```
clawcu upgrade <name> [--version <v>] [--list-versions]
                      [--remote/--no-remote] [--all-versions]
                      [--dry-run] [--yes] [--json]
```

升级到新版本。替换容器前对 datadir 与对应环境变量路径做 snapshot。

- `--list-versions` —— 列候选：实例历史 + 本地 Docker 镜像 +（`--remote`，默认开）registry v2 API 上的 release tag。remote 拉取尽力而为，失败回退到本地
- `--no-remote` —— 完全跳过 registry 拉取
- `--all-versions` —— 列完整 remote tag（默认截到最近 10 个）
- `--json` —— 总是返回完整 tag 列表
- `--dry-run` —— 只打印计划不动 Docker / 磁盘
- `--yes` / `-y` —— 跳过计划确认（非交互环境必需）

### `clawcu rollback`

```
clawcu rollback <name> [--to <version>] [--list]
                       [--dry-run] [--yes] [--json]
```

回滚到更早的 snapshot。不带 `--to` 时回滚最近一次可逆转换。

- `--to <version>` —— 选择最近一次"回复到该版本"的历史事件
- `--list` —— 枚举所有 snapshot 目标
- `--dry-run` / `--yes` / `--json` —— 与 `upgrade` 一致

### `clawcu clone`

```
clawcu clone <source> --name <name>
                      [--datadir <path>] [--port <port>]
                      [--version <v>]
                      [--include-secrets/--exclude-secrets]
```

把 source 实例复制成新的隔离实验实例。datadir 总是复制。

- `--include-secrets`（默认）/ `--exclude-secrets` —— 是否复制 source 的环境变量文件（API key / token / provider 凭据）。默认复制；`--exclude-secrets` 以空环境变量起步
- `--version <v>` —— 复制时切换 service 版本（安全的 "clone then upgrade"）

### `clawcu logs`

```
clawcu logs <name> [--follow] [--tail N] [--since DURATION]
```

显示实例日志。默认最近 200 行。

- `--follow` —— 持续流式
- `--tail 0` —— 流式完整历史
- `--since DURATION` —— 跳过早于 DURATION 的日志

### `clawcu remove` _(别名：`rm`)_

```
clawcu remove <name> [--keep-data|--delete-data]
                     [--removed] [--yes]
```

移除受管实例。默认保留 datadir。

- `--delete-data` —— 同时删除 datadir
- `--removed` —— 永久删除 `clawcu list --removed` 列出的孤儿 datadir。此模式下 `--keep-data` / `--delete-data` 会被拒绝（`--removed` 必定删除）
- 自动提示：当对记录已消失但 datadir 仍在的实例执行 `remove <name>` 时，CLI 会一步提示并删除孤儿数据（无需重新加 `--removed` 执行）

## 4. 孤儿实例生命周期

当实例记录丢失时（registry 损坏、还原备份、中断的 `create` 遗留），其 datadir 会成为"孤儿"——仍在 `CLAWCU_HOME` 下，但已不被跟踪。`v0.2.8` 提供完整恢复路径：

| 步骤 | 命令 | 说明 |
|------|------|------|
| 发现 | `clawcu list --removed` | 枚举孤儿 datadir，并从 `.clawcu-instance.json` 还原 service / version / port。 |
| 恢复 | `clawcu recreate <orphan>` | 从孤儿 datadir 重建受管实例。端口 / 版本 / 服务均由元数据还原。 |
| 恢复（老 datadir） | `clawcu recreate <orphan> --version <v>` | 恢复 `.clawcu-instance.json` 出现之前的 datadir（`v0.2.5` 及更早）。`--version` 显式钉住目标版本。 |
| 永久删除 | `clawcu remove <orphan> --removed [--yes]` | 清理孤儿 datadir。此模式下 `--keep-data` / `--delete-data` 会被拒绝。 |

`v0.2.8` 起 `clawcu recreate` 与 `clawcu upgrade` 都会刷新 `.clawcu-instance.json`，恢复后的实例自动回到"自描述"状态。

## 5. 交互访问与原生命令

### `clawcu config`

```
clawcu config <name> [-- args...]
```

在受管容器内执行服务原生配置流程。OpenClaw 对应 `openclaw configure`，Hermes 对应 `hermes setup`。

### `clawcu exec`

```
clawcu exec <name> <command...>
```

在受管容器内执行任意命令，注入实例环境变量。

### `clawcu tui`

```
clawcu tui <name> [--agent <agent>]
```

启动原生交互流程。OpenClaw 用其 TUI，Hermes 用其交互 chat。

## 6. Dashboard

### `clawcu dashboard`

```
clawcu dashboard [--host HOST] [--port PORT]
                 [--open/--no-open]
                 [--stop] [--restart] [--status] [--rebuild]
```

将 ClawCU dashboard 作为 Docker 容器在后台常驻运行和管理。

- 默认（无 flag）— 确保 dashboard 镜像存在（首次运行自动构建），如未运行则启动容器，然后打开浏览器
- `--stop` — 停止并删除 dashboard 容器
- `--restart` — 停止后重新启动容器（配置变更后可用）
- `--status` — 打印容器状态、镜像标签、URL 和健康检查
- `--rebuild` — 强制重建 dashboard Docker 镜像（升级 ClawCU 后使用）
- `--host` / `--port` — 控制 dashboard 发布的本地接口和端口（默认 `127.0.0.1:8765`）

Dashboard 容器会挂载以下宿主机路径：
- `~/.clawcu` → 容器内 StateStore 数据
- `~/.openclaw` / `~/.hermes` → 本地实例检测
- `/var/run/docker.sock` → 容器内省和日志读取

## 7. 服务相关访问命令

### `clawcu token` _(仅 OpenClaw)_

```
clawcu token <name> [--copy] [--url-only|--token-only] [--json]
```

打印 OpenClaw 仪表盘 token。默认同时显示 token 与带 `#token=…` 的访问 URL。Hermes 实例会失败并提示去用 `clawcu config <name>`（原生认证）。

- `--copy` —— 推入系统剪贴板（pbcopy / xclip / wl-copy / clip）
- `--url-only` / `--token-only` —— 便于脚本

### `clawcu approve` _(仅 OpenClaw)_

```
clawcu approve <name> [requestId]
```

批准 OpenClaw 浏览器配对请求。Hermes 实例会以 unsupported 失败。

## 7. 环境变量管理

### `clawcu setenv`

```
clawcu setenv <name> KEY=VALUE [KEY=VALUE ...]
                     [--from-file <path>]
                     [--dry-run] [--reveal] [--apply]
```

向实例环境变量文件写入变量。内联 `KEY=VALUE` 与 `--from-file <path>` 互斥。

- `--dry-run` —— 带颜色的 `+/-/~` diff 预览
- `--reveal` —— 显示敏感值（默认对 `KEY` / `TOKEN` / `SECRET` / `PASSWORD` 掩码）
- `--apply` —— 立即 recreate 容器令 Docker 重读环境变量

### `clawcu getenv`

```
clawcu getenv <name> [--reveal] [--json]
```

打印实例当前环境变量。敏感值默认掩码，`--reveal` 明示。

### `clawcu unsetenv`

```
clawcu unsetenv <name> KEY [KEY ...]
                       [--dry-run] [--reveal] [--apply]
```

删除环境变量。

- `--dry-run` —— 预览将移除哪些键（不存在的键标注 no-op）
- `--apply` —— 立即 recreate

## 8. 模型配置的采集与复用

### `clawcu provider collect`

```
clawcu provider collect (--all | --instance <name> | --path <home>)
```

采集模型配置资产。

- `--all` —— 从所有受管实例加上本地 `~/.openclaw` / `~/.hermes`（存在时）
- `--instance <name>` —— 从一个受管实例
- `--path <home>` —— 从任意 OpenClaw / Hermes home 目录

### `clawcu provider list`

```
clawcu provider list
```

列出已采集的模型配置资产，带 service 身份与掩码 API key 摘要。

### `clawcu provider show`

```
clawcu provider show <name>
```

查看某资产的落地 payload（密码掩码）。跨服务同名时用 `openclaw:<name>` / `hermes:<name>` 消歧义。

### `clawcu provider remove`

```
clawcu provider remove <name>
```

删除已采集资产。

### `clawcu provider models list`

```
clawcu provider models list <name>
```

列出某资产中的模型。

### `clawcu provider apply`

```
clawcu provider apply <provider> <instance>
                      [--agent <agent>]
                      [--primary <model>]
                      [--fallbacks <m1,m2>]
                      [--persist]
```

把已采集资产应用到目标实例。回写方式服务原生。

- `--agent` —— 目标 agent；默认 `main`
- `--primary <model>` —— 设主模型
- `--fallbacks <m1,m2>` —— 设 fallback 链
- `--persist` —— 立即写盘

## 9. 默认行为约定

- 端口默认：
  - OpenClaw 受管实例从 `18799` 起
  - Hermes 受管 API 端口从 `8652` 起
  - Hermes 仪表盘端口从 `9129` 起
  - 冲突时均以 `+10` 步长探测
- 资源：
  - 默认 `1 CPU + 2GB RAM`
- 数据目录：
  - 默认 `~/.clawcu/<instance-name>`
- 容器命名：
  - `clawcu-<service>-<instance-name>`
- Datadir 元数据：
  - 每个实例携带 `.clawcu-instance.json`，含 service / version / port / created-at
  - 支持零输入孤儿恢复
- 访问信息：
  - 两个服务的 `create` / `list` / `inspect` 均暴露 access URL
  - OpenClaw 显示主服务端口与仪表盘 URL
  - Hermes 显示仪表盘端口与 URL；就绪探测可能也用 API server
- Env 位置：
  - OpenClaw 使用 `~/.clawcu/instances/<instance>.env`
  - Hermes 使用 `<datadir>/.env`
- 快照行为：
  - `upgrade` 与 `rollback` 同时保存并还原 datadir 与对应环境变量路径
- 推荐升级策略：
  - 先 `clone`、在 clone 上 `upgrade`、验证，必要时 `rollback`
- 孤儿恢复：
  - `list --removed` → `recreate <orphan>`（端口 / 版本自动从 `.clawcu-instance.json` 还原）

## 11. Agent-to-Agent 消息（`v0.4.2`）

`v0.4.2` 引入 companion A2A adapter 表面。所有 A2A 行为通过创建时的 `--a2a` 开启；不加则已有实例一切不变。

### 创建时 opt-in

给 `clawcu create openclaw` 或 `clawcu create hermes` 传 `--a2a`，ClawCU 会在服务容器旁边启动 companion A2A adapter 容器。adapter 镜像共享为 `clawcu/a2a-adapter:<clawcu_version>`。

启用 A2A 的实例在本地 adapter 端口上暴露：

- `GET /.well-known/agent-card.json` —— 标准 Google A2A AgentCard
- `POST /` —— JSON-RPC 2.0 A2A `message/send`
- `POST /mcp` —— MCP JSON-RPC 工具表面，暴露 `a2a_call_peer`

启用 A2A 时，ClawCU 还会把 `mcp.servers.a2a` 写入服务配置，让本地 agent 能通过 `a2a_call_peer` MCP 工具调用对端。

运维说明：

- adapter 会把入站 A2A 消息转发到同容器网络里的服务网关 `/v1/chat/completions`。
- registry 通过 `GET /agents` 和 `GET /agents/<name>` 聚合卡片。
- `clawcu inspect <instance>` 会显示 A2A 端口、registry URL、hop budget 和 MCP URL。

### `clawcu a2a card`

```
clawcu a2a card [--name <instance>] [--host 127.0.0.1]
```

打印某个受管实例的 AgentCard JSON（从记录推导）。不传 `--name` 则以 JSON 数组吐出所有受管实例的卡片。

### `clawcu a2a registry serve`

```
clawcu a2a registry serve [--port 9100] [--host 127.0.0.1]
                         [--provider probe|redis]
                         [--redis-url redis://host.docker.internal:6379/0]
```

运行前台/debug registry server：通过 HTTP 提供 `GET /agents`（数组）与 `GET /agents/<name>`（单张卡片）。正常 A2A lifecycle 会自动管理 Dockerized `clawcu-a2a-registry`；`--provider redis` 读取 Redis-backed peer snapshots，`--provider probe` 保留旧的实时探测模式。

### `clawcu a2a send`

```
clawcu a2a send --to <target> --message <text>
                [--registry http://127.0.0.1:9100]
                [--from clawcu-cli] [--timeout 60]
```

在 registry 里查 `TARGET`，向其 endpoint 发送 A2A JSON-RPC `message/send` 请求。打印回复 JSON。`--timeout` 是等待 LLM 回复的时长。

### `clawcu hermes identity set`

```
clawcu hermes identity set <instance> <soul-path>
```

把用户写的 `SOUL.md` 装进 Hermes 实例 datadir。`prompt_builder.load_soul_md` 下一轮对话就能用新人格 —— 不用重启，不用 recreate。

## 12. 备注

- 本文档描述当前命令界面。
- 发布上下文见 [RELEASE_latest.zh-CN.md](RELEASE_latest.zh-CN.md)。
- 快捷方式：[USAGE_latest.zh-CN.md](USAGE_latest.zh-CN.md) 始终指向当前发布版本。
