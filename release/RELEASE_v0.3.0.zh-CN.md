# ClawCU v0.3.0

🌐 Language:
[English](RELEASE_v0.3.0.md) | [中文](RELEASE_v0.3.0.zh-CN.md)

发布日期：2026 年 4 月 22 日

> `v0.3.0` 把 **agent-to-agent 消息协议**带进 ClawCU。创建实例时加一个 `--a2a`，ClawCU 就会把 A2A v0 sidecar 烤进受管服务镜像，让任意实例在其原生网关旁边的邻居端口上同时暴露 `GET /.well-known/agent-card.json` 与 `POST /a2a/send`。原生服务行为完全不变 —— A2A 是**纯加法**。

* * *
## 亮点

- **创建实例时 `--a2a` 一键开启**
  - `clawcu create openclaw --name ... --a2a` 或 `clawcu create hermes --name ... --a2a`，把 A2A sidecar 烤进派生镜像。
  - 不加 `--a2a` 的普通实例与 `v0.2.x` 完全一致。sidecar 层对未启用者不可见。
  - 两个服务都支持：OpenClaw 用 Node sidecar（stdlib `node:http`），Hermes 用 Python sidecar（stdlib `http.server`）。无新运行时依赖。

- **邻居端口协议，不是劫持网关**
  - Sidecar 在原生网关旁边绑定第二个端口。OpenClaw 网关 18789 依然是 18789，A2A 跑在 18790。Hermes 网关照常，A2A 在 `A2A_BIND_PORT`（默认 9119）。
  - `/.well-known/agent-card.json` 返回自描述 AgentCard。
  - `POST /a2a/send` 接收 `{"from": "...", "to": "...", "message": "..."}`（可选 `thread_id`），桥接到原生 LLM 后端，返回 `{"from": "...", "message": "..."}`。

- **镜像 tag 带源码 sha 指纹**
  - Tag 形如 `clawcu/{service}-a2a:{base}-plugin{clawcu_version}.{sha}`，其中 `sha` 是 sidecar 源码（Dockerfile / entrypoint / *.js / *.py）的 SHA-256。
  - 可编辑安装（`pip install -e .`）下改了 sidecar，sha 就变，`A2AImageBuilder` 自动烤新 tag。代码改了之后不会再服务陈旧镜像。

- **Sidecar 加固**
  - 按 peer 做限流（token bucket，key 为消息的 `from`），一个聒噪 peer 无法饿死网关。
  - 就绪探针：sidecar 启动后轮询原生网关，后端响应之后才把 `/healthz` 翻到 `ok`。
  - 日志 tee：sidecar 的 stdout/stderr 镜像到 `<datadir>/a2a-sidecar.log`，不用 `docker logs` 就能事后看。
  - 可选 `thread_id`：按 peer 的 JSONL 对话历史落在 `<datadir>/threads/`，已做路径穿越加固（强制 uuid v7，拒绝 `..` / `/`）。

- **`clawcu hermes identity set <name> <path>`**
  - 把用户写的 `SOUL.md` 装进 Hermes 实例 datadir，`prompt_builder.load_soul_md` 下一轮对话就能拿到新人格 —— 不用重启，不用 recreate。

- **Wheel 打包修复**
  - `v0.3.0` 把 sidecar 资产（`Dockerfile` / `entrypoint.sh` / `*.js` / Hermes `sidecar.py`）作为 package-data 收进 wheel。`pip install clawcu==0.3.0` → `clawcu create --a2a` 从 PyPI 装完就能直接用，不再需要 clone 源码。

- **479 条测试**（pytest 450 + Node sidecar 测试 29，`v0.2.10` 是 366），覆盖镜像指纹稳定性、AgentCard 推导、限流桶、就绪探针、线程存储、以及 Node / Python sidecar 表面的端到端路径。

* * *
## 为什么要 A2A

一台机器上跑多个 agent 的那天起，你就会想让它们互相说话。朴素的做法——"A 指到 B 的 API"——会把每一对 agent 绑在彼此的鉴权方式、请求格式、流式细节上。两两就够呛，三个就散架。

A2A v0 是让 N 个 agent 相互发现、互相发消息的最小契约：

- **AgentCard** (`GET /.well-known/agent-card.json`) —— `{name, role, skills, endpoint}`。一个 JSON 文件，放在 well-known URL。
- **send** (`POST /a2a/send`) —— 进一条消息，出一条回复。

就这些。没有流式，没有传输协商，没有能力握手。v0 刻意薄 —— 互操作的价值在于收敛，不在于功能多。

ClawCU 的职责是让*任何*受管实例都能这么用，不需要服务作者去懂 A2A。你打 `--a2a`，ClawCU 烤 sidecar，实例就可达。服务本身还是原来那个服务。

* * *
## 为什么用 sidecar，而不是网关插件

OpenClaw 和 Hermes 都有自己的插件系统。最自然的做法是把 A2A 做成每个服务里的一级插件。我们没这么做，有三个原因。

1. **ClawCU 面向用户，不面向服务作者。** 让用户把一个插件装到特定位置、在服务配置里接好线、还要跟服务升级步调一致 —— 就为了"我想让这些 agent 通话"，太仪式了。sidecar 只是一个原有端口旁边的另一个端口，服务内部什么都不改。

2. **版本解耦。** Sidecar 说的是 A2A v0，不是 OpenClaw / Hermes 内部 API。OpenClaw 升级不会强制 A2A 重烤，除非 *sidecar* 源码变了。A2A 协议升级也不会强制 OpenClaw 升级。两个维度真正正交。

3. **服务不可变。** bake-time 的 Dockerfile 层可审计、可从 clawcu 源码树单独重建。没有运行时安装、没有容器第一次启动时的 `pip install`、没有"插件加载了没？"的模糊地带。`docker image inspect` 看到什么，跑的就是什么。

代价：多一个端口。单机场景里几乎是白给 —— sidecar 默认只绑 127.0.0.1。真的撞端口时，`clawcu create --a2a` 在创建时就会报冲突，不会拖到第一次对话。

* * *
## 为什么是 opt-in

`--a2a` 默认关闭。从 `v0.2.10` 升到 `v0.3.0`，普通实例行为一丝未变。理由：

- Sidecar 是第二个进程。10 个实例就是 10 个多出来的 sidecar，不是白给。
- 有些用户不希望网关之外再暴露任何端口。"我给你烤了个 A2A 端口" 算是意外，而生命周期工具里意外是坏事。
- 协议还在 v0。全量烤等于隐式承诺 v0 稳定。它不稳 —— 在 `v1` 之前契约还可能加（流式、鉴权、多收件人）。Opt-in 把早期使用者放进来，同时让保守的人不受影响。

给已有实例打开 A2A：`clawcu clone <name> --name <name>-a2a` 再 `clawcu create ... --a2a` 克隆。当前没有原地升级通道；clone-first 工作流本来就是为这类"试一下再退回"准备的。

* * *
## 为什么用源码 sha 指纹

烤出来的镜像 tag 是 `clawcu/{service}-a2a:{base}-plugin{clawcu_version}.{sha10}`。10 位 sha 是在对应服务 `sidecar_plugin/<service>/` 子目录下所有文件的 SHA-256（排除 `__pycache__` / `.pyc` / `__init__.py` —— 打包元数据）。

- 发布安装（`pip install clawcu==0.3.0`）里 sha 是固定的。每台机器烤出来的 tag 一样。
- 可编辑安装（`pip install -e .`）下改了 `sidecar/server.js`，sha 就变。下次 `clawcu create --a2a` 烤新 tag，旧 tag 留在磁盘上但已无人引用。

没有这一层，可编辑安装会开心地复用改过 sidecar 之后的陈旧镜像 —— 然后你盯着幽灵行为调一小时才发现。指纹把这个口子封了。

* * *
## 兼容性

对所有不用 `--a2a` 的实例，`v0.3.0` 是 `v0.2.10` 的无痛升级。

- `v0.2.x` 已有受管实例继续跑，镜像 tag / 端口 / env 全部不动。
- `clawcu list` / `inspect` / `upgrade` / `rollback` / `clone` / `provider` 等表面一致。
- `InstanceSpec` / `InstanceRecord` 新增布尔字段 `a2a_enabled`（默认 `False`），纯加；旧记录通过 `from_dict` 默认值正常加载。
- `.clawcu-instance.json` 也会带上 `a2a_enabled`；`v0.3.0` 之前缺字段的 datadir 在 `recreate` 时按 `False` 处理。
- `list --json` 每个实例多一个 `a2a_enabled` key。纯加；现有消费者不受影响。

不破坏任何东西，加了一个 flag、一个字段、一个子命令树。

* * *
## 推荐工作流

已有实例的用户：

- **先在 clone 上试 A2A。** `clawcu clone writer --name writer-a2a` → `clawcu remove writer-a2a` → `clawcu create openclaw --name writer-a2a --version <v> --a2a`。bake 只发生一次，后续启动就是 `docker start`。
- **多 agent 一键启动。** `clawcu a2a up` 会探测每个运行中实例的插件端，给没有插件的启动 echo bridge，最后前台跑聚合注册中心。一条命令。
- **发消息。** `clawcu a2a send --to analyst --message "summarize yesterday"` 经注册中心路由。

其他用户：

- 不加 `--a2a`，`v0.3.0` 什么也不花你。

* * *
## 结语

`v0.2.x` 交付了扎实的单 agent 生命周期 —— pull / create / upgrade / rollback / clone / snapshot / 孤儿恢复。`v0.3.0` 是单 agent 走出去的第一步：N 个 agent 在一台机器上互相发现与发消息的最小协议表面，以 sidecar 形式烤在旁边，原生服务内部一丝不动。

下一步 `v0.3.x`：沿用路线图里的统一 `--output {table|json|yaml}` 协议、经 `.clawcu-instance.json` 的 provider bundle 溯源、以及把 active-provider 升为一等字段。A2A v0 预计在 `v0.4.x` 长出流式和鉴权。
