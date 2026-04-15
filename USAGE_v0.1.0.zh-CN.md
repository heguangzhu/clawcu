# ClawCU 使用说明 v0.1.0

🌐 Language:
[English](USAGE_v0.1.0.md) | [中文](USAGE_v0.1.0.zh-CN.md)

版本范围：`v0.1.0`

这份文档是 `ClawCU v0.1.0` 的命令使用说明。

它聚焦于每条命令是做什么的、按什么类别组织，以及用户在日常运维里可以预期什么行为。

## 1. 版本与镜像

| 命令 | 说明 |
|------|------|
| `clawcu --version` | 查看已安装的 ClawCU 版本。 |
| `clawcu setup [--completion]` | 检查 Docker、ClawCU home、运行目录，并交互式配置默认 ClawCU home 与 OpenClaw 镜像仓库。只有在传 `--completion` 时才显示 shell completion 指引。 |
| `clawcu pull openclaw --version <version>` | 拉取指定版本的 OpenClaw 官方镜像。如果官方镜像不存在或拉取失败，ClawCU 会直接报错。 |

## 2. 实例生命周期

| 命令 | 说明 |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--port <port>] [--cpu 1] [--memory 2g]` | 创建并启动 OpenClaw 实例。`datadir` 默认是 `~/.clawcu/<name>`。宿主机端口默认从 `18789` 开始，冲突时按 `+10` 探测。ClawCU 会自动配置 Gateway 默认值、等待健康检查 ready，并输出 Dashboard URL。 |
| `clawcu list [--running] [--managed\|--local] [--agents]` | 列出实例视图或 agent 视图。默认同时显示本地 `~/.openclaw` 和托管实例。 |
| `clawcu inspect <name>` | 查看实例详细状态，包括 Docker 容器信息、历史记录和快照摘要。 |
| `clawcu token <name>` | 输出实例 Dashboard token。 |
| `clawcu start <name>` | 启动已停止实例。 |
| `clawcu stop <name>` | 停止运行中实例。 |
| `clawcu restart <name>` | 重启实例。 |
| `clawcu remove <name> [--keep-data\|--delete-data]` | 删除实例和容器，并选择保留或删除数据目录。 |

## 3. 故障恢复

| 命令 | 说明 |
|------|------|
| `clawcu retry <name>` | 重试一个处于 `create_failed` 状态的实例。 |
| `clawcu recreate <name>` | 根据已保存配置重建实例容器，同时保持原有实例配置不变。 |

## 4. 版本管理

| 命令 | 说明 |
|------|------|
| `clawcu upgrade <name> --version <version>` | 把实例升级到指定新版本。ClawCU 会在升级前快照数据目录和 env 文件，等待 ready，并在失败时尝试自动回滚。 |
| `clawcu rollback <name>` | 基于匹配的快照和 env 快照，把实例回滚到上一个版本。 |

## 5. 实验与克隆

| 命令 | 说明 |
|------|------|
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>]` | 复制源实例的数据目录和 env 文件，创建一个隔离的实验实例。 |

## 6. 访问与配置

| 命令 | 说明 |
|------|------|
| `clawcu approve <name> [requestId]` | 审批一个待处理的浏览器 pairing 请求。如果不传 `requestId`，ClawCU 会自动审批最新的一条。 |
| `clawcu config <name> [-- args...]` | 在托管容器内透传调用 `openclaw configure`。额外参数通过 `--` 传递，例如：`clawcu config my-instance -- --section model`。 |
| `clawcu exec <name> <command...>` | 在托管实例容器内执行任意命令。 |
| `clawcu tui <name> [--agent <agent>]` | 启动 OpenClaw TUI。进入 TUI 前，ClawCU 会自动处理常见的本地 approve 流程。 |
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE...] [--apply]` | 把环境变量写入实例 env 文件，并可选立即 `recreate`。 |
| `clawcu getenv <name>` | 输出实例 env 文件内容。 |
| `clawcu unsetenv <name> KEY [KEY...] [--apply]` | 从实例 env 文件中删除环境变量，并可选立即 `recreate`。 |
| `clawcu logs <name> [--follow]` | 查看实例日志；`--follow` 会持续跟随输出。 |

## 7. 模型配置收集与复用

| 命令 | 说明 |
|------|------|
| `clawcu provider collect --all` | 从所有托管实例以及本地 `~/.openclaw` 收集已启用的模型/provider 配置信息。 |
| `clawcu provider collect --instance <name>` | 从单个托管实例收集模型/provider 配置信息。 |
| `clawcu provider collect --path <openclaw-home>` | 从任意 OpenClaw 数据目录收集模型/provider 配置信息。 |
| `clawcu provider list` | 列出已收集的 provider，并脱敏显示 API key 摘要。 |
| `clawcu provider show <name>` | 查看某个 provider 的 `auth-profiles.json` 和 `models.json`，显示时会对敏感值做脱敏。 |
| `clawcu provider remove <name>` | 删除一个已收集的 provider 目录。 |
| `clawcu provider models list <name>` | 查看某个 provider 中包含的模型列表。 |
| `clawcu provider apply <provider> <instance> [--agent <agent>] [--primary <model>] [--fallbacks <m1,m2>] [--persist]` | 把一个已收集的 provider 应用到指定实例的 agent 上。`--agent` 默认是 `main`。 |

## 8. 默认行为约定

- 端口：
  默认使用 `18789` 作为 OpenClaw Gateway 的宿主机映射端口。冲突时按 `18789 -> 18799 -> 18809 ...` 探测。
- 资源：
  默认 `1 CPU + 2GB RAM`。
- 认证：
  强制 token 模式，因为当前 OpenClaw 的 `lan` 绑定要求启用认证。
- 数据目录：
  默认是 `~/.clawcu/<instance-name>`。
- 容器命名：
  `clawcu-openclaw-<instance-name>`。
- 本地镜像标签：
  `clawcu/openclaw:<version>`。
- Gateway 默认配置：
  ClawCU 会自动写入 `bind=lan`、`auth.mode=token`、`controlUi.allowedOrigins=["*"]`。
- 健康等待：
  ClawCU 会轮询 `http://127.0.0.1:<port>/healthz`，直到实例真正 ready 或进入失败状态。
- Dashboard URL：
  在创建、升级、回滚、克隆、重建成功后，ClawCU 会输出 `http://127.0.0.1:<port>/#token=<token>`。
- 环境变量：
  实例 env 文件保存在 `~/.clawcu/instances/<instance>.env`，`recreate`、`upgrade`、`rollback`、`clone` 都会显式处理它。
- 升级策略：
  推荐先 `clone`，再在克隆实例上 `upgrade`，验证完成后不满意再 `rollback`。

## 9. 说明

- 这份使用说明描述的是 `v0.1.0` 的实际命令面。
- 发布背景见 [RELEASE_v0.1.0.zh-CN.md](RELEASE_v0.1.0.zh-CN.md)。
