# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

`ClawCU` 是一个面向 `OpenClaw` 的本地优先生命周期管理器。

它提供了一套更稳妥的本机运维方式，让你可以在同一台机器上运行多个 OpenClaw 实例、管理版本、克隆可用实例做实验、从已有实例中收集配置，并在升级出问题时干净回退。

> 如果说 `OpenClaw` 是 agent 的运行时，那么 `ClawCU` 就是围绕它的运维安全层。

## 为什么需要 ClawCU

手工运行 OpenClaw 很灵活，但也很容易慢慢演变成一个脆弱环境：

- 升级可能会直接破坏原本可用的实例
- 和生产实例共用环境时，实验风险很高
- 如果事先没有干净快照，回滚会很困难

`ClawCU` 就是为了解决这些问题而设计的，它基于 Docker，强调：

- 显式版本管理
- 可复现的本地实例
- 安全的升级与回滚路径
- 先 clone 再实验
- 对 provider、model、agent、snapshot 和 env 的实用可观测性

## 核心亮点

- 完整的 OpenClaw 本地实例生命周期管理：
  - `pull`、`create`、`list`、`inspect`、`token`、`start`、`stop`、`restart`、`retry`、`recreate`、`upgrade`、`rollback`、`clone`、`logs`、`remove`
- 带自动快照保护的安全升级：
  - 快照同时覆盖实例数据目录和 `~/.clawcu/instances/<instance>.env`
- 先 clone 再实验：
  - 先复制一个工作中的实例，再验证新版本
- 模型配置收集与复用：
  - 可从托管实例或本地 `~/.openclaw` 收集模型配置信息
- 环境变量管理：
  - 提供 `setenv`、`getenv`、`unsetenv`，并支持 `--apply`
- 更适合日常运维：
  - readiness 等待
  - 过程进度输出
  - dashboard token 查询
  - `list` 和 `inspect` 中的快照摘要

## ClawCU 管什么

ClawCU 有意只聚焦在 OpenClaw 的本地运维层。

它负责：

- 从官方 OpenClaw 镜像仓库准备 Docker 镜像
- OpenClaw 实例创建与生命周期管理
- 本地访问所需的 gateway 启动默认配置
- 带回滚保护的版本切换
- 从已有 OpenClaw home 收集 provider 配置
- env 文件与实例元数据管理

它并不试图替代 OpenClaw 本身。

对于模型配置、插件配置、agent 级 OpenClaw 流程，ClawCU 仍然配合原生命令使用：

- `clawcu config <instance>`
- `clawcu exec <instance> <command...>`

## 安装

```bash
uv tool install .
```

或者：

```bash
pipx install .
```

## 快速开始

检查本地前置条件：

```bash
clawcu setup
```

仅在需要时显示 shell completion 指引：

```bash
clawcu setup --completion
```

在交互式终端中，`clawcu setup` 还会提示配置：

- 默认的 `ClawCU home`
- 默认的 OpenClaw 镜像仓库

默认 OpenClaw 镜像仓库是：

```bash
ghcr.io/openclaw/openclaw
```

如果 `clawcu setup` 检测到你的公网 IP 位于中国，且你还没有配置过镜像仓库，它会默认建议使用这个镜像源：

```bash
ghcr.nju.edu.cn/openclaw/openclaw
```

选择的镜像仓库会保存到：

```bash
~/.clawcu/config.json
```

选择的默认 `ClawCU home` 会保存到：

```bash
~/.config/clawcu/bootstrap.json
```

如果你在 shell 中显式导出了 `CLAWCU_HOME`，那么当前进程仍然以这个环境变量为最高优先级。

从 GHCR 拉取一个 OpenClaw 版本：

```bash
clawcu pull openclaw --version 2026.4.1
```

创建并启动一个实例：

```bash
clawcu create openclaw --name writer --version 2026.4.1
```

创建完成后，可以用下面这条命令检查实例是否真的可用：

```bash
clawcu tui writer
```

这是一条很实用的端到端检查命令，它能帮助确认：

- 实例已经正常运行
- pairing 流程可以完成
- gateway 可以连通
- 目标 agent 可以成功进入 OpenClaw TUI

获取 dashboard token：

```bash
clawcu token writer
```

查看当前运行状态：

```bash
clawcu list
clawcu list --managed
clawcu list --agents
```

查看某个托管实例的详细状态：

```bash
clawcu inspect writer
```

在 ClawCU 下，OpenClaw 运行在 `token` 认证模式下。这是当前 `lan` 绑定模型所要求的。

完整命令的使用可参考 [USAGE_v0.1.0.zh-CN.md](USAGE_v0.1.0.zh-CN.md)。

## 安全升级流程

推荐的 ClawCU 升级流程是：

1. 先 clone 一个可用实例
2. 在克隆实例上执行 upgrade
3. 验证克隆实例
4. 如果需要再 rollback
5. 最后再决定是否升级原实例

示例：

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test
```

这套方式的好处：

- 主实例保持不动
- 版本兼容性问题被隔离
- provider 或 model 修复可以先在克隆实例里完成
- 回滚有真实快照可恢复

### Upgrade 保护了什么

在执行 `upgrade` 前，ClawCU 会快照：

- 实例数据目录
- `~/.clawcu/instances/<instance>.env`

如果升级失败，ClawCU 会尝试自动恢复旧版本以及匹配的 env 快照。

### Rollback 恢复了什么

`rollback` 会恢复：

- 之前的数据目录快照
- 对应的 env 快照
- 之前的镜像版本

这意味着基于 env 的 provider 凭据，会和 OpenClaw home 一起回到同一个回滚边界。

## 用 Clone 做实验

`clone` 的设计目标就是安全实验：

```bash
clawcu clone writer --name writer-exp
```

克隆实例会继承：

- 源实例数据目录
- 源实例 env 文件（如果存在）
- 版本
- CPU
- 内存

ClawCU 会：

- 自动选择新的宿主机端口
- 端口冲突时按 `+10` 继续探测
- 如果 clone 失败，自动回滚半成品状态

## Provider 收集与复用

ClawCU 不会重新发明一套独立的 provider 配置格式，而是从已经工作的 OpenClaw home 中收集 provider 资产。

### 收集 Provider

从所有托管实例以及本地 `~/.openclaw` 收集：

```bash
clawcu provider collect --all
```

从单个托管实例收集：

```bash
clawcu provider collect --instance writer
```

从任意 OpenClaw home 收集：

```bash
clawcu provider collect --path ~/.openclaw
```

查看已收集的 provider：

```bash
clawcu provider list
clawcu provider show openrouter
clawcu provider models list openrouter
```

### 收集规则

ClawCU 以根 `openclaw.json` 作为“哪些 provider 当前有效”的事实来源。

收集后的 provider 会根据以下字段去重：

- provider 名称
- API style
- endpoint
- API key

如果这些字段相同，就会把 models 合并到同一个 provider 中。

如果 provider 名相同，但 endpoint 或 API key 不同，ClawCU 会保留编号变体，例如 `-2`。

### 把 Provider 应用到实例

把已收集的 provider 应用到某个 agent：

```bash
clawcu provider apply kimi-coding writer
clawcu provider apply kimi-coding writer --agent chat
clawcu provider apply kimi-coding writer --agent chat --primary kimi-coding/k2p5
clawcu provider apply kimi-coding writer --agent chat --fallbacks anthropic/claude-sonnet-4.5,openai/gpt-4.1
clawcu provider apply kimi-coding writer --agent chat --primary kimi-coding/k2p5 --persist
```

行为说明：

- `--agent` 默认是 `main`
- 将 provider 运行时配置添加到目标 agent
- 可以设置 `primary`
- 可以设置 `fallbacks`
- 使用 `--persist` 时，会把根配置改成 env 引用，并把真实 key 写入实例 env 文件

## 环境变量管理

ClawCU 把实例 env 文件当作一等运维状态来管理。

设置一个或多个 env 变量：

```bash
clawcu setenv writer OPENAI_API_KEY=sk-xxx
clawcu setenv writer OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://api.example.com/v1
```

查看 env 变量：

```bash
clawcu getenv writer
```

删除 env 变量：

```bash
clawcu unsetenv writer OPENAI_API_KEY
```

通过重建容器立即生效：

```bash
clawcu setenv writer OPENAI_API_KEY=sk-xxx --apply
clawcu unsetenv writer OPENAI_API_KEY --apply
```

重要说明：

- `restart` 不会重新加载 Docker env 文件
- `recreate` 会

这也是为什么 env 变更要在 `recreate` 后才会完全生效，而不是普通 `restart`。

## 访问、配对与原生 OpenClaw 流程

在托管实例里运行原生 OpenClaw 配置流程：

```bash
clawcu config writer
clawcu config writer -- --help
```

在容器中执行任意命令：

```bash
clawcu exec writer openclaw config
clawcu exec writer pwd
clawcu exec writer ls
```

如果浏览器显示 `pairing required`，可以审批最新的 pending request：

```bash
clawcu approve writer
```

启动 OpenClaw TUI，并自动处理常见的 approve 步骤：

```bash
clawcu tui writer
clawcu tui writer --agent chat
```

## 列表与详情检查

ClawCU 提供两个很实用的可观测层级：

### 实例视图

```bash
clawcu list
clawcu list --managed
clawcu list --local
```

默认 `list` 同时包含：

- ClawCU 托管实例
- 本地 `~/.openclaw`

托管实例摘要包括：

- source
- name
- home
- version
- port
- status
- providers
- models
- snapshot 摘要

### Agent 视图

```bash
clawcu list --agents
clawcu list --managed --agents
```

Agent 行会显示：

- instance
- agent
- primary
- fallbacks

### 详细 Inspect

```bash
clawcu inspect writer
```

`inspect` 包含：

- 实例元数据
- Docker 状态
- 最近历史
- 快照摘要块：
  - `latest_upgrade_snapshot`
  - `latest_rollback_snapshot`
  - `latest_restored_snapshot`

## 日志与恢复

查看日志：

```bash
clawcu logs writer --follow
```

恢复首次创建失败的实例：

```bash
clawcu retry writer
```

根据已保存配置重建实例容器：

```bash
clawcu recreate writer
```

停止、启动、重启：

```bash
clawcu stop writer
clawcu start writer
clawcu restart writer
```

删除实例：

```bash
clawcu remove writer --keep-data
clawcu remove writer --delete-data
```

## 运行时目录结构

ClawCU 会把运维状态存放在仓库之外：

- `~/.clawcu/instances/`
  - 实例元数据
  - 实例 env 文件
- `~/.clawcu/providers/`
  - 收集到的 provider 资产
- `~/.clawcu/logs/`
  - 生命周期日志
- `~/.clawcu/snapshots/`
  - upgrade 和 rollback 快照

你可以这样覆盖默认 home：

```bash
CLAWCU_HOME=/custom/path
```

## 当前范围

`v0.1.0` 有意保持聚焦。

已包含：

- 单机 OpenClaw 生命周期管理
- 带版本的 Docker 运维
- 快照与回滚
- 基于 clone 的实验流
- provider 收集与复用
- env 管理

暂不包含：

- ClawCU 自己的 Web 控制台
- 云端编排
- 多主机集群管理
- 非 OpenClaw 服务，例如 Hermes
- agent 内部的业务工作流编排

## 发布说明

- 发布说明: [RELEASE_v0.1.0.md](/Users/michael/workspaces/codex/hello/openclaw-docker/RELEASE_v0.1.0.md)

## 许可证

MIT。详见 [LICENSE](/Users/michael/workspaces/codex/hello/openclaw-docker/LICENSE)。
