# ClawCU v0.1.0

🌐 Language:
[English](RELEASE_v0.1.0.md) | [中文](RELEASE_v0.1.0.zh-CN.md)

发布日期：2026 年 4 月 15 日

> `v0.1.0` 是第一个让 ClawCU 从“轻量辅助脚本”变成“完整本地 OpenClaw 生命周期工具”的版本。这次发布的重点是运维安全：更容易创建实例、更安全地升级、更清晰的回滚行为、可复用的 provider 收集，以及更顺手的日常排障体验。

* * *
## 版本亮点

- 面向本地的 OpenClaw 生命周期管理
  - `clawcu` 现在已经覆盖托管 OpenClaw 实例的核心生命周期：`pull`、`create`、`list`、`inspect`、`start`、`stop`、`restart`、`retry`、`recreate`、`upgrade`、`rollback`、`clone`、`logs`、`remove`。

- 带版本语义的安全升级
  - `upgrade` 会在替换容器前创建升级前安全快照。
  - `rollback` 会恢复实例数据目录以及匹配的实例 env 快照。
  - 升级失败时，会自动尝试恢复旧版本和对应快照。

- Provider 收集与复用
  - Provider 资产现在可以从以下来源收集：
    - 所有托管实例加本地 `~/.openclaw`
    - 单个托管实例
    - 通过 `--path` 指定的任意 OpenClaw 数据目录
  - 收集后的 provider 会被规范化、去重并保存复用。

- 更好的运维体验
  - `create`、`clone`、`upgrade`、`rollback` 不再长时间静默等待，而是有分步进度输出。
  - 生命周期命令会等待实例真正 ready，不会过早报告成功。
  - `list` 和 `inspect` 现在会暴露更多运维状态，包括 provider/model 摘要和 snapshot 上下文。

- 150+ 测试覆盖
  - 当前仓库的测试套件已经较完整覆盖生命周期、回滚、provider 收集、env 处理和 CLI 行为。

* * *
## 核心生命周期能力

### 实例创建与恢复

- `clawcu create openclaw --name <name> --version <version>`
  - `datadir` 默认是 `~/.clawcu/<name>`
  - 宿主机端口默认从 `18789` 开始
  - 端口冲突时按 `+10` 探测
  - 自动写入 Gateway 默认配置
  - 会等待实例真正 ready

- `clawcu retry <name>`
  - 用于重试卡在 `create_failed` 的实例
  - 保留失败历史，方便排障

- `clawcu recreate <name>`
  - 使用已保存的实例配置重建容器
  - 复用相同的数据目录、版本、资源配置和 env 文件

### 克隆

- `clawcu clone <source> --name <name>`
  - 复制源实例的数据目录
  - 如果源实例存在 env 文件，也会一并复制
  - 继承版本、CPU 和内存配置
  - 自动选择端口，并沿用和 `create` 相同的 `+10` 探测逻辑
  - 如果 clone 失败，会回滚半成品状态

* * *
## 安全升级与回滚

### Upgrade 流程

`clawcu upgrade <name> --version <target>`

- 快照当前数据目录
- 快照 `~/.clawcu/instances/<name>.env`
- 准备目标版本镜像
- 使用同一个实例配置替换容器
- 在返回成功前等待 OpenClaw ready
- 在实例历史里记录 snapshot 路径

如果新版本启动失败：

- 失败的新容器会被删除
- 旧快照会被恢复
- 旧版本会被重新拉起
- 失败信息会写入实例历史

### Rollback 流程

`clawcu rollback <name>`

- 从历史记录中解析最近一次可回滚的版本切换
- 如有需要，先准备旧镜像
- 对当前状态额外创建一份 rollback 安全快照
- 恢复旧快照
- 恢复匹配的 env 快照
- 启动旧版本并等待 ready

### Snapshot 可见性

可观测性增强体现在两个地方：

- `clawcu list --managed`
  - 现在包含 `SNAPSHOT` 摘要列，例如：
    - `upgrade -> 2026.4.10`
    - `rollback -> 2026.4.1`

- `clawcu inspect <instance>`
  - 现在包含一个 `snapshots` 块，提供：
    - `latest_upgrade_snapshot`
    - `latest_rollback_snapshot`
    - `latest_restored_snapshot`

* * *
## Provider 工作流

### Collect

`clawcu provider collect` 现在围绕 OpenClaw 现有配置文件工作，而不是重新发明一套平行的 provider 定义格式。

支持的收集方式：

- `clawcu provider collect --all`
  - 扫描所有 ClawCU 托管实例
  - 同时包含本地 `~/.openclaw`

- `clawcu provider collect --instance <instance>`
  - 从单个托管实例收集

- `clawcu provider collect --path <openclaw-home>`
  - 从非托管的 OpenClaw 数据目录收集

### 收集规则

- 根 `openclaw.json` 是 provider 是否算作当前激活状态的主要真源
- provider payload 会被拆开并独立保存
- 去重依据是：
  - provider 名
  - API style
  - endpoint
  - API key
- 如果这些相同，models 会被合并到同一个 provider
- 如果同名 provider 的 API key 或 endpoint 不同，则生成编号变体，例如 `-2`

### Provider Apply

`clawcu provider apply <provider> <instance> --agent <agent>`

- `--agent` 默认是 `main`
- 可以设置：
  - `--primary`
  - `--fallbacks`
  - `--persist`

行为：

- 将 provider 配置写入目标 agent 的运行时文件
- 可以更新该 agent 的 `primary` 和 `fallbacks`
- 使用 `--persist` 时，会把 API key 写入实例 env 文件，并在根配置中写入 env 引用

* * *
## 环境变量管理

环境变量现在已经成为实例运维的一等能力。

### 命令

- `clawcu setenv <instance> KEY=VALUE [KEY=VALUE ...]`
- `clawcu getenv <instance>`
- `clawcu unsetenv <instance> KEY [KEY ...]`

可选的立即应用行为：

- `clawcu setenv ... --apply`
- `clawcu unsetenv ... --apply`

这会更新实例 env 文件，并立刻重建容器，以便 Docker 重新加载 env 文件。

### 存储位置

实例 env 文件保存在：

- `~/.clawcu/instances/<instance>.env`

这样 env 配置就和 OpenClaw home 目录解耦了，但 `recreate`、`upgrade`、`rollback`、`clone` 仍然能保持一致行为。

* * *
## 列表、检查与排障

### List 视图

- `clawcu list`
  - 默认是一个类似 `--all` 的视图，同时包含：
    - 本地 `~/.openclaw`
    - 托管实例

- `clawcu list --managed`
  - 展示托管实例摘要

- `clawcu list --local`
  - 展示本地 `~/.openclaw`

- `clawcu list --agents`
  - 展开为 agent 级别视图

### 实例级可见性

实例摘要现在包含：

- source
- name
- home
- version
- port
- status
- providers
- models
- snapshot summary

### Agent 级可见性

Agent 行现在会显示：

- instance
- agent
- primary
- fallbacks

Agent 名字现在基于真实 agent 目录名推导，而不再回退成像 `defaults` 这样的占位值。

* * *
## 访问、配对与配置

- `clawcu token <instance>`
  - 打印托管实例的 dashboard token

- `clawcu approve <instance> [request-id]`
  - 审批本地 Docker 场景下的浏览器 pairing 请求

- `clawcu config <instance>`
  - 在托管实例容器中运行 `openclaw configure`

- `clawcu exec <instance> <command...>`
  - 在容器中运行任意命令

- `clawcu tui <instance> [--agent <agent>]`
  - 启动 OpenClaw TUI
  - 在进入 TUI 前自动处理常见的本地 approve 流程

这些命令让 ClawCU 管理的生命周期操作可以自然衔接 OpenClaw 自己的配置流程。

* * *
## Setup 与运行时布局

`clawcu setup` 现在已经是一个真正的本地前置检查入口。

它会检查：

- Docker CLI 是否可用
- Docker daemon 是否可用
- ClawCU home 目录是否就绪
- 运行目录布局是否就绪
- OpenClaw 镜像仓库配置
- 在显式请求时显示 shell completion 指引

运行时状态仍然保存在仓库之外：

- `~/.clawcu/instances/`
- `~/.clawcu/providers/`
- `~/.clawcu/logs/`
- `~/.clawcu/snapshots/`

* * *
## 推荐升级策略

对于有风险的 OpenClaw 升级，推荐工作流是：

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test
```

这样你就可以在克隆分支里验证升级结果，而不会碰原始实例。

* * *
## 已知约束

- ClawCU 仍然有意聚焦在本地 Docker 场景下的 OpenClaw 管理。
- `restart` 不会重新加载 env 文件，因为 Docker 只会在容器创建时应用 `--env-file`。
- 如果你希望 env 变更真正进入进程环境，请使用 `recreate` 或 `setenv/unsetenv --apply`。
- Provider 的持久化与运行时应用逻辑目前采取保守策略，以避免意外破坏一个已经工作的 OpenClaw 实例。

* * *
## 结语

`v0.1.0` 的重点，是让 OpenClaw 在单机上作为基础设施运行时更安全：

- 更安全地创建
- 更安全地克隆
- 更安全地复用 provider
- 更安全地升级
- 更安全地回滚
- 在出问题时更清晰地检查状态

而这种运维层面的信任感，才是这个版本真正的目标。
