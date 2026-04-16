# ClawCU v0.2.0

🌐 Language:
[English](RELEASE_v0.2.0.md) | [中文](RELEASE_v0.2.0.zh-CN.md)

发布日期：2026 年 4 月 15 日

> `v0.2.0` 是第一个把 ClawCU 从“只服务 OpenClaw 的工具”升级成“多 agent runtime 本地运维层”的版本。这次发布的重点是架构清晰：共享生命周期核心、明确的服务 adapter，以及 Hermes 作为第二个一等公民 runtime 的正式接入。

* * *
## 亮点

- 多 Agent 生命周期核心
  - ClawCU 现在拥有一套共享生命周期核心，负责 Docker 编排、实例记录、快照、env 处理、日志和 CLI 分发。
  - 服务专属行为被隔离在各自 adapter 内，不再混在一个高度 OpenClaw 偏置的 service 层里。

- 两个一等公民服务
  - `openclaw`
  - `hermes`
  - `clawcu create`、`pull`、`list`、`inspect`、`clone`、`upgrade`、`rollback`、`exec`、`config`、`tui` 现在都能按服务分发。

- Hermes 正式接入
  - ClawCU 现在可以从配置好的 Hermes 镜像仓库拉取预构建 Hermes Docker 镜像。
  - Hermes 实例拥有独立的访问 URL、`.env`、快照、clone、upgrade 和 rollback 流程。

- 服务感知的模型配置复用
  - 现有 `provider` 命令族在内部已演进成跨服务的模型配置收集与复用层。
  - 收集范围现在覆盖托管实例以及本地 `~/.openclaw`、`~/.hermes`。
  - 配置资产会记录服务身份，避免 OpenClaw 与 Hermes 的同名配置静默冲突。

- 更好的访问可见性
  - `create`、`list`、`inspect` 现在都会展示两类服务的访问摘要。
  - OpenClaw 继续保留 dashboard token 与 pairing 流程。
  - Hermes 被视作具备 Web 访问面的托管实例，但不会被硬塞进和 OpenClaw 一样的认证模型。

- 170+ 测试
  - 当前测试已覆盖共享生命周期核心、OpenClaw 回归，以及 Hermes 的生命周期与模型配置行为。

* * *
## 架构变化

### 共享核心

ClawCU 现在拆成三层：

- `src/clawcu/core/`
  - 共享 models
  - 共享 storage
  - 共享 paths
  - Docker wrapper
  - subprocess helpers
  - 生命周期编排
  - snapshot helpers
  - adapter contract

- `src/clawcu/openclaw/`
  - OpenClaw 专属镜像管理
  - 就绪判断
  - dashboard/token/pairing/TUI 集成
  - OpenClaw 模型配置收集与 apply

- `src/clawcu/hermes/`
  - Hermes 镜像拉取与管理
  - Hermes home/config/env 处理
  - 就绪判断与访问摘要
  - Hermes config/chat 集成
  - Hermes 模型配置收集与 apply

这次重构最核心的设计原则是：

- 生命周期统一
- 服务内部结构保持原生

### 访问摘要

共享核心不再假设所有服务都像 OpenClaw 一样工作。

现在由各 adapter 提供：

- access URL
- readiness strategy
- auth hint
- 生命周期摘要

这样 ClawCU 可以回答：

- 用户应该访问哪里？
- 怎么判断实例真的 ready 了？
- 应该提示什么认证方式？

而不需要把 OpenClaw 特有的语义硬编码进共享层。

* * *
## v0.2.0 里的 Hermes

Hermes 现在已经是 ClawCU 中的正式托管服务。

### 制品准备

`clawcu pull hermes --version <tag>`

会完成以下动作：

- 从配置好的 Hermes 镜像仓库拉取指定 tag

### 实例模型

每个 Hermes 实例都是：

- 一个独立受管 home
- 挂载自选择的 `datadir`
- 拥有单独的宿主机访问端口
- 使用 Hermes 原生 env 文件：
  - `<datadir>/.env`

### 生命周期能力

Hermes 现在参与和 OpenClaw 一样的运维流程：

- `create`
- `clone`
- `upgrade`
- `rollback`
- `recreate`
- `logs`
- `exec`
- `config`
- `tui`

从用户视角看，Hermes 也具备了同样的生命周期安全性：

- 隔离实例 home
- 可复现实例版本
- 基于快照的升级
- 可回滚恢复
- 明确可见的访问 URL

* * *
## OpenClaw 兼容性

OpenClaw 在 `v0.2.0` 中继续保持完整支持，并延续了 `v0.1.0` 已经稳定下来的工作流。

几个关键兼容策略：

- OpenClaw 仍然只使用官方镜像
- token 与浏览器 pairing approval 仍然是 OpenClaw 专属能力
- OpenClaw env 文件仍位于：
  - `~/.clawcu/instances/<instance>.env`

也就是说，`v0.2.0` 的目标是扩展 ClawCU，而不是打散已经稳定的 OpenClaw 体验。

* * *
## 模型配置收集与复用

旧的 `provider` 命令仍然保留，但其内部已经变成更通用的跨服务模型配置层。

当前支持的收集来源包括：

- 所有 ClawCU 托管实例
- 单个托管实例
- 本地 `~/.openclaw`
- 本地 `~/.hermes`
- 显式 `--path <home>`

`v0.2.0` 的关键变化：

- 资产会带上服务身份
- 不同服务的同名模型配置不会静默冲突
- apply 会根据目标实例的服务 adapter 分发

这让命令面保持连续，同时又真正适配了多 agent runtime 的世界。

* * *
## 环境变量与快照

在 `v0.2.0` 中，ClawCU 有意不强行统一所有服务的 env 落位。

当前规则是：

- OpenClaw 使用：
  - `~/.clawcu/instances/<instance>.env`
- Hermes 使用：
  - `<datadir>/.env`

共享生命周期层已经能感知 adapter，因此 clone / upgrade / rollback 都会快照并恢复各自正确的 env 路径。

这意味着：

- OpenClaw 继续保留已稳定的 sidecar env 模型
- Hermes 可以顺着自己的原生 home 布局工作
- 快照安全边界仍然一致

* * *
## 服务专属访问行为

并不是所有用户命令都必须在两类服务上强行等价。

在 `v0.2.0` 里：

- `clawcu token <instance>`
  - OpenClaw 支持
  - Hermes 不支持

- `clawcu approve <instance>`
  - OpenClaw 支持
  - Hermes 不支持

这是有意设计的。ClawCU 现在统一的是生命周期，而不是伪装成 OpenClaw 和 Hermes 拥有同一套 dashboard 认证模型。

* * *
## 推荐工作流

最安全的工作模式仍然是：

1. 先复制一个工作中的实例
2. 在副本上升级
3. 验证副本
4. 如果不满意就回滚
5. 最后再决定是否改动主实例

这套模式现在适用于两类服务：

- OpenClaw
- Hermes

所以 `v0.2.0` 的意义，不只是“多支持了一个 runtime”，而是让 ClawCU 真正开始成为一个可复用的本地多 agent 运维层。

* * *
## 结语

`v0.2.0` 是 ClawCU 从“OpenClaw 辅助工具”走向“本地多 agent 运维平台”的起点：

- 一台机器
- 多个受管 agent runtime
- 明确的生命周期控制
- 更安全的升级
- 原生而清晰的服务边界
- 更适合继续往下演进的架构基础
