# ClawCU

🌐 Language:
[English](README.md) | [中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/clawcu.svg)](https://pypi.org/project/clawcu/)
[![Python](https://img.shields.io/pypi/pyversions/clawcu.svg)](https://pypi.org/project/clawcu/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/heguangzhu/clawcu/actions/workflows/ci.yml/badge.svg)](https://github.com/heguangzhu/clawcu/actions/workflows/ci.yml)

`ClawCU` 是一个面向本地多 AI Agent Runtime 的生命周期管理工具，适合在同一台机器上稳定运行多个实例。目前支持 [OpenClaw](https://github.com/openclaw/openclaw) 和 [Hermes Agent](https://github.com/NousResearch/hermes-agent)。

<details>
<summary>目录</summary>

- [核心亮点](#核心亮点)
- [安装](#安装)
- [快速开始](#快速开始)
- [安全升级流程](#安全升级流程)
- [模型配置](#模型配置)
- [环境变量与访问](#环境变量与访问)
- [发布历史](#发布历史)
- [参与贡献](#参与贡献)
- [卸载](#卸载)
- [License](#license)

</details>

## 核心亮点

- **一套 CLI，两个运行时** — OpenClaw 和 Hermes 共用同一套生命周期命令
- **每次升级前自动快照** — datadir 与环境变量同步捕获；`rollback` 从真实备份恢复
- **先克隆再实验** — 复制一份实例，在副本上升级，主实例原地不动
- **Agent-to-agent 消息（`v0.3.0`）** — 创建时加 `--a2a`，把 A2A v0 sidecar 烤进受管镜像，在邻居端口上暴露 `/.well-known/agent-card.json` + `POST /a2a/send`。普通实例不受影响。
- **Available versions 支持缓存刷新** — 默认 `clawcu list` 走按天缓存，需要立即看最新 tag 时用 `clawcu list --no-cache`

```text
$ clawcu list
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ NAME            ┃ SERVICE  ┃ VERSION   ┃ PORT  ┃ STATUS  ┃ ACCESS          ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ writer          │ openclaw │ 2026.4.1  │ 18799 │ running │ 127.0.0.1:18799 │
│ analyst         │ hermes   │ 2026.4.13 │ 9129  │ running │ 127.0.0.1:9129  │
└─────────────────┴──────────┴───────────┴───────┴─────────┴─────────────────┘

Available versions (top 10 by semver, newest first)
  openclaw  2026.4.15, 2026.4.14, 2026.4.12, 2026.4.11, 2026.4.10, 2026.4.9,
            2026.4.8, 2026.4.7, 2026.4.5, 2026.4.2
  hermes    2026.4.16, 2026.4.13, 2026.4.8, 2026.4.3, 2026.3.30
```

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

首次使用，检查 Docker 访问并配置默认值：

```bash
clawcu setup
```

创建一个 OpenClaw 实例并进入 TUI：

```bash
clawcu pull openclaw --version 2026.4.1
clawcu create openclaw --name writer --version 2026.4.1
clawcu tui writer
```

如果你维护了自定义运行镜像，可以继续显式记录逻辑版本，同时只覆盖实际运行镜像：

```bash
clawcu create openclaw \
  --name writer-tools \
  --version 2026.4.1 \
  --image registry.example.com/openclaw:2026.4.1-tools
```

或者用同样的模式创建一个 Hermes 实例：

```bash
clawcu pull hermes --version 2026.4.13
clawcu create hermes --name analyst --version 2026.4.13
clawcu tui analyst
```

进入实例原生的配置流程（OpenClaw 的 `configure`、Hermes 的 `setup`），在容器内完成模型 / API key 等设置：

```bash
clawcu config writer
```

用 A2A 让实例之间对话（opt-in，`v0.3.0`）：

```bash
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a
clawcu a2a up                                             # 注册中心 + 桥接，一条命令
clawcu a2a send --to analyst --message "summarize yesterday"
```

如果你想忽略当天的 Available versions 缓存，立刻重新拉一遍 registry：

```bash
clawcu list --no-cache
```

A2A sidecar 深度指南（架构 / 协议 / 运维 / 排障）：[docs/a2a-sidecar.zh-CN.md](docs/a2a-sidecar.zh-CN.md)。完整命令参考（`list` / `inspect` / `exec` / `upgrade` / `provider` / `a2a` …）见 [USAGE_latest.zh-CN.md](release/USAGE_latest.zh-CN.md)。

## 安全升级流程

先在克隆上升级，验证通过后再升级主实例：

```bash
clawcu clone writer --name writer-upgrade-test
clawcu upgrade writer-upgrade-test --version 2026.4.10
clawcu rollback writer-upgrade-test    # 新版本有问题时回滚
```

`upgrade` 也支持 `--image`：当你想保留目标版本标签，但实际运行自定义镜像时使用：

```bash
clawcu upgrade writer-upgrade-test \
  --version 2026.4.10 \
  --image registry.example.com/openclaw:2026.4.10-tools
```

每次 `upgrade` 都会先对实例 datadir 和对应的环境变量文件（OpenClaw 在 `~/.clawcu/instances/<instance>.env`，Hermes 在 `<datadir>/.env`）创建快照，然后才替换容器。升级失败时 ClawCU 会自动恢复两者。若使用了 `--image`，选中的 runtime image 也会写入实例状态，因此后续 `recreate`、孤儿恢复和 `rollback` 会沿着同一条镜像链继续工作。

## 模型配置

从任意受管实例或本地 home 采集 API key 和模型列表，在别处复用：

```bash
clawcu provider collect --all
clawcu provider list
clawcu provider apply openclaw:minimax writer --agent main --primary minimax/MiniMax-M2.7
```

每份采集的配置都会带上服务标识，OpenClaw 和 Hermes 中同名的配置不会静默冲突。

## 环境变量与访问

环境变量文件按服务原生路径管理，不强行统一：

- OpenClaw：`~/.clawcu/instances/<instance>.env`
- Hermes：`<datadir>/.env`

用 `clawcu setenv` / `getenv` / `unsetenv` 管理（加 `--apply` 会立即 recreate 容器以重新加载环境变量）。服务专属访问：`clawcu token <instance>` 和 `clawcu approve <instance>` 只适用于 OpenClaw（对应其 dashboard token + pairing 模型）；Hermes 使用 `clawcu tui` / `config` / `exec` 作为运维入口。完整命令参考：[USAGE_latest.zh-CN.md](release/USAGE_latest.zh-CN.md)。

## 发布历史

版本演进见 [CHANGELOG.md](CHANGELOG.md)。最新发布说明：[RELEASE_latest.zh-CN.md](release/RELEASE_latest.zh-CN.md)。

## 参与贡献

Issue 和 PR 都欢迎，在 [github.com/heguangzhu/clawcu/issues](https://github.com/heguangzhu/clawcu/issues) 提交。

本地开发：

```bash
git clone https://github.com/heguangzhu/clawcu.git
cd clawcu
uv sync --all-extras
uv run pytest -q
```

## 卸载

```bash
pip uninstall clawcu
# 或
pipx uninstall clawcu
# 或
uv tool uninstall clawcu
```

卸载 CLI 不会触碰 `~/.clawcu` 下的任何 datadir。如果需要彻底清除实例数据，卸载前用 `clawcu remove <name> --delete-data` 显式删除。

## License

MIT — 详见 [LICENSE](LICENSE)。
