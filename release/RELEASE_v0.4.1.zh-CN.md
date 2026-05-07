# ClawCU v0.4.1

🌐 语言：
[English](RELEASE_v0.4.1.md) | [中文](RELEASE_v0.4.1.zh-CN.md)

发布日期：2026年4月29日

> `v0.4.1` 基于重度用户反馈对 CLI 生命周期命令进行了打磨。所有变更均为增量或可选；不破坏现有工作流。

* * *
## 亮点

### Dashboard（Docker 常驻容器）

- **`clawcu dashboard` 现在以 Docker 容器运行**
  - 首次运行时自动构建 `clawcu-dashboard:<version>` 镜像。
  - 容器以 `--restart unless-stopped` 常驻后台。
  - 挂载 `~/.clawcu`、`~/.openclaw`、`~/.hermes` 和 `/var/run/docker.sock`。
  - 默认发布在 `127.0.0.1:8765`（仅本地可访问）。
  - 新增 flag：`--stop`、`--restart`、`--status`、`--rebuild`。
  - Dashboard 交互操作（`open_cli`、`open_config`、`open_tui`）在容器内优雅降级，提示用户在宿主机执行对应命令。
  - `/health` 健康检查端点，供 Docker HEALTHCHECK 和启动轮询使用。

### Provider 命令（跨服务认证/模型管理）

- **`clawcu provider collect/list/show/apply/remove`**
  - 从 OpenClaw 和 Hermes 收集 provider 认证 bundle，转换为统一规范形式。
  - 将一个 provider 应用到另一个实例，无需重新输入密钥。
  - `list` 显示 `IN_USE` 列，指示哪些实例引用了该 provider。
  - OAuth 检测：对使用 OAuth 的 provider 显示 `oauth` 状态。
  - 跨服务 apply：OAuth token 和 API key 可在 OpenClaw 和 Hermes 之间迁移。

### CLI 重新设计（v0.4.x 基础）

- **`list` 默认不再显示版本页脚**
  - `clawcu list` 不再打印版本页脚。需要时加 `--versions` 即可查看可用服务版本。

- **`token` / `approve` 移至 `clawcu openclaw` 下**
  - 根级别的 `clawcu token` 和 `clawcu approve` 仍可作为隐藏别名使用，但会显示弃用警告。
  - 新位置：`clawcu openclaw token <name>` 和 `clawcu openclaw approve <name>`。

- **移除 `pull`**
  - `clawcu create` 现在通过服务层自动拉取镜像。单独的 `pull` 步骤是冗余的。

- **移除 `hermes identity set`**
  - 直接使用 `docker cp` 或 `clawcu exec <name>` 编辑 Hermes 人格文件。

### 场景优化

- **`tui` 启动前检查实例状态**
  - 如果实例已停止，CLI 会明确退出并提示：`Run clawcu start <name> to start it before entering the TUI.`
  - 不自动启动——在资源受限的机器上，由用户决定启动哪个实例。

- **`remove` 自动停止运行中的实例**
  - `clawcu remove <name> --delete-data` 现在会在删除前自动停止运行中的容器（10秒优雅期）。
  - 之前这会导致 Docker 报错；现在一条命令即可完成。

- **`remove` 对孤儿数据目录自动提示**
  - 当对记录已消失但数据目录仍在的实例执行 `clawcu remove <name>` 时，CLI 会一步提示并删除孤儿数据。
  - 无需重新加 `--removed` 执行；正确的操作路径会自动提供。

- **`logs --follow` Ctrl+C 后 ANSI 重置**
  - 在 `clawcu logs <name> --follow` 期间按 Ctrl+C 会发送 ANSI 重置序列，防止终端保留 Docker 的颜色代码。

- **`getenv --table` 分组输出**
  - `clawcu getenv <name> --table` 以富文本表格形式渲染环境变量，按敏感 / 通用分组。
  - 敏感值默认脱敏；加 `--reveal` 可显示原始值。

- **`setenv --reload` 热重载**
  - `clawcu setenv <name> KEY=VALUE --reload` 在写入环境文件后向运行中的容器发送 SIGHUP。
  - 尽力而为：支持信号触发的配置重载的服务无需重建即可生效。如果信号发送失败，CLI 会提示改用 `--apply`。

- **`snapshots` 子命令组**
  - `clawcu snapshots list [name]` — 列出实例（或所有实例）的升级/回滚快照。
  - `clawcu snapshots clean --keep-last N [name]` — 清理旧快照，每个实例保留最近的 N 个。
  - 每次 `upgrade` 成功后，ClawCU 自动清理超过最近 10 个的快照（被历史记录引用的快照永不被删除）。

- **`upgrade --list-versions` 失败回退增强**
  - 当远程镜像仓库不可达时，CLI 现在会显示醒目的 `[green]Fallback:[/green]` 消息，展示本地镜像以及使用本地标签升级的确切命令。

- **`config` 帮助增加服务专属示例**
  - `clawcu config --help` 现在显示 OpenClaw 和 Hermes 的用法示例，包括 `--non-interactive` 透传。

* * *
## 兼容性

`v0.4.1` 可从 `v0.3.0` 直接升级。

- 现有托管实例保持相同的镜像标签、端口和环境变量继续运行。
- 移除的命令（`pull`、`hermes identity set`）在 `v0.3.0` 用法中已隐藏或未记录。
- `clawcu token` / `clawcu approve` 根别名仍可工作，并显示弃用警告。
- 新标志（`--table`、`--reload`、`--versions`）严格可选。

* * *
## 测试覆盖

**848 个测试**（pytest），比 `v0.3.0` 的 479 个大幅增加。

* * *
## 推荐工作流

```bash
# 创建并启动
clawcu create openclaw --name writer --version 2026.4.1

# 克隆前检查环境变量
clawcu getenv writer --table

# 不带密钥克隆，然后应用 provider
clawcu clone writer --name writer-shared --exclude-secrets
clawcu provider apply <provider> writer-shared

# 升级（含安全快照 + 自动清理）
clawcu upgrade writer --version 2026.4.2

# 按需手动清理旧快照
clawcu snapshots clean --keep-last 5
```

* * *
## 结语

`v0.4.x` 是第一个将 CLI 定位为**生命周期工具**而非运行时的版本。CLI 专注于创建、启动、停止、升级、回滚、删除、环境变量、日志和快照。
