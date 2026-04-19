# ClawCU Usage v0.2.8

🌐 Language:
[English](USAGE_v0.2.8.md) | [中文](USAGE_v0.2.8.zh-CN.md)

发布范围：`v0.2.8`

本文是 `ClawCU v0.2.8` 的命令参考。

覆盖 OpenClaw 与 Hermes 共享的命令界面、两者的差异、在 `v0.2.6` 引入的孤儿实例生命周期，以及 ClawCU 的运行时默认值。

## 1. 初始化与镜像准备

| 命令 | 说明 |
|------|------|
| `clawcu --version` | 显示已安装的 ClawCU 版本。 |
| `clawcu setup [--completion]` | 检查 Docker CLI、Docker daemon 可达性、ClawCU home、运行目录，并交互式配置默认 ClawCU home、OpenClaw image repo 与 Hermes image repo。 |
| `clawcu pull openclaw --version <version>` | 预取官方 OpenClaw 镜像引用。本地缺失镜像时，`create` / `start` / `recreate` 会在需要时触发 Docker 下载。 |
| `clawcu pull hermes --version <tag>` | 从配置的 Hermes image repo 拉取对应 tag 的预构建镜像。 |

## 2. 实例创建

| 命令 | 说明 |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | 创建并启动 OpenClaw 实例。`datadir` 默认 `~/.clawcu/<name>`。默认端口 `18799`，冲突时以 `+10` 步长探测。同时写入 `.clawcu-instance.json` 元数据 sidecar，实例可仅凭 datadir 恢复。 |
| `clawcu create hermes --name <name> --version <ref> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | 创建并启动 Hermes 实例。`datadir` 默认 `~/.clawcu/<name>`。API 端口默认 `8652`，同时分配仪表盘端口起点 `9129`。均以 `+10` 步长探测。`.clawcu-instance.json` sidecar 同样会写入。 |

## 3. 共享生命周期命令

| 命令 | 说明 |
|------|------|
| `clawcu list [--source managed\|local\|removed\|all] [--local] [--managed] [--all] [--removed] [--service X] [--status X] [--running] [--agents] [--wide] [--reveal] [--json]` | 别名 `ls`。列出实例摘要或逐 agent 行。默认 source 为 `managed`。`--local` / `--managed` / `--all` / `--removed` 是 source 快捷别名；冲突组合会一行错误拒绝。`--removed` 列出 `CLAWCU_HOME` 下记录已消失的 datadir——每条尝试从 `.clawcu-instance.json` 还原 service / version / port（`v0.2.6` 之前的老 datadir 无此 sidecar，缺失字段显示 `-`）。窄模式 6 列；`--wide` 追加 SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT。`--reveal` 显示完整 dashboard token。 |
| `clawcu inspect <name> [--show-history] [--reveal]` | 以紧凑可读视图展示实例细节（摘要 / access / 快照 / 容器 / 历史）。历史默认折叠，`--show-history` 展开，或用 `clawcu --json inspect <name>` 拿到原始 JSON。`--reveal` 明文显示 token。 |
| `clawcu start <name>` | 启动处于 stopped 状态的受管实例。 |
| `clawcu stop <name> [--time N / -t N]` | 停止运行中实例。`--time` 是优雅关机秒数（默认 5），传给 `docker stop --time`。 |
| `clawcu restart <name> [--no-recreate-if-config-changed]` | 重启实例。**默认**：若检测到环境变量漂移或容器缺失，会升级为完整 `recreate`。传 `--no-recreate-if-config-changed` 强制走 `docker restart`。 |
| `clawcu recreate <name> [--fresh] [--timeout N] [--version <v>] [--yes]` | 按保存配置重建容器，或从遗留 datadir 恢复一个已删除的实例。自动重试 `create_failed` 状态。`--fresh` 重建前清空 datadir（破坏性，非 `--yes` 时会询问）。`--timeout` 覆盖强制删除前的优雅 stop 窗口。`--version <v>` 用于恢复不带 `.clawcu-instance.json` 的老 datadir。 |
| `clawcu upgrade <name> [--version <v>] [--list-versions] [--remote/--no-remote] [--all-versions] [--dry-run] [--yes] [--json]` | 升级到新版本。替换容器前对 datadir 与对应环境变量路径做 snapshot。`--list-versions` 列候选：实例历史 + 本地 Docker 镜像 + （`--remote`，默认开）registry v2 API 上的 release tag。remote 拉取尽力而为，失败回退到本地。`--no-remote` 完全跳过 registry。remote 段默认截到最近 10 个 tag，`--all-versions` 列全量。`--json` 总是返回全量 tag。`--dry-run` 只打印计划不动 Docker / 磁盘。正常路径先显示计划再询问，`--yes` / `-y` 跳过（非交互环境必需）。 |
| `clawcu rollback <name> [--to <version>] [--list] [--dry-run] [--yes] [--json]` | 回滚到更早的 snapshot。不带 `--to` 时回滚最近一次可逆转换。`--to <version>` 选择最近一次"回复到该版本"的历史事件。`--list` 枚举所有 snapshot 目标。`--dry-run` / `--yes` / `--json` 行为与 `upgrade` 一致。 |
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>] [--version <v>] [--include-secrets/--exclude-secrets]` | 把一个 source 实例复制成新的隔离实验实例。datadir 总是复制。默认环境变量文件（API key / token / provider 凭据）也会复制，传 `--exclude-secrets` 以空环境变量起步。`--version <v>` 在复制时切换 service 版本（安全的 "clone then upgrade"）。 |
| `clawcu logs <name> [--follow] [--tail N] [--since DURATION]` | 显示实例日志。默认最近 200 行。`--follow` 持续流式；`--tail 0` 流式完整历史。 |
| `clawcu remove <name> [--keep-data\|--delete-data] [--removed] [--yes]` | 别名 `rm`。移除受管实例（默认保留 datadir）。传 `--removed` 从 `clawcu list --removed` 永久删除孤儿 datadir——此模式下 `--keep-data` / `--delete-data` 会被拒绝，因 `--removed` 必定删除。 |

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

| 命令 | 说明 |
|------|------|
| `clawcu config <name> [-- args...]` | 在受管容器内执行服务原生配置流程。OpenClaw 对应 `openclaw configure`，Hermes 对应 `hermes setup`。 |
| `clawcu exec <name> <command...>` | 在受管容器内执行任意命令，注入实例环境变量。 |
| `clawcu tui <name> [--agent <agent>]` | 启动原生交互流程。OpenClaw 用其 TUI，Hermes 用其交互 chat。 |

## 6. 服务相关访问命令

| 命令 | 说明 |
|------|------|
| `clawcu token <name> [--copy] [--url-only\|--token-only] [--json]` | 打印 OpenClaw 仪表盘 token。默认同时显示 token 与带 `#token=…` 的访问 URL。`--copy` 推入系统剪贴板（pbcopy/xclip/wl-copy/clip）。`--url-only` / `--token-only` 便于脚本。Hermes 实例会失败并提示去用 `clawcu config <name>`（原生认证）。 |
| `clawcu approve <name> [requestId]` | 批准 OpenClaw 浏览器配对请求。仅 OpenClaw。Hermes 会显式拒绝。 |

## 7. 环境变量管理

| 命令 | 说明 |
|------|------|
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE ...] [--from-file <path>] [--dry-run] [--reveal] [--apply]` | 向实例环境变量文件写入变量。内联 `KEY=VALUE` 与 `--from-file <path>` 互斥。`--dry-run` 打印带颜色的 `+/-/~` diff，敏感值（`KEY`/`TOKEN`/`SECRET`/`PASSWORD`）默认掩码，`--reveal` 明示。`--apply` 立即 recreate 容器令 Docker 重读环境变量。 |
| `clawcu getenv <name> [--reveal] [--json]` | 打印实例当前环境变量。敏感值默认掩码，`--reveal` 明示。 |
| `clawcu unsetenv <name> KEY [KEY ...] [--dry-run] [--reveal] [--apply]` | 删除环境变量。`--dry-run` 预览将移除哪些键（不存在的键标注 no-op）。`--apply` 立即 recreate。 |

## 8. 模型配置的采集与复用

| 命令 | 说明 |
|------|------|
| `clawcu provider collect --all` | 从所有受管实例加上本地 `~/.openclaw` / `~/.hermes`（存在时）采集模型配置。 |
| `clawcu provider collect --instance <name>` | 从一个受管实例采集。 |
| `clawcu provider collect --path <home>` | 从任意 OpenClaw / Hermes home 目录采集。 |
| `clawcu provider list` | 列出已采集的模型配置资产，带 service 身份和掩码 API key 摘要。 |
| `clawcu provider show <name>` | 查看某资产的落地 payload（密码掩码）。跨服务同名时用 `openclaw:<name>` / `hermes:<name>` 消歧义。 |
| `clawcu provider remove <name>` | 删除已采集资产。 |
| `clawcu provider models list <name>` | 列出某资产中的模型。 |
| `clawcu provider apply <provider> <instance> [--agent <agent>] [--primary <model>] [--fallbacks <m1,m2>] [--persist]` | 把已采集资产应用到目标实例。`--agent` 默认 `main`。回写方式服务原生。 |

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

## 10. 备注

- 本文档描述 `v0.2.8` 命令界面。
- 发布上下文见 [RELEASE_v0.2.8.zh-CN.md](RELEASE_v0.2.8.zh-CN.md)。
- `v0.2.0` 归档使用说明仍可参考 [USAGE_v0.2.0.zh-CN.md](USAGE_v0.2.0.zh-CN.md)；`v0.1.0` 见 [USAGE_v0.1.0.zh-CN.md](USAGE_v0.1.0.zh-CN.md)。
