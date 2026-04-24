# A2A 使用场景示例

A2A（Agent-to-Agent）的核心设计是：**让每个实例暴露一张身份卡片（AgentCard），通过一个注册中心（Registry）互相发现，然后直接发送消息**。

以下示例覆盖从快速上手到编程集成的常见场景。

---

## 场景一：快速开始 — 让两个 Agent 互相说话

创建两个带 A2A 的实例，然后一键启动整个联邦：

```bash
# 1. 创建两个启用了 A2A 的实例
clawcu create openclaw --name writer   --version 2026.4.12 --a2a --cpu 1 --memory 2g
clawcu create hermes   --name analyst  --version 2026.4.13 --a2a --cpu 1 --memory 2g

# 2. 一键启动：探测 sidecar → 补齐 echo bridge → 启动 registry
clawcu a2a up

# 3. 从另一个终端发送消息
clawcu a2a send --to analyst --message "Summarize the key points of A2A protocol"
```

`a2a up` 会：
- 探测每个运行实例的 sidecar 端口
- 对没有 sidecar 的实例自动启动一个 echo bridge
- 在前台启动 Registry（默认 `:9100`）
- 按 `Ctrl+C` 一键停止所有服务

---

## 场景二：只启动 Registry（实例已有 sidecar）

如果实例镜像已经自带了 sidecar，不需要 echo bridge，可以只启动注册中心：

```bash
# 终端 1：启动 registry（聚合所有实例的 AgentCard）
clawcu a2a registry serve --port 9100

# 终端 2：查看注册中心里有哪些 agent
curl http://127.0.0.1:9100/agents | jq .

# 终端 2：查看某个 agent 的详细卡片
curl http://127.0.0.1:9100/agents/writer | jq .

# 终端 2：发送消息
clawcu a2a send --to writer --message "Write a short poem about Docker" --registry http://127.0.0.1:9100
```

---

## 场景三：查看本地实例的 AgentCard

不需要启动任何服务，直接查看当前管理实例的身份卡片：

```bash
# 查看所有实例的卡片
clawcu a2a card

# 只看某一个
clawcu a2a card --name writer

# 自定义 endpoint 里的 host（比如在局域网内共享）
clawcu a2a card --name analyst --host 192.168.1.100
```

输出示例：
```json
{
  "endpoint": "http://127.0.0.1:9130/a2a/send",
  "name": "analyst",
  "role": "Hermes local analyst",
  "skills": ["chat", "analysis"]
}
```

---

## 场景四：纯协议演示 — 没有真实实例也能玩

想快速体验 A2A 协议，但不想拉镜像、起容器。可以用 `bridge serve` 模拟一个虚拟 agent：

```bash
# 终端 1：启动一个纯协议的 echo bridge
clawcu a2a bridge serve \
  --instance demo-bot \
  --port 18080 \
  --role "Demo assistant" \
  --skills "chat,echo" \
  --endpoint http://127.0.0.1:18080/a2a/send

# 终端 2：直接 POST 消息（不经过 registry）
curl -X POST http://127.0.0.1:18080/a2a/send \
  -H "Content-Type: application/json" \
  -d '{"from":"user","to":"demo-bot","message":"hello"}'

# 查看它暴露的 AgentCard
curl http://127.0.0.1:18080/.well-known/agent-card.json | jq .
```

---

## 场景五：编程方式调用（Python）

在你的脚本或应用里直接用 `clawcu` 的 Python API：

```python
from clawcu.a2a.client import send_via_registry, list_agents, lookup_agent
from clawcu.a2a.card import AgentCard

# 1. 列出 registry 中所有 agent
cards = list_agents("http://127.0.0.1:9100")
for card in cards:
    print(f"- {card.name}: {card.role} @ {card.endpoint}")

# 2. 查找指定 agent
card = lookup_agent("http://127.0.0.1:9100", "writer")
print(card.to_json())

# 3. 发送消息（自动查 registry → 取 endpoint → POST）
reply = send_via_registry(
    registry_url="http://127.0.0.1:9100",
    sender="my-app",
    target="analyst",
    message="What is the capital of France?",
    send_timeout=30.0,
)
print(reply)
# {'from': 'analyst', 'reply': '[analyst] got: What is the capital of France?'}

# 4. 也可以跳过 registry，直接 POST 到已知 endpoint
from clawcu.a2a.client import post_message
reply = post_message(
    endpoint="http://127.0.0.1:18080/a2a/send",
    sender="my-app",
    target="demo-bot",
    message="ping",
)
```

---

## 场景六：给已有实例启用 A2A（重建）

如果实例创建时忘了加 `--a2a`，可以通过 `recreate` 重新构建为 A2A 版本（数据目录会保留）：

```bash
# 1. 移除容器（保留数据）
clawcu remove writer

# 2. 从残留的数据目录重建，并启用 A2A
clawcu recreate writer --a2a

# 3. 启动联邦
clawcu a2a up
```

---

## 场景七：多轮对话（带 thread_id）

A2A sidecar 支持通过 `thread_id` 维持跨调用的对话上下文。发送时带上即可：

```bash
curl -X POST http://127.0.0.1:18820/a2a/send \
  -H "Content-Type: application/json" \
  -d '{
    "from": "user",
    "to": "writer",
    "message": "Remember my favorite color is blue",
    "thread_id": "session-42"
  }'

# 后续消息使用同一个 thread_id，agent 能看到历史
curl -X POST http://127.0.0.1:18820/a2a/send \
  -H "Content-Type: application/json" \
  -d '{
    "from": "user",
    "to": "writer",
    "message": "What did I say my favorite color was?",
    "thread_id": "session-42"
  }'
```

> 注：`thread_id` 在 v0 协议中是可选的。Sidecar 会把对话历史以 JSONL 形式保存在数据目录的 `threads/` 下，因此 `recreate` 后历史仍然保留。

---

## 快速对照表

| 命令 | 作用 |
|------|------|
| `clawcu create ... --a2a` | 创建带 sidecar 的实例 |
| `clawcu a2a up` | 一键探测 + bridge + registry |
| `clawcu a2a registry serve` | 只启动注册中心 |
| `clawcu a2a bridge serve` | 为单个实例启动 echo bridge |
| `clawcu a2a card` | 查看 AgentCard |
| `clawcu a2a send --to X --message "..."` | 发送消息 |
| `clawcu inspect <name>` | 查看实例详情（含 A2A 配置段） |
