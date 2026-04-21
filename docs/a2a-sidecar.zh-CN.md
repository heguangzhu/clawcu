# A2A Sidecar 使用指南

🌐 Language:
[English](a2a-sidecar.md) | [中文](a2a-sidecar.zh-CN.md)

> 本文聚焦 ClawCU 的 A2A sidecar：是什么、为什么做成独立进程、怎么打开、怎么运维。命令行表面细节见 [USAGE_latest.zh-CN.md](../release/USAGE_latest.zh-CN.md) §11。版本上下文见 [RELEASE_v0.3.0.zh-CN.md](../release/RELEASE_v0.3.0.zh-CN.md)。

* * *
## TL;DR

- `clawcu create openclaw|hermes --a2a ...` 把 **A2A v0 sidecar** 烤进实例镜像。
- Sidecar 是原生网关旁边的第二个进程，在邻居端口上发布两个接口：`GET /.well-known/agent-card.json`（发现）和 `POST /a2a/send`（发消息）。
- 不加 `--a2a` 的普通实例一丝不变。A2A 严格 opt-in、纯加法。
- `clawcu a2a up` 一条命令起全套：探测运行中的实例，给没 sidecar 的打 echo bridge，前台跑聚合 registry。
- `clawcu a2a send --to <name> --message "..."` 是冒烟测试命令。

* * *
## 目录

- [Sidecar 是什么](#sidecar-是什么)
- [为什么是 sidecar，不是网关插件](#为什么是-sidecar不是网关插件)
- [架构一览](#架构一览)
- [Opt-in：把 sidecar 烤进实例](#opt-in把-sidecar-烤进实例)
- [协议表面（v0）](#协议表面v0)
- [可选：thread_id 与多轮上下文](#可选thread_id-与多轮上下文)
- [运维内置项](#运维内置项)
- [镜像生命周期与源码 sha 指纹](#镜像生命周期与源码-sha-指纹)
- [双实例完整演练](#双实例完整演练)
- [给已有实例开启 A2A](#给已有实例开启-a2a)
- [`a2a up` vs `registry serve` vs `bridge serve`](#a2a-up-vs-registry-serve-vs-bridge-serve)
- [排障](#排障)
- [当前限制](#当前限制)
- [FAQ](#faq)

* * *
## Sidecar 是什么

这里的 **sidecar** 指：与原生服务打包在同一个容器镜像里、但绑另一个端口、说另一套协议的第二个进程。它**不是**服务里的插件、**不是**原生服务前面的反向代理、也**不是**另一个容器。

ClawCU 做 sidecar 的目的只有一个：在任意受管实例之上暴露 **A2A v0**（一套极小的 agent-to-agent 消息协议），**不需要**服务作者去懂 A2A、也不需要服务去配合 A2A。

sidecar 做的事：

1. 在 `GET /.well-known/agent-card.json` 发布 **AgentCard**，让对等方知道这个 agent 是谁、把消息发到哪。
2. 在 `POST /a2a/send` 收 A2A 消息，翻译成原生服务的 chat/completion API，把回复送回去。
3. **不挡路**：原生网关继续在自己的端口上处理自己的流量。原本的用户看不到任何新东西。

sidecar **不做**的事：

- 不是通用 API 网关。唯一的 POST 只有 `/a2a/send`。
- 不提供流式、多收件人广播、鉴权协商、RPC。v0 就是一进一出。
- 不是原生服务里的"启动钩子"——原生服务没起来时 sidecar 照样起，但 `/healthz` 会如实报告。

* * *
## 为什么是 sidecar，不是网关插件

OpenClaw 和 Hermes 各自都有插件系统。最自然的做法是把 A2A 做成每个服务里的一级插件。ClawCU 刻意没这么做，原因有三：

**1. ClawCU 面向用户，不面向服务作者。** 让用户把一个插件装到特定位置、接进服务配置、还要跟服务升级步调一致——就为了"我想让这些 agent 通话"，这仪式太重了。sidecar 只是原有端口旁边多一个端口，服务内部什么都不改。

**2. 版本解耦。** Sidecar 说的是 A2A v0，不是 OpenClaw / Hermes 内部 API。OpenClaw 升级**不会**强制 A2A 重烤，除非 *sidecar 源码自身*改了。A2A 协议升级**不会**强制 OpenClaw / Hermes 升级。两个维度真正正交。

**3. 服务不可变。** bake-time 的 Dockerfile 层可审计、可从 clawcu 源码树单独重建。没有运行时安装、没有首启时的 `pip install`、没有"插件这次加载上了吗？"的模糊地带。`docker image inspect` 看到什么，跑的就是什么。

代价：多一个端口。在单机开发场景几乎免费——sidecar 默认只绑 127.0.0.1，真撞端口时 `clawcu create --a2a` **在创建时**就会报冲突，不会拖到第一次对话。

* * *
## 架构一览

```
┌──────────────────────── 受管容器 ───────────────────────────┐
│                                                             │
│   ┌────────────────────┐        ┌──────────────────────┐    │
│   │ 原生网关           │        │ A2A sidecar          │    │
│   │  (OpenClaw /       │◀────── │  stdlib 实现         │    │
│   │   Hermes)          │  LLM   │  端口 18790 / 9119   │    │
│   │  端口 18789/8642   │  调用  │  ┌────────────────┐  │    │
│   └────────────────────┘        │  │ GET            │  │    │
│          ▲                      │  │  /.well-known/ │  │    │
│          │                      │  │  agent-card    │  │    │
│          │                      │  ├────────────────┤  │    │
│   （原有用户）                  │  │ POST /a2a/send │  │    │
│                                 │  ├────────────────┤  │    │
│                                 │  │ GET /healthz   │  │    │
│                                 │  └────────────────┘  │    │
│                                 │  按 peer 限流        │    │
│                                 │  日志 tee → 文件     │    │
│                                 │  线程存储（可选）    │    │
│                                 └──────────────────────┘    │
│                                                             │
└──────────────────────┬────────────────────┬─────────────────┘
                       │ 18819 (网关)       │ 18820 (A2A)
                       ▼                    ▼
                    宿主机网络（默认 127.0.0.1）
```

**关键点**：容器内网关路径与 A2A 路径相互独立。对等方发消息进来时，sidecar 在 `127.0.0.1:<内部端口>` 上向网关的 `POST /v1/chat/completions` 发请求——**容器内 localhost**，没有额外的网络跳。

**各服务默认端口**：

| 服务 | 网关端口（容器内） | Sidecar 端口（容器内） | 就绪路径 |
|---|---|---|---|
| OpenClaw | 18789 | 18790 | `/healthz` |
| Hermes | 8642 | 9119 | `/health` |

ClawCU 把两者都发布到宿主。宿主侧的 A2A 端口是 ClawCU 自动挑的（`clawcu inspect <name>` 的 access 信息里能看到）——上表里的容器内端口用户平时不用关心。

* * *
## Opt-in：把 sidecar 烤进实例

`clawcu create` 时加 `--a2a` 会切镜像：

```bash
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a
```

发生的事：

1. ClawCU 计算 **plugin 指纹** `<clawcu_version>.<sha10>`，`sha10` 是对应服务磁盘上 sidecar 源码的 SHA-256。
2. 查本地是否已有镜像 `clawcu/{service}-a2a:{base}-plugin{fingerprint}`。
3. 没有就烤：`FROM {基础镜像} + COPY sidecar + COPY entrypoint.sh + ENTRYPOINT 监督进程`。
4. 实例从烤好的镜像启动。监督进程把原生网关和 sidecar 都拉起来，都跑在 PID 1 下。
5. datadir 里的 `.clawcu-instance.json` 写上 `a2a_enabled: true`，让 `recreate` / `inspect` 知道这是 A2A 实例。

完了。没有创建后续步骤。

验证：

```bash
curl -s http://127.0.0.1:<a2a_port>/.well-known/agent-card.json | jq .
# {
#   "name": "writer",
#   "role": "OpenClaw-backed assistant",
#   "skills": ["chat", "a2a.bridge"],
#   "endpoint": "http://127.0.0.1:18820/a2a/send"
# }
```

* * *
## 协议表面（v0）

### `GET /.well-known/agent-card.json`

返回 JSON：

```json
{
  "name": "writer",
  "role": "OpenClaw-backed assistant",
  "skills": ["chat", "a2a.bridge"],
  "endpoint": "http://127.0.0.1:18820/a2a/send"
}
```

- `name` —— 身份，默认与实例名一致。
- `role` —— 人类可读的角色说明。
- `skills` —— 自由标签，v0 不做强校验；对等方据此判断是否把消息路由过来。
- `endpoint` —— 对等方应 POST 的完整 URL。这是**对外广播的 URL**，在有反向代理时可能与 bind 主机 / 端口不一致。

### `POST /a2a/send`

请求：

```json
{
  "from": "analyst",
  "to": "writer",
  "message": "summarize yesterday's standup",
  "thread_id": "0192a3b4-..."         // 可选 —— 见下节
}
```

响应：

```json
{
  "from": "writer",
  "message": "Yesterday's standup focused on...",
  "thread_id": "0192a3b4-..."         // 仅当请求带了 thread_id 时回显
}
```

错误形如 `{"error": "..."}`，配合相应 HTTP 状态码（400 输入不合法、429 限流、503 网关尚未就绪）。

### `GET /healthz`

返回简单 JSON：`status` / `gateway_ready` / `plugin_version`。`clawcu a2a up` 的探测循环在用；也适合接自己的存活检查。

```json
{
  "status": "ok",
  "gateway_ready": true,
  "plugin_version": "0.3.0.d7226c2b58"
}
```

这里的 `plugin_version` 就是烤镜像时打进 tag 的指纹 —— **出现非预期行为时，先对这个值和你安装的 clawcu 版本对齐一下。** 不一致就是还在跑旧镜像。

* * *
## 可选：thread_id 与多轮上下文

v0 的 `POST /a2a/send` 默认无状态。想要对话在多轮之间累积上下文，请求里带 **`thread_id`**（uuid v7）。每次用同一个 `thread_id` 调过来时，sidecar 会：

1. 把 `{peer, message, timestamp}` 追加到 `<datadir>/threads/<peer>.jsonl`。
2. 下一轮用同一 `thread_id` 来时，把之前的消息当上下文拼在前面，再去调原生网关。

存储格式是 JSONL（每行一条消息，append-only）。一个 peer 一个文件，所以 `writer` 与 `analyst`、`writer` 与 `planner` 之间是独立线程，哪怕调用端用了同一个 `thread_id` 命名空间。

安全：`thread_id` 强制为合法 uuid v7，拒绝 `..` / `/`、避免通过 id 做路径穿越。不传 `thread_id` 也没问题 —— 就是无状态单发。

* * *
## 运维内置项

都烤在 sidecar 里，当前没有对外 flag。在这里列出来，让你心里有数：

- **按 peer 限流** —— token bucket，key 是消息里的 `from` 字段。默认 30 条/分钟/peer。一个聒噪 peer 无法饿死其他 peer 对原生网关的访问。超限返回 `429`。
- **就绪探针** —— 容器启动后 sidecar 带退避地轮询原生网关 `/healthz`（OpenClaw）或 `/health`（Hermes）。sidecar 自己的 `/healthz` 只在后端响应过一次之后才翻 `"ok"`。避免"sidecar 活了但网关还没活"的竞争窗口。
- **日志 tee** —— sidecar 的所有 stdout/stderr 同时写到 `<datadir>/a2a-sidecar.log`。调 A2A 不用 `docker logs`，`tail -f ~/.clawcu/<instance>/a2a-sidecar.log` 就够。
- **可选线程存储** —— 见上节。已做路径穿越加固。

当前 CLI 上没有直接的调节开关。对应旋钮在容器内以环境变量暴露（比如 `A2A_RATE_LIMIT_PER_MINUTE`、`A2A_BIND_PORT`）；需要调就 `clawcu setenv <instance> ...` + `clawcu restart <instance>`。

* * *
## 镜像生命周期与源码 sha 指纹

烤出来的 tag 形如：

```
clawcu/{service}-a2a:{base_version}-plugin{clawcu_version}.{sha10}
```

例子：`clawcu/openclaw-a2a:2026.4.12-plugin0.3.0.d7226c2b58`。

**`sha10`** 是对应服务 `src/clawcu/a2a/sidecar_plugin/<service>/` 目录下所有磁盘文件 SHA-256 的前 10 位。排除：`__pycache__`、`.pyc`、`.pyo`、`__init__.py`（最后这个是打包元数据，不是运行时代码）。

为什么要有 sha：两个理由。

1. **可编辑 dev 安装。** 你 `pip install -e .` 了 clawcu 源码，然后改了 `sidecar/server.js`。clawcu 版本*没变*，但 sidecar 源码变了。没有指纹，`A2AImageBuilder` 会美滋滋地复用陈旧缓存镜像，你盯着幽灵行为调一小时才反应过来。
2. **可审计。** `clawcu/openclaw-a2a:...plugin0.3.0.abc123` 和 `plugin0.3.0.def456` 一眼就知道 sidecar 不一样。`docker image inspect` 就够说明你跑的是哪次 build。

**会触发重烤的改动**：

- `sidecar_plugin/<service>/` 下任意文件（Dockerfile、entrypoint、*.js、*.py）变了。
- clawcu 包版本变了。
- 基础镜像版本（OpenClaw / Hermes 上游）变了。

**不会触发重烤的改动**：

- `__pycache__` / `.pyc` 变化（pytest、import）。
- `__init__.py` 的编辑（仅打包相关）。
- `sidecar_plugin/<service>/` 之外的 Python 代码。

如果出于排错需要强制重烤：`docker image rm clawcu/openclaw-a2a:...` 删 tag，然后在 clone 上重新 `clawcu create --a2a`。

* * *
## 双实例完整演练

典型冒烟测试：两个 A2A 实例经过 registry 对话。

```bash
# 1. 创建两个启用 A2A 的实例（第一次会烤镜像）。
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a

# 2. 起 A2A 拓扑（registry + 必要的 bridge，前台运行）。
clawcu a2a up
# [green]OK[/green] writer  (plugin-backed on :18820)
# [green]OK[/green] analyst (plugin-backed on :9129)
# [bold]A2A registry[/bold] listening on http://127.0.0.1:8765 (Ctrl+C to stop)

# 3. 另开一个终端：发消息。
clawcu a2a send --to analyst --message "summarize yesterday"
# {
#   "from": "analyst",
#   "message": "Yesterday's discussion covered..."
# }
```

直连 sidecar（绕过 registry）也行——就是一个普通 HTTP POST：

```bash
curl -s -X POST http://127.0.0.1:9129/a2a/send \
     -H 'content-type: application/json' \
     -d '{"from":"writer","to":"analyst","message":"hi"}' | jq .
```

* * *
## 给已有实例开启 A2A

当前**没有原地升级**通道 —— 普通实例不会变成 A2A 实例。契约是：`--a2a` 在 `create` 时就得打。给已有实例开启的办法是 clone-first：

```bash
clawcu clone writer --name writer-a2a
clawcu remove writer-a2a                     # 删 clone 的容器
clawcu create openclaw --name writer-a2a \
       --version 2026.4.12 --a2a             # 用克隆的 datadir 重建
```

datadir（模型、历史、env）在 clone + create 过程中保留。只有镜像 tag 从普通版换成 A2A-baked 版。

为什么当前没有原地通道？镜像变更是重建，不是原地变形。我们不希望 `upgrade` 上悄悄多一个 flag 重烤镜像——显式胜过聪明。如果后续实际很痛，可以单独加一个 `clawcu enable-a2a <name>` 动词。

* * *
## `a2a up` vs `registry serve` vs `bridge serve`

三个相关命令，按你的场景选一个：

- **`clawcu a2a up`** —— 常见场景。探测每个运行中的受管实例，给没 sidecar 的起 echo bridge，前台跑聚合 registry。一条命令。
- **`clawcu a2a registry serve`** —— 只跑 registry，不探测、不 bridge。适合每个实例都已烤好 sidecar、不需要 auto-bridge 回退的情况。
- **`clawcu a2a bridge serve --instance <name>`** —— 只给一个实例起 bridge，不起 registry。demo / 离线 / CI 用。实例本身已有 sidecar 时这个 bridge 是不必要的；它存在是为了让没烤的实例也能在 A2A 面前亮个相。

一个直觉模型：

- **Sidecar** = 容器内跑的东西。烤一次，永久在里面。
- **Bridge** = 容器外替没 sidecar 的实例站台。按实例、短暂。
- **Registry** = 聚合器，跨实例。告诉调用方"这台机器上大家的卡片都在这里"。

* * *
## 排障

**`clawcu a2a send` 返回 503 "gateway not ready"。**
Sidecar 先于原生网关起来，还在等后端的 `/healthz` / `/health`。等 10–30 秒重试。如果一直这样，`clawcu logs <instance>` 看看原生网关卡在哪。

**`clawcu a2a send` 返回 429。**
按 peer 限流，默认 30/分钟/peer。稀疏调用间隔，或者 `clawcu setenv <instance> A2A_RATE_LIMIT_PER_MINUTE=120` + `clawcu restart <instance>` 改阈值。

**对端在 200 A2A 回复里返回 OpenClaw / Hermes 错误。**
Sidecar 原样转发原生网关返回的东西。去 `clawcu logs <instance>` 看底层 provider 错误（鉴权、模型、配额）。

**`curl :<port>/.well-known/agent-card.json` 正常，但 `POST /a2a/send` 挂住。**
通常是模型提供方超时。`clawcu a2a send --timeout 120` 放宽等待窗口；长 LLM 调用会超过默认 60 秒。

**烤出来的镜像 tag 跟我预期不一致。**
看 sidecar 的 `/healthz` —— 里面的 `plugin_version` 是权威值。如果跟 `clawcu --version` 对不上，说明还在跑旧镜像；`docker image ls clawcu/*-a2a` 找出来 `docker image rm` 掉，重新 create。

**改了 `sidecar/server.js` 但没生效。**
可编辑安装，`A2AImageBuilder` 看到新 sha 但容器还跑老镜像？`clawcu inspect <instance>` 看当前镜像 tag。如果 tag 已是新指纹但行为还是老的，就是没重启容器 —— `clawcu restart <instance>`。

**create 时端口冲突。**
`clawcu create --a2a` 在创建时就会探 A2A 端口。被占用会立即报错。换 `--port` 或在宿主上释放端口。

**日志去哪看？**
- `clawcu logs <instance>` —— 原生网关日志（底层就是 docker logs）。
- `tail -f ~/.clawcu/<instance>/a2a-sidecar.log` —— sidecar 专属日志（上面讲的 tee 文件）。

* * *
## 当前限制

- **协议版本：v0。** `v1` 之前契约还可能扩（流式、鉴权、多收件人、更丰富的错误分类）。请把客户端钉在 v0 的请求/响应形状，并假定 `0.3.x` 线上的变动是向后兼容的新增，而不是破坏性修改。
- **内建无鉴权。** `/a2a/send` 接受任何能触达端口的请求。Sidecar 默认只绑 127.0.0.1，单机 OK；跨机时请放到做鉴权的反向代理后面，或者等协议扩展。
- **Registry 仅本地。** 聚合**本机**受管实例的卡片。跨机联邦不在 v0 范围。
- **普通 / A2A 创建时硬切换。** 无原地开启；用 clone-first。
- **Sidecar 不做流式。** v0 是请求/响应。原生服务就算流式返回，sidecar 也是等完整回复再一次性以 JSON 返回。

* * *
## FAQ

**不用 `--a2a` 的普通实例会有额外开销吗？**
没有。Sidecar 只在 A2A-baked 镜像里跑。普通实例用的还是原镜像 tag，字节级等同于 `v0.2.x`。

**能运行时关掉 sidecar，不重建实例吗？**
没干净办法。监督进程在容器启动时把 sidecar 拉起来。你可以 `docker exec <container> kill $(pgrep -f sidecar)` 杀进程，但这是 hack——下次重启就回来了。

**开销多大？**
一个常驻 stdlib-only HTTP 进程。内存：两个服务闲时各 <30 MB。CPU：静息 0；每次请求的成本主要来自下游 LLM 调用。

**和 `clawcu exec` / `clawcu tui` 会冲突吗？**
不会。Sidecar 是并行进程，`exec` / `tui` / `token` / `config` 与非 A2A 实例行为完全一致。

**同一个实例能跑两个 sidecar（比如 A2A v0 和 v1 并排）吗？**
当前不支持。一个实例一个 sidecar、一个端口。未来协议版本倾向于在同端口做向后兼容的增量。

**`/healthz` 里的 `plugin_version` 是哪里来的？**
是烤镜像时通过 `CLAWCU_PLUGIN_VERSION` build-arg 打进镜像的完整 `<clawcu_version>.<sha10>` 指纹。和镜像 tag 里的是同一个值。

**怎么独立于服务升级 sidecar 代码？**
`pip install --upgrade clawcu`，然后 `clawcu clone <name> --name <name>-new` + `clawcu create ... --a2a --version <同一服务版本>`。`service` 基础不动；只是指纹变了导致 sidecar 层重烤。

* * *

延伸阅读：

- [USAGE_latest.zh-CN.md](../release/USAGE_latest.zh-CN.md) —— `clawcu a2a` 命令参考
- [RELEASE_v0.3.0.zh-CN.md](../release/RELEASE_v0.3.0.zh-CN.md) —— A2A 设计动机、兼容、路线图
- [CHANGELOG.md](../CHANGELOG.md) —— 完整版本历史
