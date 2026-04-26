# ClawCU Usage v0.2.12

🌐 Language:
[English](USAGE_v0.2.12.md) | [中文](USAGE_v0.2.12.zh-CN.md)

发布范围：`v0.2.12`

`ClawCU v0.2.12` 的命令参考。覆盖 OpenClaw 与 Hermes 的共享命令界面、孤儿实例生命周期、`v0.2.11` 的 runtime image 覆盖流程，以及 `v0.2.12` 新增的 `clawcu list --no-cache`，用于对 Available Versions 做一次性强制刷新。

## 1. 初始化与镜像准备

### `clawcu --version`

```bash
clawcu --version
```

显示已安装的 ClawCU 版本。

### `clawcu setup`

```bash
clawcu setup [--completion]
```

检查 Docker CLI、Docker daemon 可达性、ClawCU home 与运行目录，并交互式配置默认 ClawCU home、OpenClaw image repo、Hermes image repo。

### `clawcu pull openclaw`

```bash
clawcu pull openclaw --version <version>
```

预取官方 OpenClaw 镜像引用。本地缺失镜像时，后续 `create` / `start` / `recreate` 会在需要时触发 Docker 下载。

### `clawcu pull hermes`

```bash
clawcu pull hermes --version <tag>
```

从配置的 Hermes image repo 拉取对应 tag 的预构建镜像。

## 2. 实例创建

### `clawcu create openclaw`

```bash
clawcu create openclaw --name <name> --version <version>
                       [--image <ref>]
                       [--datadir <path>] [--port <port>]
                       [--cpu 1] [--memory 2g]
```

- `--version <version>` —— 必填逻辑版本标签，会写入实例记录
- `--image <ref>` —— 可选 runtime image 覆盖。Docker 启动该镜像，但记录中的 OpenClaw 版本仍取 `--version`
- `datadir` 默认 `~/.clawcu/<name>`
- 默认端口 `18799`，冲突时以 `+10` 步长探测
- 会在 datadir 中写入 `.clawcu-instance.json`

### `clawcu create hermes`

```bash
clawcu create hermes --name <name> --version <ref>
                     [--image <ref>]
                     [--datadir <path>] [--port <port>]
                     [--cpu 1] [--memory 2g]
```

- `--version <ref>` —— 必填逻辑版本 / ref，会写入实例记录
- `--image <ref>` —— 可选 runtime image 覆盖。Docker 启动该镜像，但记录中的 Hermes 版本仍取 `--version`
- `datadir` 默认 `~/.clawcu/<name>`
- API 端口默认 `8652`，仪表盘端口起点 `9129`
- 两者都以 `+10` 步长探测
- 同样写入 `.clawcu-instance.json`

## 3. 共享生命周期命令

### `clawcu list` _(别名：`ls`)_

```bash
clawcu list [--source managed|local|removed|all]
            [--local] [--managed] [--all] [--removed]
            [--service X] [--status X] [--running]
            [--agents] [--wide] [--reveal]
            [--remote/--no-remote] [--no-cache] [--json]
```

列出实例摘要或逐 agent 行。默认 source 为 `managed`。

- `--local` / `--managed` / `--all` / `--removed` —— source 快捷别名
- `--removed` —— 列出 `CLAWCU_HOME` 下记录已删除、但 datadir 仍存在的孤儿实例
- `--agents` —— 逐 agent 一行，而非逐实例
- `--wide` —— 增加 SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT 列
- `--reveal` —— 显示完整 dashboard token
- `--remote` / `--no-remote` —— 控制文本页脚里的 "Available versions" registry 拉取（默认开）
- `--no-cache` —— 跳过当天的 `<clawcu_home>/cache/available_versions.json`，强制重新拉取页脚版本；成功结果仍会刷新回缓存
- 页脚行为：
  - 每个服务最多展示 10 个稳定版本，最新在前
  - 预发布（`-beta`、`-rc`、`-alpha`）被过滤
  - `--json` / `--agents` / `--removed` 视图下不渲染
  - registry 不可达或 `--no-remote` 时，会回退展示本地 Docker 镜像
- `--json` —— 机器可读实例数组；版本页脚只在文本模式渲染

### `clawcu inspect`

```bash
clawcu inspect <name> [--show-history] [--reveal]
```

以紧凑可读的方式显示实例状态（摘要 / access / 快照 / 容器 / 历史）。

### `clawcu start`

```bash
clawcu start <name>
```

启动处于 stopped 状态的受管实例。

### `clawcu stop`

```bash
clawcu stop <name> [--time N | -t N]
```

停止运行中实例。`--time` 为优雅关机秒数（默认 `5`）。

### `clawcu restart`

```bash
clawcu restart <name> [--no-recreate-if-config-changed]
```

重启实例。若检测到环境变量漂移或容器缺失，默认会升级为完整 `recreate`。

### `clawcu recreate`

```bash
clawcu recreate <name> [--fresh] [--timeout N]
                       [--version <v>] [--yes]
```

按保存配置重建容器，或从剩余 datadir 恢复一个已删除实例。

- `--fresh` —— 重建前清空 datadir
- `--timeout N` —— 强制删除前的优雅 stop 窗口
- `--version <v>` —— 恢复旧 datadir 且缺少 `.clawcu-instance.json` 时显式指定版本
- 已受管实例会复用保存下来的 runtime image

### `clawcu upgrade`

```bash
clawcu upgrade <name> [--version <v>] [--list-versions]
                      [--image <ref>]
                      [--remote/--no-remote] [--all-versions]
                      [--dry-run] [--yes] [--json]
```

升级到新版本。替换容器前对 datadir 与对应环境变量路径做 snapshot。

- `--version <v>` —— 除 `--list-versions` 外必填
- `--image <ref>` —— 可选 runtime image 覆盖
- `--list-versions` —— 列候选版本
- `--no-remote` —— 跳过 registry 拉取
- `--all-versions` —— 展示完整 remote tag
- `--dry-run` / `--yes` / `--json` —— 计划预览、非交互确认、机器输出
- 选中的 runtime image 会持久化，后续 `recreate`、孤儿恢复和 `rollback` 会沿用同一条镜像链

### `clawcu rollback`

```bash
clawcu rollback <name> [--to <version>] [--list]
                       [--dry-run] [--yes] [--json]
```

回滚到更早的 snapshot。不带 `--to` 时回滚最近一次可逆转换。

### `clawcu clone`

```bash
clawcu clone <source> --name <name>
                      [--datadir <path>] [--port <port>]
                      [--version <v>]
                      [--include-secrets/--exclude-secrets]
```

把 source 实例复制成新的隔离实验实例。

### `clawcu logs`

```bash
clawcu logs <name> [--follow] [--tail N] [--since DURATION]
```

显示实例日志。默认最近 200 行。

### `clawcu remove` _(别名：`rm`)_

```bash
clawcu remove <name> [--keep-data|--delete-data]
                     [--removed] [--yes]
```

移除受管实例。默认保留 datadir。

- `--delete-data` —— 同时删除 datadir
- `--removed` —— 永久删除 `clawcu list --removed` 列出的孤儿 datadir

## 4. 孤儿实例生命周期

实例记录丢失后，其 datadir 会成为"孤儿"——仍在 `CLAWCU_HOME` 下，但不再受跟踪。

| 步骤 | 命令 | 说明 |
|------|------|------|
| 发现 | `clawcu list --removed` | 枚举孤儿 datadir，并从 `.clawcu-instance.json` 还原 service / version / port。 |
| 恢复 | `clawcu recreate <orphan>` | 从孤儿 datadir 重建受管实例。 |
| 恢复（老 datadir） | `clawcu recreate <orphan> --version <v>` | 恢复 `.clawcu-instance.json` 出现之前的 datadir。 |
| 永久删除 | `clawcu remove <orphan> --removed [--yes]` | 清理孤儿 datadir。 |

## 5. 交互访问与原生命令

### `clawcu config`

```bash
clawcu config <name> [-- args...]
```

在受管容器内执行服务原生配置流程。OpenClaw 对应 `openclaw configure`，Hermes 对应 `hermes setup`。

### `clawcu exec`

```bash
clawcu exec <name> <command...>
```

在受管容器内执行任意命令，并注入实例环境变量。

### `clawcu tui`

```bash
clawcu tui <name> [--agent <agent>]
```

启动原生交互流程。OpenClaw 使用其 TUI；Hermes 使用其交互 chat。

## 6. 服务相关访问命令

### `clawcu token` _(仅 OpenClaw)_

```bash
clawcu token <name> [--copy] [--url-only|--token-only] [--json]
```

打印 OpenClaw dashboard token。Hermes 实例会失败并提示去用 `clawcu config <name>`。

### `clawcu approve` _(仅 OpenClaw)_

```bash
clawcu approve <name> [requestId]
```

批准 OpenClaw 浏览器配对请求。Hermes 实例会以 unsupported 失败。

## 7. 环境变量管理

### `clawcu setenv`

```bash
clawcu setenv <name> KEY=VALUE [KEY=VALUE ...]
                     [--from-file <path>]
                     [--dry-run] [--reveal] [--apply]
```

向实例环境变量文件写入变量。

### `clawcu getenv`

```bash
clawcu getenv <name> [--reveal] [--json]
```

打印实例当前环境变量。

### `clawcu unsetenv`

```bash
clawcu unsetenv <name> KEY [KEY ...]
                       [--dry-run] [--reveal] [--apply]
```

删除环境变量。

## 8. 模型配置的采集与复用

### `clawcu provider collect`

```bash
clawcu provider collect (--all | --instance <name> | --path <home>)
```

采集模型配置资产。

### `clawcu provider list`

```bash
clawcu provider list
```

列出已采集的模型配置资产。

### `clawcu provider show`

```bash
clawcu provider show <name>
```

查看某资产的保存内容。跨服务同名时用 `openclaw:<name>` / `hermes:<name>` 消歧义。

### `clawcu provider remove`

```bash
clawcu provider remove <name>
```

删除已采集资产。

### `clawcu provider models list`

```bash
clawcu provider models list <name>
```

列出某资产中的模型。

### `clawcu provider apply`

```bash
clawcu provider apply <provider> <instance>
                      [--agent <agent>]
                      [--primary <model>]
                      [--fallbacks <m1,m2>]
                      [--persist]
```

把已采集资产应用到目标实例。

## 9. 默认行为约定

- 端口默认：
  - OpenClaw 受管实例从 `18799` 起
  - Hermes 受管 API 端口从 `8652` 起
  - Hermes 仪表盘端口从 `9129` 起
  - 冲突时均以 `+10` 步长探测
- 数据目录：
  - 默认 `~/.clawcu/<instance-name>`
- 容器命名：
  - `clawcu-<service>-<instance-name>`
- Datadir 元数据：
  - 每个实例都带 `.clawcu-instance.json`
  - 支持零输入孤儿恢复
- 访问信息：
  - 两个服务的 `create` / `list` / `inspect` 都会暴露 access URL
  - OpenClaw 显示主服务端口与 dashboard URL
  - Hermes 显示 dashboard 端口与 dashboard URL
- Env 位置：
  - OpenClaw 使用 `~/.clawcu/instances/<instance>.env`
  - Hermes 使用 `<datadir>/.env`
- 快照行为：
  - `upgrade` 与 `rollback` 同时保存并还原 datadir 与对应环境变量路径
- 记录兼容性：
  - 加载保存的实例记录时，会忽略未知历史字段

## 10. 备注

- 本文档描述 `v0.2.12` 命令界面。
- 发布上下文见 [RELEASE_v0.2.12.zh-CN.md](RELEASE_v0.2.12.zh-CN.md)。
- 快捷方式：[USAGE_latest.zh-CN.md](USAGE_latest.zh-CN.md) 始终指向当前发布版本。
