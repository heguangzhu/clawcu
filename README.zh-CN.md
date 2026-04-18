# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

`ClawCU` 是一个面向本地多 AI Agent Runtime 的生命周期管理工具，适合在同一台机器上稳定运行多个实例。

在 `v0.2.0` 中，ClawCU 从只管理 OpenClaw 的工具，升级成同时支持两个一等公民服务的多 agent 管理器：

- `openclaw`
- `hermes`

如果说 `OpenClaw` 和 `Hermes` 是运行时，`ClawCU` 就是包在它们外面的运维层。

## 为什么需要 ClawCU

手工运行 agent runtime 虽然灵活，但很容易逐渐变脆：

- 升级一个新版本，原本能跑的实例就可能失效
- 想做实验时，很难不影响在线实例
- 没有清晰快照边界时，回滚会非常痛苦
- 当同一台机器上开始出现多个实例、甚至多种 runtime 时，配置与状态会越来越难管理

ClawCU 的目标，就是用一套 Docker 工作流把这些问题稳稳兜住：

- 明确的版本管理
- 隔离的本地实例
- 先克隆再实验
- 安全的升级与回滚路径
- 对服务、访问地址、模型配置、env 和快照的实用可观测性

## 核心亮点

- 一套生命周期工具，同时管理两类服务：
  - `openclaw`
  - `hermes`
- 尽可能统一的命令面：
  - `pull`、`create`、`list`、`inspect`、`start`、`stop`、`restart`、`retry`、`recreate`、`upgrade`、`rollback`、`clone`、`logs`、`remove`、`exec`、`config`、`tui`
- 安全升级：
  - 升级前自动创建快照，快照同时覆盖数据目录和该服务对应的 env 文件
- 先克隆再实验：
  - 先复制一个工作中的实例，再验证新版本
- 服务感知的模型配置收集与复用：
  - 可从托管实例或本地 `~/.openclaw`、`~/.hermes` 收集模型配置信息
- 更顺手的日常运维体验：
  - 就绪等待
  - 分步进度输出
  - `create`、`list`、`inspect` 直接展示访问地址
  - `list`、`inspect` 展示快照摘要

## 当前支持的服务

### OpenClaw

- 制品来源：
  - 官方镜像仓库
- 访问方式：
  - Dashboard URL
  - token
  - 浏览器 pairing 审批
- 通过 ClawCU 暴露的原生命令入口：
  - `config`
  - `tui`
  - `token`
  - `approve`
- env 位置：
  - `~/.clawcu/instances/<instance>.env`

### Hermes

- 制品来源：
  - 从 `clawcu/hermes-agent:<tag>` 拉取预构建 Hermes 镜像
- 访问方式：
  - 本地 Web Dashboard URL
  - 通过 `tui`、`config`、`exec` 进入 CLI / chat
- 通过 ClawCU 暴露的原生命令入口：
  - `config`
  - `tui`
- env 位置：
  - `<datadir>/.env`

在 `v0.2.0` 中，ClawCU 有意不强行统一 OpenClaw 和 Hermes 的认证模型与 env 落位。生命周期统一，服务内部结构保持原生。

## 安装

```bash
uv tool install .
```

或者：

```bash
pipx install .
```

## 快速开始

先检查 Docker 访问、运行目录，并配置默认值：

```bash
clawcu setup
```

只有在你需要时，再查看 shell completion 指引：

```bash
clawcu setup --completion
```

在交互式终端里，`clawcu setup` 会提示配置：

- 默认 `ClawCU home`
- 默认 OpenClaw 镜像源
- 默认 Hermes 镜像仓库

### OpenClaw

为 OpenClaw 准备要使用的版本：

```bash
clawcu pull openclaw --version 2026.4.1
```

这一步会记录 ClawCU 应使用的 OpenClaw 镜像；如果本地还没有该镜像，Docker 会在实例启动时自动拉取。

创建并启动实例：

```bash
clawcu create openclaw --name writer --version 2026.4.1
```

验证实例是否真的可用：

```bash
clawcu tui writer
```

获取 Dashboard token：

```bash
clawcu token writer
```

### Hermes

拉取预构建 Hermes 镜像：

```bash
clawcu pull hermes --version v2026.4.13
```

创建并启动 Hermes 实例：

```bash
clawcu create hermes --name analyst --version v2026.4.13
```

进入 Hermes 交互式入口：

```bash
clawcu tui analyst
```

### 通用日常命令

查看当前实例：

```bash
clawcu list
clawcu list --managed
clawcu list --agents
```

查看实例详情：

```bash
clawcu inspect writer
clawcu inspect analyst
```

在容器里执行原生命令：

```bash
clawcu exec writer pwd
clawcu exec analyst hermes version
```

完整命令的使用可参考 [USAGE_v0.2.0.zh-CN.md](USAGE_v0.2.0.zh-CN.md)。

## 安全升级流程

ClawCU 推荐的升级方式是：

1. 先复制一个工作中的实例
2. 在复制出来的实例上升级
3. 验证新实例
4. 如果不满意就回滚
5. 最后再决定是否升级原实例

示例：

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test
```

Hermes 也适用同样模式：

```bash
clawcu clone analyst --name analyst-upgrade-test
clawcu upgrade analyst-upgrade-test --version v0.9.1
clawcu rollback analyst-upgrade-test
```

这套流程的好处是：

- 主实例不受影响
- 兼容性问题被隔离到实验实例里
- 修复动作可以在副本里慢慢处理
- 回滚始终有真实快照可恢复

### upgrade 保护了什么

在 `upgrade` 前，ClawCU 会自动快照：

- 实例数据目录
- 该服务对应的 env 文件

也就是：

- OpenClaw：
  - `datadir`
  - `~/.clawcu/instances/<instance>.env`
- Hermes：
  - `datadir`
  - `<datadir>/.env`

如果升级失败，ClawCU 会尝试自动恢复旧版本和对应 env 快照。

## 模型配置收集与复用

为了兼容已有命令面，ClawCU 仍然保留 `provider` 这组命令；但在 `v0.2.0` 里，可以把它理解成跨服务的模型配置收集与复用。

从所有托管实例加本地 home 收集：

```bash
clawcu provider collect --all
```

从某一个托管实例收集：

```bash
clawcu provider collect --instance writer
clawcu provider collect --instance analyst
```

从本地 home 收集：

```bash
clawcu provider collect --path ~/.openclaw
clawcu provider collect --path ~/.hermes
```

查看已收集的配置：

```bash
clawcu provider list
clawcu provider show openclaw:minimax
clawcu provider show hermes:openrouter
```

应用已收集的模型配置：

```bash
clawcu provider apply openclaw:minimax writer --agent main --primary minimax/MiniMax-M2.7
clawcu provider apply hermes:openrouter analyst --persist
```

ClawCU 会把服务身份一起存入配置资产，因此 OpenClaw 和 Hermes 中同名的模型配置不会默默冲突。

## 环境变量

ClawCU 会按服务原生模型管理 env，而不是强制所有服务共用一套落位。

OpenClaw：

- env 路径：
  - `~/.clawcu/instances/<instance>.env`

Hermes：

- env 路径：
  - `<datadir>/.env`

通用命令：

```bash
clawcu setenv <instance> KEY=VALUE
clawcu getenv <instance>
clawcu unsetenv <instance> KEY
```

如果希望立即生效：

```bash
clawcu setenv <instance> KEY=VALUE --apply
clawcu unsetenv <instance> KEY --apply
```

`--apply` 会执行 `recreate`，让 Docker 重新加载该实例对应的 env 文件。

## 访问方式与服务差异

ClawCU 尽量保持命令统一，但仍有少数能力是服务专属的。

OpenClaw 在 `v0.2.0` 里仍保留：

- `clawcu token <instance>`
- `clawcu approve <instance> [requestId]`

因为 OpenClaw 当前就有对应的 dashboard token 和 pairing 模型。

Hermes 在 `v0.2.0` 里的主入口则是：

- Dashboard URL
- `tui`
- `config`
- `exec`

Hermes 目前不会被强行塞进和 OpenClaw 一样的 `token` / `approve` 语义里。

## 发布说明

- `v0.2.0` 发布说明：[RELEASE_v0.2.0.zh-CN.md](RELEASE_v0.2.0.zh-CN.md)
- `v0.1.0` 归档发布说明：[RELEASE_v0.1.0.zh-CN.md](RELEASE_v0.1.0.zh-CN.md)
