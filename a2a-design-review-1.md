# A2A 分支设计评审（review-1）

**评审范围**：`a2a` 分支相对 `main`（merge-base `ea93e8d`）的全部新增/变更，约 **67 个文件、+19 718 行**。
**评审维度**：模块划分、协议与数据契约、并发与生命周期、可观测性、安全、开发/运维体验、测试。
**评审方式**：通读 `src/clawcu/a2a/` 全部 Python 源码、两个 sidecar（Hermes `sidecar.py`、OpenClaw `server.js` 及拆分模块）、CLI、两个 Adapter 改动、`a2a-example.md` 使用场景。

本文只讨论**设计**层面——不讨论具体 bug，也不重复已在历次 `Review-N P?-?` 注释里自报的安全修复。

---

## 一、设计得好的地方

### 1. 控制面 / 数据面分离清晰
- `clawcu.a2a.*`（CLI、registry、client、card、bridge、builder、detect）是**控制面**：纯 stdlib，随 `clawcu` pip 包发布，host 侧运行。
- `clawcu/a2a/sidecar_plugin/<service>/` 是**数据面**：打包进镜像，container 内跑，只依赖 Node/Python 内置库。
- 二者通过 HTTP 协议（`/.well-known/agent-card.json` + `/a2a/send`）解耦，从源码一眼能分辨出职责。
- **好在哪**：host CLI 不需要 docker exec 就能做 registry 聚合和 card 探测；sidecar 不需要 `clawcu` 的任何 Python 运行时即可独立工作，连 `pip install clawcu` 失败都不影响已经装好镜像的实例。

### 2. "sidecar-plugin" 打包模型 + 指纹式镜像标签
`clawcu.a2a.sidecar_plugin.plugin_fingerprint` 用 `<clawcu_version>.<sha10>` 做 image tag（见 `builder.py:47`）：
- `clawcu_version` 人类可读，`sha10` 是 sidecar 源文件目录的 SHA-256 前 10 位。
- 忽略 `__pycache__`、`.pyc/.pyo`、`node_modules` 等运行期噪声（`sidecar_plugin/__init__.py:82-84`）。
- **好在哪**：editable install 场景下只要改了 sidecar 源文件，tag 就变，`A2AImageBuilder.ensure_image` 自动触发 rebake，解决了老方案里"改了 sidecar 但 docker 缓存没失效"的坑（注释 `review-5 P0-c` 已明示）。这是个非常工程化的决定。

### 3. AgentCard 是 frozen dataclass + 双向严校验
`card.py:40-73`：
- `from_dict` 校验 4 个必需字段、类型、非空；`skills` 要求全是非空字符串。
- 不变、可哈希、对测试友好。
- **好在哪**：协议的数据契约被集中表达，registry、bridge、CLI、sidecar 自己实现的 Python/Node 版都围绕这同一份 schema 转。

### 4. Registry 的"降级行为"区分 `running` vs `starting`
`registry.py:118-175`：
- `running` 实例探 card 失败 → 回退到 placeholder（可能是瞬时抖动，让 peer 自己重试）。
- `starting` 实例探 card 失败 → **直接跳过**，不 publish 一个注定 504 的 endpoint。
- **好在哪**：很少有人在 registry 层去想"publish 假 card 会害 peer"这种二阶影响，这里专门留了 `review-12 P2-D2` 的思考注释，可读性极高。

### 5. Container-advertise host vs Host-local host 的分离
- Sidecar 在 AgentCard 里填的是 `host.docker.internal`（macOS/Win 下 container 间可达）。
- CLI 通过 `localize_endpoint_for_host`（`client.py:32-59`）在 host 侧 send 前改写成 `127.0.0.1`。
- **好在哪**：registry 一份数据同时服务 container-peer 和 host-CLI 两类消费者，不需要双份 endpoint。`_CONTAINER_HOSTNAME_ALIASES` 只对已知别名改写，不误伤 LAN 部署。

### 6. Closure-based TTL 缓存替代模块级全局
`make_cards_provider`（`registry.py:188-216`）把 cards 缓存状态写进 closure 的 `state` dict，没有用模块级 global：
- 多 registry 共存（测试、dev-box）互不串扰。
- `ttl <= 0` 关缓存。
- **好在哪**：这是大多数人第一次写会踩的"模块 global 导致测试交叉污染"的问题，这里直接避免。

### 7. 网状安全防线
大量防御性设计且每条都有 review 迭代号背书：
- `_NoRedirectHandler`（registry + client 两处）拦 `302 Location: ftp://…` 逃逸。
- `_validate_outbound_url` 对 scheme 做 `http/https` 白名单。
- `_read_capped` + `A2A_MAX_RESPONSE_BYTES = 4 MiB` 防 OOM。
- `readJsonBody(… limit=64 KiB)` 防 body 炸内存。
- `A2A_INBOUND_REQUEST_TIMEOUT_S = 30s` 限 slowloris。
- `A2A_HOP_BUDGET = 8` 限网状回路。
- `X-A2A-Request-Id` 跨跳关联，`X-A2A-Hop` 递增计数——log 可 grep、行为可追踪。
- `PeerRateLimiter` 有 LRU 驱逐（`max_peers=1024`）防 rotating-name DoS。
- **好在哪**：每一条都在同一文件行内的 docstring 里说明了**为什么要加 / 不加会怎样被攻破**，这是"可审计的安全代码"该有的样子。

### 8. Gateway-agnostic sidecar
sidecar 自己不知道 gateway 的 readiness 路径（`/health` vs `/healthz`），由 Adapter 通过 `A2A_GATEWAY_READY_PATH` env 注入（`hermes/adapter.py:198`）：
- 一套 sidecar 代码复用在 OpenClaw（`/healthz`）和 Hermes（`/health`）。
- 对外暴露的健康端点又同时接受 `/health` 和 `/healthz`（`server.js:748-762`），监控工具不用关心 upstream 用哪种拼写。
- **好在哪**：协议、封装、复用都恰到好处。

### 9. CLI 一键体验
`clawcu a2a up`（`cli.py:227-303`）：probe 每个实例的 plugin → 没 plugin 的补 echo bridge → 启 registry → Ctrl-C 整套下线。对 demo / 新用户极度友好。
- `port_already_bound` 同时探 IPv4/IPv6（`detect.py:42-61`），避免 Docker-for-Mac 的 `*:PORT` 和 `127.0.0.1:PORT` 不同 family 遮蔽导致的静默绑在无效 socket 上。

### 10. 线程历史（thread_id）作为**可选**扩展
`ThreadStore`（Python 和 JS 两侧对等实现）：
- 协议核心保持无状态（peer 不发 `thread_id` → 行为完全不变）。
- 启用时 `_SAFE_ID = ^[A-Za-z0-9._\-]{1,128}$` 防路径穿越。
- JSONL 以 `<peer>/<thread_id>.jsonl` 分片，数据落在 `/opt/data` 挂载下，`clawcu recreate` 仍能保留。
- load-cap + file 全量——**策略**和**事实**分开。

### 11. 注释即文档
全仓几乎每一处非平凡决策都带 `# Review-N P?-? …` 的 docstring，说明触发这条代码的评审轮次、严重级、以及"不加会怎样"。这条分支是"代码里长出来的决策记录"的正面样本。

---

## 二、设计得不够好 / 值得质疑的地方

### 1. 两种语言的 sidecar 是最大的维护债务
现状：Hermes 用 Python，OpenClaw 用 Node（因为 OpenClaw gateway 本身是 Node）。结果是**同一套协议关切**要写两遍：
- hop budget、rate limit、readiness cache、thread store、outbound limiter、SSRF 白名单、redirect 拦截、response cap、request-id 生成、slowloris 超时……
- 代码注释里大量 "mirror of iter-17"、"hermes mirror of openclaw thread.js"——每次安全加固都要**人工同步**到另一侧。
- Node 侧已经拆成 `readiness.js / ratelimit.js / thread.js / outbound_limit.js / mcp.js / bootstrap.js` 6 个模块；Hermes 侧全塞在 **1910 行的 `sidecar.py`**（注释自辩："design-11 §4 规定单文件约定"）——这条约定本身值得重新审视。

**为什么不好**：每一次安全修复都是 2× 成本 + 漂移风险。`review-17/18 P1-I/J` 就是一个典型："SSRF 白名单先加在 Python，再加在 Node"——任何一次遗漏就是单边缺陷。

**怎么办更好**：
- 方案 A：把 sidecar 统一成 Python（Hermes 已是 Python；OpenClaw 虽为 Node，但 sidecar 是独立进程，完全可以 Python）。
- 方案 B：如果坚持双语言，至少把 Python 版也拆成 `readiness.py / ratelimit.py / thread.py / …`，与 Node 侧结构对等，让"同步"变成文件级 diff 而不是 1910 行文件内的手动定位。

### 2. AgentCard 的 schema 无版本、无前向兼容
`card.py:52-69`：`from_dict` 对 `name / role / skills / endpoint` 四字段做**硬性**校验，多一个或少一个字段都拒绝。
- 协议一旦演进（比如加 `pub_key` 或 `capabilities`），老客户端会直接 `ValueError`，而不是"忽略未知字段继续跑"。
- 没有 `schema_version` / `protocol_version` 字段。
- **怎么办更好**：`from_dict` 只校验必需字段、允许并忽略未知字段；同时给 AgentCard 加一个可选 `version: str = "0.1"` 字段；严格模式留给内部测试。

### 3. 服务特定常量泄漏到"协议"层
`card.py:10-36`：
```python
_SERVICE_SKILLS = {"openclaw": [...], "hermes": [...]}
_SERVICE_ROLES = {"openclaw": "...", "hermes": "..."}
_SERVICE_DEFAULT_DISPLAY_PORT = {"openclaw": 18819, "hermes": 9129}
_SERVICE_PLUGIN_PORT_OFFSETS = {"openclaw": (0, 1), "hermes": (0,)}
```
这 4 张表的每一条都是 service 知识（OpenClaw 的 gateway 占 display_port，sidecar 在 +1；Hermes sidecar 直占 display_port）。
- `display_port_for_record` 已经试图走 `service.adapter_for_record(record).display_port(...)`，但 fallback 又回到这个表——结果是"双份事实源"。
- 加第三种服务时，`card.py` 必须改动。
- **怎么办更好**：把 skills/roles/port-offsets 移成 Adapter 的属性（`adapter.default_skills()`、`adapter.plugin_port_offsets()`），card.py 全走 adapter 接口。fallback 表留作一个最朴素默认，不再按 service_name 分支。

### 4. 300 秒的默认 send timeout 对 CLI 过长
最新一条 commit `058fbb0` 把 `DEFAULT_SEND_TIMEOUT` 从 60s 提到 **300s**（`client.py:13`），`cli.py:333` 的 `--timeout` 也默认 300。
- 动机能理解：LLM 回复有时确实慢。
- 但 `clawcu a2a send` 是一个交互式 CLI 命令，没有任何 progress spinner。用户按下回车后最多**干等五分钟**才知道 peer 挂了。
- **怎么办更好**：保留后端/集成调用的 300s；CLI 层改为 60s 默认 + 文档里建议长任务 `--timeout 300`；或者加一个"每 10s 打一个点"的心跳提示。

### 5. Registry 无任何鉴权
`registry.py` 的 `make_registry_handler` 完全开放 `GET /agents` 和 `GET /agents/<name>`，无 token、无 mTLS。
- 默认绑 `127.0.0.1` 部分规避了问题，但：
  - docker-desktop 下 host 的 127.0.0.1 会被 container 的 `host.docker.internal` 直达——任意同机 container 能拿到整张 agent 清单。
  - `--host 0.0.0.0` 一旦开下去就彻底裸奔。
- **怎么办更好**：至少预留一个 `A2A_REGISTRY_TOKEN` 环境变量，绑定 `Authorization: Bearer …` 校验；默认可以 opt-out（兼容），但**有**这条开关。现在是连开关都没有。

### 6. `_fetch_card_at` 失败日志过于啰嗦
`registry.py:65-92`：任何失败（含 `ConnectionRefused`）都 `_log.info(...)`。
- `a2a up` 启动瞬间，如果宿主机上有 10 个已停实例，会直接 INFO 级刷 10 条"plugin card fetch failed"。
- `ConnectionRefused` / `timeout` 是**正常**探测负面结果，不应当和"bad schema"一样级别。
- **怎么办更好**：`ConnectionRefused` / `TimeoutError` → DEBUG；HTTP non-200、JSON 解析失败、schema 不合法 → INFO。

### 7. `lookup_timeout` 与 `send_timeout` 分离但 CLI 不暴露
`send_via_registry` 的两个 timeout 语义不同（`client.py:235-243`）：
- `lookup_timeout` 默认 5s（registry 查询）。
- `send_timeout` 默认 300s（实际 LLM 调用）。

但 CLI 的 `--timeout` 只映射到 `send_timeout`。用户若期望 `--timeout 10` 代表整个请求 10s，会得到一个 registry 仍然 5s 超时的偏差行为。
- **怎么办更好**：要么把两个 timeout 都吃掉（`min(user_timeout, default_lookup)` 不合理，直接分 `--lookup-timeout` / `--send-timeout`），要么 CLI 显式说明 `--timeout` 只影响 send。

### 8. `_resolve_bridge_card` 的宽泛 `except Exception`
`cli.py:202-205`：
```python
try:
    service = _get_service()
except Exception:  # noqa: BLE001 — clawcu may be uninitialised in demo mode
    service = None
```
- 会吞掉 `KeyboardInterrupt` 以外的一切，包括用户在 dev-box 配置错误（权限问题、yaml 格式错误）这类本该让用户看到的报错。
- **怎么办更好**：只抓已知会发生的异常，例如 `(FileNotFoundError, PermissionError)` 或 `ClawCUServiceError`（如有），而不是 bare `Exception`。

### 9. `_GATEWAY_READY_UNTIL` 是模块级全局
`hermes/sidecar.py:419`：`_GATEWAY_READY_UNTIL = 0.0` 是 module-global，`wait_for_gateway_ready` 和 `invalidate_gateway_ready_cache` 都操作它。
- 单进程 sidecar 下能跑，但单元测试里要 reset 必须改模块属性，略脏。
- 对比：注释里 `make_cards_provider` 专门避免了模块级 global（见优点 #6），这里又回去了，不一致。
- **怎么办更好**：包装成小型 `ReadinessCache` class，测试传入实例即可。

### 10. Host 模式的 OpenClaw sidecar 每次请求都 `docker exec cat`
`server.js:145-162` `makeHostAdapter`：读 `openclaw.json` 经由 `execFileSync("docker", ["exec", ..., "cat", path])`。
- `readGatewayAuth(adapter)` 是 `/a2a/send` 的必经路径（`server.js:818-823`）。
- 每条 A2A 消息 → 至少一次 `docker exec` 进程拉起，几十毫秒延迟。
- 虽然注释写了"host 模式只作为 one-off debugging"，但它仍然是代码路径，未来长尾是否会被滥用不可知。
- **怎么办更好**：对 token/auth-mode 做内存缓存 + 基于 mtime 的失效；或者干脆在 `main()` 启动时 snapshot 一次、记录到文件变更 watcher。

### 11. Bridge 的 "pure-protocol" 模式校验不对称
`_resolve_bridge_card`（`cli.py:114-168`）：
- 有 record → 允许任一 override 缺失。
- 无 record → 必须同时给 `--role / --skills / --endpoint`。

但 `_parse_skills("")` 会返回 `[]`（`cli.py:71-74`），传进去构造出 `AgentCard(name=..., role=..., skills=[], endpoint=...)`——这张 card 在 registry 那一侧 `AgentCard.from_dict` 里会因 `"skills must be non-empty"` 被拒（`card.py:58-59`）。
- 也就是 CLI 能构造一张**自己合法但 registry 不接受**的 card。
- **怎么办更好**：`_resolve_bridge_card` 应该在 no-record 分支显式要求 `len(skills_override) >= 1` 并给出具体的错误提示，而不是让用户启起来后才在 list-agents 时发现空。

### 12. `CLAWCU_A2A_HOST_HOSTNAME` 环境变量未做任何格式校验
`client.py:25-29`：读入即用、仅 `.strip()`。
- 用户若设成 `http://evil/` 或 `127.0.0.1:8080/path`，`localize_endpoint_for_host` 会把它硬塞进 netloc，生成 `http://http://evil/:9100/a2a/send` 这种畸形 URL。
- **怎么办更好**：用 `ipaddress.ip_address(...)` + 简单 hostname 正则校验；非法时 log 一条 WARNING 并 fallback 到 `127.0.0.1`。

### 13. `port_already_bound` 串行探测
`detect.py:42-61`：IPv4 / IPv6 两个 family 串行连接，100ms timeout。
- 实际 `a2a up` 里对每个实例都调一次，若 10 个实例 → 最多 2s 额外延迟。
- 不致命，但 UX 上能感觉到。
- **怎么办更好**：两个 family 并发探；或者用 `socket.getaddrinfo` 拿到列表后循环非阻塞 connect。

### 14. Docker tag 构造的字符安全靠黑名单
`builder.py:40`：`_TAG_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")`。
- 够用，但 docker 官方 tag 规范还有"不能以 `.` 或 `-` 开头"等细节，这里只做了 `.strip("-.")`。
- `clawcu_version=".rc1"` 这类极端输入仍能勉强通过，最终 tag 里残留 `.rc1`——不致命但依赖了 `str.strip` 的语义。
- **怎么办更好**：切到 docker 官方 tag 规范的 allow-list 正则（`[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}`），不符合直接抛异常而非 silent-fix。

### 15. 缺少"协议版本"握手
`/a2a/send` 的 body 是 `{from, to, message, thread_id?}`，response 是 `{from, reply, thread_id?, request_id}`。没有任何地方报"我说的是 v0.1"。
- 新增字段（如 `thread_id`）靠 peer 看不看得见决定行为——扩展性可以，但到协议 v0.2 若要做**不兼容**变更时无从区分。
- **怎么办更好**：AgentCard 里加 `protocol: ["a2a/v0.1"]` 字段；或者 sidecar 对 `/a2a/send` 加 `X-A2A-Version` 响应头。成本低、收益在未来。

---

## 三、没有明显好坏但值得留意的点

1. **Registry 是 in-process thread-pool 模型**（`ThreadingHTTPServer`）。对于 ≤100 实例的本地开发足够，对云上联邦（成千上万 agent）会撑不住。当前定位是本地，没问题；若将来路线图里有"跨机房 registry"一定会要重写。
2. **`A2A_THREAD_DIR` 落在 `/opt/data`**——也就是和用户业务数据混在一起。对用户而言"清 thread"变成"在 datadir 里找目录删"；没有 `clawcu a2a thread gc` 这类运维命令。短期不必做，长期会积灰。
3. **Echo bridge 默认 port = display_port + 1000**（`card.py:84-88`）。挑 +1000 偏移没有 docker publish 冲突检测；高端口段如果有别的业务占用会 fail。现在靠 `port_already_bound` 防住，但默认偏移量是约定不是共识。

---

## 四、优先级建议

若要做第二轮迭代，按"ROI 从高到低"：

1. **把 Hermes sidecar 拆成和 Node 侧对等的多模块**——最大的长期维护减负。（§1）
2. **AgentCard 允许未知字段 / 加 `protocol` 版本**——现在改很便宜，等 v1 再改就要兼容两代。（§2、§15）
3. **把 service 特定常量挪到 Adapter**——让 `card.py` 重新变成"协议层"。（§3）
4. **Registry 加可选 token**——零开销 opt-in，防未来 0.0.0.0 误配置。（§5）
5. **CLI `--timeout` 语义澄清 / 降默认**——影响可感知的 UX。（§4、§7）
6. **日志级别收敛**——`ConnectionRefused` 降 DEBUG。（§6）

其它条目都是 polish，可以滚进日常 review。

---

**评审人**：Claude Code
**评审日期**：2026-04-23
**覆盖面**：`src/clawcu/a2a/` 全部 + 两个 sidecar + adapter 改动
**未覆盖**：`tests/sidecar_*.test.js`、`tests/test_a2a.py`（~5 874 行）——测试代码本身的覆盖率 / 结构质量留作下一轮。
