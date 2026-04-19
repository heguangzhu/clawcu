# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

`ClawCU` 是一个面向本地多 AI Agent Runtime 的生命周期管理工具，适合在同一台机器上稳定运行多个实例。目前支持：[OpenClaw](https://github.com/openclaw/openclaw) 和 [Hermes Agent](https://github.com/NousResearch/hermes-agent)。

## 为什么需要 ClawCU

手工运行 agent runtime 会以可预料的方式出问题。ClawCU 给每个运行时：

- **隔离** —— 独占的容器、数据目录与环境变量
- **可回滚** —— 升级前自动快照，一个命令回到上一版
- **可复刻** —— 先 clone 再验证，验证通过才升级主实例

## 核心亮点

- **一套 CLI，两个运行时** — OpenClaw 和 Hermes 共用同一套生命周期命令
- **每次升级前自动快照** — datadir 与环境变量同步捕获；`rollback` 从真实备份恢复
- **先克隆再实验** — 复制一份实例，在副本上升级，主实例原地不动

## 安装

需要 Python 3.11+ 和一个正在运行的 Docker daemon。

```bash
pip install clawcu
```

或者用隔离环境安装 CLI：

```bash
pipx install clawcu
# 或
uv tool install clawcu
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
clawcu list --removed     # 记录已丢失的孤儿 datadir
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

完整命令的使用可参考 [USAGE_v0.2.8.zh-CN.md](release/USAGE_v0.2.8.zh-CN.md)。

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
- 该服务对应的环境变量文件

也就是：

- OpenClaw：
  - `datadir`
  - `~/.clawcu/instances/<instance>.env`
- Hermes：
  - `datadir`
  - `<datadir>/.env`

如果升级失败，ClawCU 会尝试自动恢复旧版本和对应的环境变量快照。

## 模型配置收集与复用

为了兼容已有命令面，ClawCU 仍然保留 `provider` 这组命令；但现在它的最佳读法是跨服务的模型配置收集与复用。

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

ClawCU 会按服务原生模型管理环境变量，而不是强制所有服务共用一套落位。

OpenClaw：

- 环境变量路径：
  - `~/.clawcu/instances/<instance>.env`

Hermes：

- 环境变量路径：
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

`--apply` 会执行 `recreate`，让 Docker 重新加载该实例对应的环境变量文件。

## 访问方式与服务差异

ClawCU 尽量保持命令统一，但仍有少数能力是服务专属的。

OpenClaw 专属：

- `clawcu token <instance>`
- `clawcu approve <instance> [requestId]`

因为 OpenClaw 有对应的 dashboard token 和 pairing 模型。

Hermes 的主入口：

- Dashboard URL
- `tui`
- `config`
- `exec`

Hermes 不会被强行塞进和 OpenClaw 一样的 `token` / `approve` 语义里。

## 发布说明

- `v0.2.8` 发布说明：[RELEASE_v0.2.8.zh-CN.md](release/RELEASE_v0.2.8.zh-CN.md)
- `v0.2.0` 归档发布说明：[RELEASE_v0.2.0.zh-CN.md](release/RELEASE_v0.2.0.zh-CN.md)
- `v0.1.0` 归档发布说明：[RELEASE_v0.1.0.zh-CN.md](release/RELEASE_v0.1.0.zh-CN.md)
