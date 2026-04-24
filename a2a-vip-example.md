# A2A VIP 场景：Agent 协作调用链

> 典型场景：用户与 **agentA** 对话，agentA 需要向 **agentB** 请求某个数据，agentB 处理完成后返回给 agentA，agentA 整合后再返回给用户。
>
> 这是 A2A 设计的核心目标场景，架构上完全支持。

---

## 场景架构

```
┌─────────────┐      ┌──────────────────────────────────────┐
│   用户      │      │           agentA 容器                │
│  (TUI/API)  │─────▶│  ┌─────────────┐  ┌─────────────┐  │
└─────────────┘      │  │   Gateway   │  │  A2A Sidecar│  │
      ▲              │  │ (OpenClaw/  │  │  (MCP +     │  │
      │              │  │  Hermes)    │  │  转发)      │  │
      │              │  └──────┬──────┘  └──────┬──────┘  │
      │              │         │                │         │
      │              │         │  LLM 决定调用  │         │
      │              │         │  a2a_call_peer │         │
      │              │         │───────────────▶│         │
      │              │         │                │         │
      │              │         │◀───────────────│         │
      │              │         │   tool result  │         │
      │              │         │   (agentB reply)         │
      │              │         │                │         │
      │              └─────────┼────────────────┘         │
      │                        │                          │
      │              ┌─────────┼────────────────┐         │
      │              │         ▼                │         │
      │              │  ┌──────────────────────────────────────────┐
      │              │  │           agentB 容器                    │
      │              │  │  ┌─────────────┐  ┌─────────────┐       │
      │              └──│  │   Gateway   │◀ │  A2A Sidecar│       │
      │                 │  │ (OpenClaw/  │  │  (/a2a/send)│       │
      │                 │  │  Hermes)    │  └─────────────┘       │
      │                 │  └──────┬──────┘                        │
      │                 │         │                               │
      │                 │         │ LLM 处理消息                   │
      │                 │         │ 生成 reply                    │
      │                 │         │                               │
      │                 └─────────┼───────────────────────────────┘
      │                           │
      └───────────────────────────┘
               agentA 整合后返回给用户
```

---

## 调用链详解

| 步骤 | 动作 | 负责组件 |
|------|------|---------|
| 1 | 用户发送消息给 agentA | TUI / API |
| 2 | agentA Gateway 接收消息，进入 LLM agent 循环 | OpenClaw / Hermes |
| 3 | agentA 的 LLM 判断需要外部数据，决定调用 MCP 工具 `a2a_call_peer` | LLM + MCP |
| 4 | MCP 请求发到 agentA 的 sidecar (`POST /mcp`) | Sidecar |
| 5 | Sidecar 查 Registry 获取 agentB 的 AgentCard | Registry |
| 6 | Sidecar 向 agentB 发送 `POST /a2a/send` | Sidecar → Network |
| 7 | agentB 的 sidecar 收到消息，转发给 agentB Gateway | Sidecar |
| 8 | agentB 的 LLM 处理请求，生成回复 | LLM |
| 9 | 回复沿原路返回给 agentA 的 sidecar | Network |
| 10 | agentA 的 LLM 收到 tool result，整合生成最终回复 | LLM |
| 11 | 最终回复返回给用户 | Gateway |

---

## 代码层面的支撑

### 1. MCP 工具自动注入

Sidecar 启动时自动把 `mcp.servers.a2a` 写入服务配置，无需手动修改：

```javascript
// sidecar_plugin/openclaw/sidecar/bootstrap.js
function buildMcpUrl({ port }) {
  return `http://127.0.0.1:${port}/mcp`;
}
// 自动 merge mcp.servers.a2a = { url } 到 openclaw.json
```

### 2. `a2a_call_peer` 工具定义

工具描述会**动态拉取当前 Registry 中的 peer 列表**，让 LLM 知道能找谁：

```javascript
// sidecar_plugin/openclaw/sidecar/mcp.js
const BASE_DESCRIPTION =
  "Call another agent in the A2A federation and return its reply. " +
  "Use when the current task needs data or work owned by a different " +
  "agent (e.g., an analyst for market data, a writer for prose).";

// 动态追加可用 peer 列表
function formatPeerSummary(peers, selfName) {
  // Available peers:
  //   - analyst (chat, analysis)
  //   - writer (chat, tools)
}
```

### 3. 工具调用 → 消息转发

MCP `tools/call` 处理 `a2a_call_peer` 时，内部调用 `forwardToPeer`：

```javascript
// mcp.js handleToolsCall
peerResp = await deps.forwardToPeer({
  endpoint: card.endpoint,
  selfName: deps.selfName,
  peerName: args.to,
  message: args.message,
  threadId,
  hop: 1,              // 跳数计数
  timeoutMs: deps.timeoutMs,
});
```

### 4. Sidecar → Gateway 原生路由

收到 A2A 消息后，sidecar **不走裸 LLM**，而是走 gateway 的完整 agent 流水线：

```javascript
// server.js postChatCompletion
const payload = {
  model: model || "openclaw",
  stream: false,
  messages: [
    ...(systemPrompt ? [{ role: "system", content: systemPrompt }] : []),
    ...history,
    { role: "user", content: userMessage },
  ],
};
// POST /v1/chat/completions — 运行完整 persona + skills + tools
```

### 5. Hop Budget 防循环

```javascript
const A2A_HOP_BUDGET = Number(process.env.A2A_HOP_BUDGET || 8);
if (incomingHop >= A2A_HOP_BUDGET) {
  return jsonResponse(res, 508, {
    error: `hop budget exceeded (hop=${incomingHop}, budget=${A2A_HOP_BUDGET})`,
  });
}
```

### 6. Thread 上下文保持

多轮对话时，历史消息以 JSONL 保存在数据目录，支持跨 recreate 持久化：

```javascript
// thread.js
const threadStore = createThreadStore({
  storageDir: process.env.A2A_THREAD_DIR || "",
  maxHistoryPairs: 10,
});
// 加载历史 → 追加新回合
```

---

## 前提条件

### 1. LLM 必须支持 function calling / tool use

OpenClaw 和 Hermes 都支持 MCP 工具，但底层 provider 需要原生支持 function calling：
- ✅ OpenAI GPT-4 / GPT-4o
- ✅ Claude (Anthropic)
- ⚠️ 部分国产模型支持程度不一

### 2. Prompt 需要引导 LLM 使用 peer

Sidecar 会自动把 peer 列表注入工具描述，但 LLM 是否**主动决定调用**取决于 prompt 引导。建议在 persona 中明确说明：

**OpenClaw** (`<datadir>/workspace/IDENTITY.md`):
```markdown
You are a coordinator agent. When you need market analysis or data
that you don't have, you MUST call the 'analyst' peer via the
a2a_call_peer tool. Do not make up data.
```

**Hermes** (`<datadir>/SOUL.md`):
```markdown
你是协调员 Agent。当你需要市场分析或没有的数据时，
必须通过 a2a_call_peer 工具调用 'analyst' peer。不要编造数据。
```

### 3. 正确配置 Provider

agentA 和 agentB 都需要配置可用的 LLM provider（API Key），否则 LLM 无法运行：

```bash
clawcu provider collect --all
clawcu provider apply openclaw:openai coordinator --agent main --primary openai/gpt-4o
clawcu provider apply hermes:openrouter analyst --primary openrouter/anthropic/claude-sonnet-4
```

---

## 完整操作示例

### Step 1: 创建两个启用了 A2A 的实例

```bash
# coordinator (OpenClaw) — 负责与用户交互和调度
clawcu create openclaw \
  --name coordinator \
  --version 2026.4.12 \
  --a2a \
  --cpu 1 \
  --memory 2g

# analyst (Hermes) — 负责数据分析
clawcu create hermes \
  --name analyst \
  --version 2026.4.13 \
  --a2a \
  --cpu 1 \
  --memory 2g
```

### Step 2: 配置 Provider（两边都需要）

```bash
# 收集本地已有的 provider 配置
clawcu provider collect --all
clawcu provider list

# 应用到 coordinator
clawcu provider apply openclaw:openai coordinator \
  --agent main \
  --primary openai/gpt-4o

# 应用到 analyst
clawcu provider apply hermes:openrouter analyst \
  --primary openrouter/anthropic/claude-sonnet-4
```

### Step 3: 配置 Persona（引导使用 peer）

```bash
# OpenClaw: 编辑 workspace/IDENTITY.md
cat >> ~/.clawcu/coordinator/workspace/IDENTITY.md << 'EOF'

## A2A Federation Rules

You have access to an A2A federation with the following peers:
- analyst: market analysis, data processing, research

When the user asks for analysis, market data, or research that you
cannot perform yourself, you MUST use the a2a_call_peer tool to ask
the analyst. Summarize its reply for the user.
EOF

# Hermes: 编辑 SOUL.md
cat >> ~/.clawcu/analyst/SOUL.md << 'EOF'

You are the analyst agent. You specialize in data analysis, market
research, and detailed factual answers. When called by another agent
via A2A, provide concise, accurate, well-structured replies.
EOF
```

### Step 4: 启动 A2A 联邦

```bash
clawcu a2a up
```

输出示例：
```
OK coordinator (plugin-backed on :18800)
OK analyst (plugin-backed on :9130)
A2A registry listening on http://127.0.0.1:9100 (Ctrl+C to stop)
```

### Step 5: 与 coordinator 对话

```bash
# 进入 coordinator 的 TUI
clawcu tui coordinator
```

用户输入：
```
> 请帮我分析今天的科技股市场趋势
```

coordinator 的 LLM 判断自己无法直接获取实时股市数据 → 调用 `a2a_call_peer(to="analyst", message="分析今天科技股市场趋势")` → analyst 处理 → 返回结果 → coordinator 整合后回复用户。

---

## 验证方法

### 方法 1: 查看 sidecar 日志

```bash
# 查看 coordinator 的 sidecar 日志
cat ~/.clawcu/coordinator/logs/a2a-sidecar.log | grep "a2a.outbound"

# 预期输出：
# [sidecar:coordinator] a2a.outbound begin request_id=xxx to=analyst hop=1
# [sidecar:analyst]     a2a.send accepted request_id=xxx from=coordinator hop=1
# [sidecar:analyst]     a2a.send replied request_id=xxx from=coordinator
# [sidecar:coordinator] a2a.send replied request_id=xxx from=analyst
```

### 方法 2: 直接调用 MCP 工具测试

```bash
# 测试 coordinator 是否能正确调用 analyst
curl -X POST http://127.0.0.1:18800/a2a/send \
  -H "Content-Type: application/json" \
  -d '{
    "from": "test-user",
    "to": "coordinator",
    "message": "请让 analyst 总结一下 A2A 协议的核心设计"
  }' | jq .
```

### 方法 3: 查看 registry

```bash
curl http://127.0.0.1:9100/agents | jq .
```

---

## 当前限制

| 限制 | 说明 | 缓解方法 |
|------|------|---------|
| **同步阻塞** | A2A v0 是请求-响应模式，agentA 会阻塞等待 agentB 完全回复后才继续 | 设计对话时避免超长分析任务；必要时让 agentB 返回"分析中"占位符 |
| **默认超时 120s** | `A2A_TIMEOUT_SECONDS` 包含 LLM 生成时间 | 设置环境变量 `A2A_TIMEOUT_SECONDS=300` 延长超时 |
| **LLM 可能不调用工具** | 取决于 model 的 tool use 能力和 prompt 引导 | 在 IDENTITY.md/SOUL.md 中明确规则；使用更强的 model (GPT-4o/Claude) |
| **单 host 部署** | 当前 A2A 默认绑定 127.0.0.1，跨机器需要额外网络配置 | 使用 `--a2a-advertise-host` 指定可路由地址 |
| **无权限控制** | 任何知道 registry 的 peer 都可以调用任何 agent | A2A sidecar 端口默认只绑 127.0.0.1；生产环境需配合防火墙 |
| **无重试/队列** | 如果 agentB 暂时不可用，调用直接失败 | 由调用方的 LLM 决定是否重试；或在外层添加重试逻辑 |

---

## 进阶：多跳调用 (A → B → C)

理论上支持更长的调用链，hop budget 默认为 8：

```
用户 → coordinator → analyst → writer → 返回
```

但实践中不建议超过 2 跳，因为：
1. 累积延迟显著增加（每跳都有 LLM 生成时间）
2. 上下文传递的噪声累积
3. 单点失败概率上升

如需限制深度，可在创建时设置：

```bash
clawcu create openclaw --name coordinator --version 2026.4.12 \
  --a2a --a2a-hop-budget 3 \
  --cpu 1 --memory 2g
```

---

## 总结

**用户 ↔ agentA ↔ agentB → 用户** 这个场景：

- ✅ 架构上完全支持（MCP 工具 + Sidecar 转发 + Registry 发现）
- ✅ 代码已完整实现（OpenClaw 和 Hermes 双运行时）
- ⚠️ 实际效果取决于 **LLM 的 tool use 能力** 和 **prompt 引导**
- ⚠️ 当前是同步模式，不适合超长耗时任务
