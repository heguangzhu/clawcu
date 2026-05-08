# ClawCU v0.5.1

🌐 语言：
[English](RELEASE_v0.5.1.md) | [中文](RELEASE_v0.5.1.zh-CN.md)

发布日期：2026-05-08

## 亮点

- Dashboard 改为 Docker 常驻容器运行，并提供 `--stop`、`--restart`、`--status`、`--rebuild` 控制。
- Provider 命令支持跨服务采集、列出、查看、应用和删除 auth/model bundle。
- `clawcu list --versions` 显式显示可升级候选版本，并支持 `--no-cache` 强制刷新。
- 被删除实例的恢复流程进入主线：`list --removed`、`recreate`、`remove --removed`。
- `upgrade`、`rollback` 等高风险生命周期操作仍会先快照 datadir 和 env 文件。
- A2A 功能已从 `main` 拆出，保留在独立的 `a2a` 分支。

## 说明

当前 `main` 分支发布线专注于 OpenClaw 与 Hermes 的本地生命周期管理。实验性的 agent-to-agent 功能保留在独立的 `a2a` 分支。
