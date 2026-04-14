# ClawCU Product Plan v0.0.1

## 1. 产品概述

`ClawCU` 是一个面向 AI 发烧友、自托管极客和独立开发者的本地 OpenClaw 运维产品。它的第一形态不是 Web 平台，而是一个可安装的命令行工具 `clawcu`，帮助用户在自己的电脑上稳定运行、升级、回滚和复制 OpenClaw 实例。

`v0.0.1` 的目标非常明确：先把本地单机跑稳，再谈更复杂的平台化。

一句话定位：

> 你的私有 Compute Unit，用来稳定养数字员工。

**当前状态：** v0.0.1 核心功能已完成，核心命令已实现，通过真实 smoke test 验证了 `pull -> create -> approve -> config` 的完整流程。

## 2. 品牌定义

- 品牌名：`ClawCU`
- 品牌结构：`Claw + Compute Unit`
- `Claw`：代表龙虾，也代表 OpenClaw 的核心意象
- `CU`：代表最小计算单元，强调资源、实例和工作负载都可以被清晰管理

品牌心智：

- 公有云把算力卖成 CU
- `ClawCU` 把 CU 带回本地自托管
- 让一台 Mac mini 也能像私有 AI 基础设施一样持续工作

## 3. 核心痛点

### 3.1 上手困难，配置复杂

- OpenClaw 的本地部署、配置和修复依赖较强的手工能力
- 配置出错后，往往需要靠临时热修和反复重启排查
- 用户很难建立稳定、可复用的运行流程

### 3.2 升级容易挂

- 升级新版本后，配置和插件兼容性容易出问题
- 服务一旦挂掉，就需要手动排障，恢复路径不清晰
- 缺少标准化的版本切换和无损回退机制

### 3.3 缺少安全实验场

- 想试新版本、新插件时，很难在不影响主实例的情况下做实验
- 没有一条简单的“复制一份数据然后单独验证”的路径

### 3.4 业务价值不透明

- 本地跑着 AI 系统，但不容易持续感知它的稳定性和使用价值
- 如果底层运维不稳，用户很难把它真正当成数字员工基础设施

## 4. 解决方案

`ClawCU` 用一个统一的命令行工具来覆盖 OpenClaw 的本地运维全链路：

- 用 `clawcu` 统一实例的拉取、构建、创建、启动、停止、升级、回滚和删除
- 用 Docker 固化运行环境，避免”同样的配置在不同时间跑不起来”
- 用版本化镜像和宿主机数据目录分离，支持升级和无损回退
- 用数据目录复制实现实验分支，让试验新版本和插件更安全
- 用本地元数据记录每个实例的版本、资源、端口、状态和历史
- 用 Gateway 自动配置（bind=lan, auth=token, allowedOrigins=*）解决本地访问问题
- 用健康检查等待循环确保实例真正 ready 后再报告成功
- 用 `approve` 命令解决 Docker 环境下浏览器配对问题
- 用 `config` 和 `exec` 穿透调用 OpenClaw 原生配置流程与调试命令，降低学习成本

## 5. 面向人群

### 首批用户

- AI 发烧友
- 自托管极客
- 独立开发者
- 希望把个人电脑变成 AI 工作底座的人

### 次级用户

- 一人公司经营者
- 需要 7x24 小时数字员工基础能力的人

### 当前不优先服务的人群

- 企业级多租户团队
- 纯云端运维团队
- 需要复杂控制面板和权限系统的组织用户

## 6. MVP 范围

`v0.0.1` 只做一件事：把本地 OpenClaw 的 Docker 生命周期管理标准化。

### 包含能力

- Python 命令行工具 `clawcu`，通过 `uv tool install .` 或 `pipx install .` 全局安装
- 优先拉取官方镜像（`ghcr.io/openclaw/openclaw:<version>`），失败时 fallback 到 git clone + docker build
- 创建并启动 OpenClaw 实例，自动配置 Gateway（bind=lan, auth=token, allowedOrigins=*）
- 健康检查等待循环：创建后持续轮询 `/healthz`，直到实例真正 ready 或失败
- 端口自动探测：默认 18789，冲突时自动 +10 重试
- 失败实例追踪：`create_failed` 状态保留，支持 `retry` 重试
- 查看实例列表和详细状态（含 Docker 容器实时状态）
- 升级实例到新版本（自动快照 + 失败自动回滚）
- 自动创建快照并支持回滚
- 复制数据目录以创建实验实例
- 删除实例并选择是否保留数据目录
- 浏览器配对审批（解决 Docker 环境下 IP 不匹配导致的 pairing required）
- 穿透调用 OpenClaw 原生 `configure` 与其他实例内命令（通过 `config` / `exec`）
- 重建已有实例（`recreate`），用于修复 Gateway 配置等问题
- 分步进度输出，关键操作有 Step N/M 提示
- Dashboard URL 自动输出（含 token），创建成功即可直接访问
- 实例历史事件记录（create、upgrade、rollback、retry 等全部留痕）

### 明确不包含

- Web UI
- 云端多机部署
- 多用户权限系统
- 客户端类型参数，例如 `web` / `feishu`
- 实例内部 agent 的业务编排
- 多种认证模式（v0.0.1 仅支持 token，因为 OpenClaw 要求 lan binding 必须开启认证）
- 其他服务类型（如 Hermes），仅支持 OpenClaw
- 统一的模型/API Key 跨实例配置管理（各实例独立配置）

## 7. 命令行产品形态

`ClawCU` 的产品原则是：

> 每个运维动作，都应该有一个对应的 `clawcu` 命令。

### 7.1 版本与镜像

| 命令 | 说明 |
|------|------|
| `clawcu --version` | 查看已安装的 ClawCU 版本 |
| `clawcu pull openclaw --version <version>` | 拉取或构建指定版本的 OpenClaw 镜像（优先 GHCR 官方镜像，失败后 fallback 到 git clone + docker build） |

### 7.2 实例生命周期

| 命令 | 说明 |
|------|------|
| `clawcu create openclaw --name <name> --version <version> [--port <port>] [--cpu 1] [--memory 2g]` | 创建并启动 OpenClaw 实例，`--datadir` 默认 `~/.clawcu/<name>`，`--port` 默认 18789（冲突时自动 +10 探测）。创建过程中自动配置 Gateway、等待健康检查通过、输出 Dashboard URL |
| `clawcu list [--running]` | 列出所有实例，`--running` 仅显示运行中的实例 |
| `clawcu inspect <name>` | 查看实例详细状态和 Docker 容器信息（JSON 格式） |
| `clawcu start <name>` | 启动已停止的实例 |
| `clawcu stop <name>` | 停止运行中的实例 |
| `clawcu restart <name>` | 重启实例 |
| `clawcu remove <name> [--keep-data\|--delete-data]` | 删除实例和容器，选择是否保留数据目录 |

### 7.3 故障恢复

| 命令 | 说明 |
|------|------|
| `clawcu retry <name>` | 重试创建失败的实例（仅限 `create_failed` 状态） |
| `clawcu recreate <name>` | 重建已有实例的 Docker 容器（保持原有配置），用于修复 Gateway 配置等问题 |

### 7.4 版本管理

| 命令 | 说明 |
|------|------|
| `clawcu upgrade <name> --version <version>` | 升级实例到新版本（自动创建数据快照，失败时自动回滚） |
| `clawcu rollback <name>` | 回滚到上一个版本（基于快照恢复） |

### 7.5 实验与克隆

| 命令 | 说明 |
|------|------|
| `clawcu clone <source> --name <name> --datadir <path> --port <port>` | 复制源实例数据目录创建独立实验实例 |

### 7.6 访问与配置

| 命令 | 说明 |
|------|------|
| `clawcu approve <name> [requestId]` | 审批浏览器配对请求（解决 Docker 环境下 IP 不匹配导致的 pairing required）。不指定 requestId 时自动审批最新的待处理请求 |
| `clawcu config <name> [-- args...]` | 穿透调用容器内的 `openclaw configure`，用于配置模型、插件等。额外参数通过 `--` 传递，如 `clawcu config my-instance -- --section model` |
| `clawcu exec <name> <command...>` | 在实例容器内执行任意命令，例如 `clawcu exec my-instance openclaw onboard`、`clawcu exec my-instance pwd` |
| `clawcu logs <name> [--follow]` | 查看实例日志，`--follow` 实时跟踪 |

### 7.7 默认行为约定

- **端口：** 默认 18789（OpenClaw Gateway 内部端口），冲突时按 18789 -> 18799 -> 18809 ... 自动探测
- **资源：** 默认 1 CPU + 2GB 内存，即 1 CU
- **认证：** 强制 token 模式（OpenClaw 要求 lan binding 必须开启认证）
- **数据目录：** 默认 `~/.clawcu/<instance-name>`
- **容器命名：** `clawcu-openclaw-<instance-name>`
- **镜像命名：** `clawcu/openclaw:<version>`（本地统一别名）
- **Gateway 自动配置：** 创建/重建时自动写入 `openclaw.json`，设置 `bind=lan`、`auth.mode=token`、`controlUi.allowedOrigins=["*"]`
- **健康检查：** 创建后每 10 秒轮询 `http://127.0.0.1:<port>/healthz`，不设超时，直到 ready 或进入失败状态
- **Dashboard URL：** 创建成功后输出 `http://127.0.0.1:<port>/#token=<token>`，可直接点击访问

## 8. 用户价值

- 本地部署门槛降低（一条命令创建实例，自动等待 ready，直接输出访问地址）
- 升级风险降低（自动快照 + 失败自动回滚）
- 回滚路径清晰（基于快照的版本回退）
- 做实验更安心（clone 复制数据，完全隔离）
- 故障可追溯（失败实例保留记录，支持 retry；全部操作有历史事件记录）
- 配置更简单（穿透调用 OpenClaw 原生命令，不需要额外学习 ClawCU 的配置体系）
- 更容易把 OpenClaw 当成长期可运维对象，而不是一次性 demo

## 9. 市场推广方向

### 核心关键词

- 本地 AI 基础设施
- 私有 Compute Unit
- OpenClaw 版本化运维
- 自托管数字员工底座

### 传播渠道

- X / Twitter
- 中文 AI 社群
- 独立开发者社区
- 自托管和 homelab 内容社区
- 真实案例复盘内容

### 内容策略

- 展示“一台 Mac mini 如何养数字员工”
- 强调“升级可回退、实验可隔离”
- 避免夸大自动赚钱能力
- 先讲稳定性和控制感，再讲想象空间

## 10. 资源定义

首版定义：

> `1 CU = 1 CPU + 2GB RAM`

这个定义用于建立用户对资源、实例和规模的统一认知。

## 11. 版本路线图

### v0.0.1（当前版本，核心功能已完成）

- 建立产品定位和品牌定义
- 初始化 Git 仓库和项目结构
- 交付 Python CLI 工具，核心命令已实现
- 覆盖 OpenClaw 本地实例完整生命周期：pull、create、list、inspect、start、stop、restart、retry、recreate、upgrade、rollback、clone、logs、remove
- 提供环境检查入口 `clawcu setup`，可检查 Docker、`~/.clawcu` 目录和 shell completion 就绪状态
- 实现 Gateway 自动配置（bind=lan, auth=token, allowedOrigins=*）
- 实现健康检查等待循环，创建后等待真正 ready
- 实现浏览器配对审批（approve）
- 实现 OpenClaw 原生命令穿透调用（config、exec）
- 实现失败实例追踪和 retry 机制
- 实例历史事件记录（create、upgrade、rollback 等全部留痕）
- 镜像策略：GHCR 官方镜像优先，git clone + build fallback
- 通过真实 smoke test 验证完整流程

**待收尾：**
- 未提交的代码变更需要 commit 和整理
- upgrade 命令未经过真实版本切换测试
- 多实例模型/API Key 统一配置管理未实现

### v0.0.2

- 把 `clawcu setup` 从“环境体检”升级为“环境初始化与全局配置入口”
- 支持在 `clawcu setup` 中配置 Docker 镜像仓库来源
  - 例如官方 GHCR、国内镜像代理、私有镜像仓库
  - 目标是减少首次拉取失败和网络敏感问题
- 支持在 `clawcu setup` 中配置 ClawCU home 目录
  - 允许用户显式设置运行目录，而不是仅依赖 `CLAWCU_HOME`
  - 适合把状态、日志、快照放到外置盘或指定目录
- 支持在 `clawcu setup` 中写入并持久化全局默认配置
  - 如默认镜像仓库、默认 home、默认 shell completion 路径
- 为 `clawcu setup` 增加更清晰的“已配置 / 未配置 / 建议操作”输出
- 为后续多实例共享配置能力打基础
  - 先从全局环境配置开始，不直接进入模型/API Key 管理
- 明确区分两类配置
  - 全局环境配置：由 `clawcu setup` 管理
  - 单实例运行配置：由 `clawcu config` 和实例数据目录管理

### v0.1.0

- 增强健康检查（启动超时、异常状态自动诊断）
- `start` 命令增加端口冲突自动重试
- `list` 对失败实例显示 retry/recover 操作提示
- upgrade/clone 命令补充分步进度输出
- 完善实例状态诊断和错误恢复建议
- 多实例模型/API Key 统一配置管理

### v0.2.0

- 加入更多保护机制（操作前确认、资源预检）
- 补强实验实例管理（实验实例标记、批量清理）
- 完善实例历史和事件记录的可读性
- 插件安装管理（跨实例插件共享）

### v0.3.0

- 评估是否需要轻量控制台或状态可视化

## 12. 成功标准

如果 `v0.0.1` 能满足以下条件，就算成功：

- 用户能在本地用 `clawcu` 跑起 OpenClaw ✅（已通过真实 smoke test 验证）
- 用户能清楚知道实例当前版本、端口、资源和数据目录 ✅（list + inspect 命令）
- 用户能升级后快速回退 ✅（upgrade + rollback 命令，含自动快照）
- 用户能复制一份数据做实验，不影响主实例 ✅（clone 命令）
- 用户能完成从创建到访问到配置的完整流程 ✅（create -> approve -> config）
- 用户愿意把 `ClawCU` 继续当成后续产品演进的基础
