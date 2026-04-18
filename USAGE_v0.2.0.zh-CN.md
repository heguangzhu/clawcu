# ClawCU 使用说明 v0.2.0

🌐 Language:
[English](USAGE_v0.2.0.md) | [中文](USAGE_v0.2.0.zh-CN.md)

版本范围：`v0.2.0`

这份文档是 `ClawCU v0.2.0` 的命令使用说明。

它描述的是共享命令面、OpenClaw 与 Hermes 的服务差异，以及 ClawCU 当前采用的默认行为。

## 1. setup 与制品准备

| 命令 | 说明 |
|------|------|
| `clawcu --version` | 显示当前安装的 ClawCU 版本。 |
| `clawcu setup [--completion]` | 检查 Docker CLI 是否可用、Docker daemon 是否可达、ClawCU home、运行目录，并交互式配置默认的 ClawCU home、OpenClaw 镜像源和 Hermes 镜像源。 |
| `clawcu pull openclaw --version <version>` | 为指定版本准备 OpenClaw 官方镜像引用。如果本地还没有该镜像，后续 `create`、`start` 或 `recreate` 需要它时，Docker 会自动拉取。 |
| `clawcu pull hermes --version <tag>` | 从配置好的 Hermes 镜像仓库拉取指定 tag 的预构建 Hermes 镜像。 |

## 2. 实例创建

| 命令 | 说明 |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | 创建并启动 OpenClaw 实例。`datadir` 默认是 `~/.clawcu/<name>`，托管实例的宿主机端口默认从 `18799` 开始，冲突时按 `+10` 探测，这样不会占用本地 OpenClaw 默认端口。 |
| `clawcu create hermes --name <name> --version <ref> [--datadir <path>] [--port <port>] [--cpu 1] [--memory 2g]` | 创建并启动 Hermes 实例。`datadir` 默认是 `~/.clawcu/<name>`，托管 API 端口默认从 `8652` 开始；ClawCU 还会额外分配一个从 `9129` 开始的托管 dashboard 端口。两者冲突时都按 `+10` 探测。 |

## 3. 共享生命周期命令

| 命令 | 说明 |
|------|------|
| `clawcu list [--source managed\|local\|all] [--service X] [--status X] [--running] [--agents] [--wide] [--reveal]` | 查看实例摘要或 agent 级视图。默认 `--source managed`，仅显示由 ClawCU 托管的实例；加 `--source local` 或 `--source all` 才包含 ~/.openclaw / ~/.hermes 下的伪实例。默认窄表 6 列（NAME / SERVICE / VERSION / PORT / STATUS / ACCESS host:port），`--wide` 追加 SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT 并显示完整 URL。`--reveal` 会打印 dashboard token 片段。 |
| `clawcu inspect <name> [--show-history] [--reveal]` | 以紧凑可读视图查看实例详细状态（摘要 / 访问 / 快照 / 容器 / 历史）。默认会把 history 折叠，只显示最新一条；需要完整历史请加 `--show-history`，需要原始 JSON 载荷请用 `clawcu --json inspect <name>`。`--reveal` 会显示未打码的 dashboard token。 |
| `clawcu start <name>` | 启动一个已停止的托管实例。 |
| `clawcu stop <name> [--time N / -t N]` | 停止一个正在运行的托管实例。`--time` 是优雅退出窗口（秒），会透传给 `docker stop --time`，默认 5 秒；把值调大可以让 OpenClaw/Hermes 的长任务在收到 SIGKILL 前先自行完成。 |
| `clawcu restart <name> [--no-recreate-if-config-changed]` | 重启一个托管实例。**默认开启**漂移检测：ClawCU 会先 inspect 容器，如发现 env 漂移（比如 `setenv` 没有 `--apply`）或容器已丢失，就自动把重启升级为完整 `recreate`，让新的 env 文件生效——和 `clawcu start` 现有行为一致。想强制走原生 `docker restart`（即使检测到漂移），加 `--no-recreate-if-config-changed` 关闭即可。 |
| `clawcu recreate <name>` | 按保存的实例配置重建容器；若实例处于 `create_failed`，会自动走重试路径。 |
| `clawcu upgrade <name> [--version <v>] [--list-versions] [--dry-run] [--yes] [--json]` | 将实例升级到新的服务版本或 tag。升级前会自动快照实例 home 和对应的 env 路径，env 文件会在升级过程中保留。`--list-versions` 不需要 `--version`，会列出候选版本（本地 Docker 上存在的对应 image 仓库的 tag，以及该实例的版本历史）。`--dry-run` 会打印升级计划（当前版本 → 目标版本、datadir、env 保留摘要、投射的 image tag、快照路径），不动 Docker 或磁盘。正常路径会先渲染计划然后要求二次确认——加 `--yes` / `-y` 跳过提示（非交互式 shell 里必须加）。`--json` 会把 plan / 版本列表以 JSON 形式输出。 |
| `clawcu rollback <name>` | 通过恢复匹配的快照和 env 快照，把实例回退到上一次可逆的版本切换。 |
| `clawcu clone <source> --name <name> [--datadir <path>] [--port <port>]` | 复制源实例，生成新的隔离实验实例。 |
| `clawcu logs <name> [--follow] [--tail N] [--since DURATION]` | 查看实例日志，默认只显示最后 200 行。`--follow` 会持续跟随输出；`--tail 0` 打印全部日志。 |
| `clawcu remove <name> [--keep-data\|--delete-data]` | 删除实例和容器，并选择是否保留数据目录。 |

## 4. 交互访问与原生命令

| 命令 | 说明 |
|------|------|
| `clawcu config <name> [-- args...]` | 在托管容器内运行服务原生配置流程。OpenClaw 对应 `openclaw configure`，Hermes 对应 `hermes setup`。 |
| `clawcu exec <name> <command...>` | 在托管容器内执行任意命令，并自动注入该实例对应的 env。 |
| `clawcu tui <name> [--agent <agent>]` | 启动该实例的原生交互入口。OpenClaw 走 TUI 流程，Hermes 走交互式 chat 流程。 |

## 5. 服务专属访问命令

| 命令 | 说明 |
|------|------|
| `clawcu token <name> [--copy] [--url-only\|--token-only] [--json]` | 输出 OpenClaw dashboard token。默认同时打印 token 和带 `#token=…` 锚点的完整访问 URL。`--copy` 会把 token 塞进系统剪贴板（pbcopy/xclip/wl-copy/clip）。`--url-only` / `--token-only` 适合脚本。Hermes 实例会返回"不支持"错误，并提示运行 `clawcu config <name>` 走 native auth 流程。 |
| `clawcu approve <name> [requestId]` | 审批 OpenClaw 的浏览器 pairing 请求。当前仅支持 OpenClaw。对 Hermes 会给出明确的“不支持”提示。 |

## 6. 环境变量管理

| 命令 | 说明 |
|------|------|
| `clawcu setenv <name> KEY=VALUE [KEY=VALUE ...] [--from-file <path>] [--dry-run] [--reveal] [--apply]` | 写入实例 env 文件。内联 `KEY=VALUE` 参数和 `--from-file <path>`（`.env` 风格，按 `KEY=VALUE` 逐行读入，`#` 注释和空行会被忽略）是互斥的。`--dry-run` 会以 `+/-/~` 彩色 diff 的形式打印与当前 env 的差异，不实际写入；敏感 key（`KEY`/`TOKEN`/`SECRET`/`PASSWORD`）会打码，加 `--reveal` 可显示原值。`--apply` 会立即执行 `recreate` 让 Docker 重新加载 env（不能与 `--dry-run` 同时使用）。 |
| `clawcu getenv <name> [--reveal] [--json]` | 输出当前实例配置的环境变量。敏感 key 默认会打码，加 `--reveal` 可显示原值。 |
| `clawcu unsetenv <name> KEY [KEY ...] [--dry-run] [--reveal] [--apply]` | 从实例 env 文件中删除环境变量。`--dry-run` 会预览将要删除的 key，并把不存在的 key 作为 no-op 单独列出，不实际写入。`--apply` 会立即执行 `recreate`（不能与 `--dry-run` 同时使用）。 |

## 7. 模型配置收集与复用

| 命令 | 说明 |
|------|------|
| `clawcu provider collect --all` | 从所有 ClawCU 托管实例加本地 `~/.openclaw`、`~/.hermes` 中收集模型配置信息。 |
| `clawcu provider collect --instance <name>` | 从单个托管实例收集模型配置。 |
| `clawcu provider collect --path <home>` | 从任意 OpenClaw 或 Hermes home 目录收集模型配置。 |
| `clawcu provider list` | 列出已收集的模型配置资产，包含服务身份和脱敏后的 API key 摘要。 |
| `clawcu provider show <name>` | 查看某一条收集资产的存储内容，展示时会自动脱敏。若名称在不同服务中重复，可使用 `openclaw:<name>` 或 `hermes:<name>`。 |
| `clawcu provider remove <name>` | 删除一条已收集的模型配置资产。 |
| `clawcu provider models list <name>` | 查看该资产包含的模型列表。 |
| `clawcu provider apply <provider> <instance> [--agent <agent>] [--primary <model>] [--fallbacks <m1,m2>] [--persist]` | 将已收集的模型配置应用到目标实例。`--agent` 默认是 `main`。具体写回行为遵循目标服务的原生配置方式。 |

## 8. 默认行为约定

- 端口默认值：
  - OpenClaw 托管实例从 `18799` 开始
  - Hermes 托管 API 端口从 `8652` 开始
  - Hermes 托管 dashboard 端口从 `9129` 开始
  - 冲突时都按 `+10` 探测
- 资源默认值：
  - `1 CPU + 2GB RAM`
- 数据目录默认值：
  - `~/.clawcu/<instance-name>`
- 容器命名：
  - `clawcu-<service>-<instance-name>`
- 访问摘要：
  - 两类服务都会在 `create`、`list`、`inspect` 中展示访问 URL
  - OpenClaw 展示主服务端口和 dashboard URL
  - Hermes 展示 dashboard 端口和 dashboard URL，就绪判断也可能同时依赖 API server
- env 路径：
  - OpenClaw 使用 `~/.clawcu/instances/<instance>.env`
  - Hermes 使用 `<datadir>/.env`
- 快照行为：
  - `upgrade` 和 `rollback` 会一起快照并恢复实例 home 与匹配的 env 路径
- 推荐升级策略：
  - 先 `clone`，再在克隆实例上 `upgrade`，验证后决定是否 `rollback`

## 9. 说明

- 这份使用说明描述的是 `v0.2.0` 的命令面。
- 发布背景见 [RELEASE_v0.2.0.zh-CN.md](RELEASE_v0.2.0.zh-CN.md)。
- `v0.1.0` 归档命令说明仍保留在 [USAGE_v0.1.0.zh-CN.md](USAGE_v0.1.0.zh-CN.md)。
