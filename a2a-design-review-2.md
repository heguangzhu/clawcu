# A2A 分支设计评审（review-2）

**评审范围**：`a2a` 分支相对 `main`（merge-base `ea93e8d`）的全部新增/变更，约 **85 个文件、+21 813 / -76 行**，相对 review-1（67 文件、+19 718 行）又往前走了约 **30 个 refactor commit**。
**评审维度**：模块划分、sidecar 双胞胎对等性、协议契约、共享层（`_common/`）边界、测试表面、开发/运维体验。
**评审方式**：通读 `src/clawcu/a2a/` 控制面全部 Python（5 个模块，1196 行）、两个 sidecar（`hermes/sidecar/` 6 模块 1406 行、`openclaw/sidecar/` 7 模块 1582 行、`_common/` 13 模块 2174 行），对比 review-1 留下的 15 条"值得质疑"条目。

本文延续 review-1 的口径——只谈**设计**，不重复已在 `Review-N P?-?` 注释里自报的 bug/加固；并显式追踪 review-1 每条"值得质疑"项的当前状态。

---

## 一、设计得好的地方（相对 review-1 新增或已解决的部分）

### 1. review-1 §1 已解决：Node→Python 端口 + `_common/` 抽取
review-1 最大的一条债务是 "OpenClaw Node sidecar vs Hermes Python sidecar，同一套协议关切写两遍、1910 行单文件"。本轮：
- OpenClaw sidecar 完整 Python 化（`openclaw/sidecar/*.py`，7 模块 1582 行）。
- 双胞胎共享逻辑抽进 `sidecar_plugin/_common/`：`bootstrap / http_response / inbound_limits / mcp / outbound_limit / payload / peer_cache / protocol / ratelimit / readiness / streams / thread` 共 **13 个模块、约 2174 行**。
- 每一条安全加固——hop budget、SSRF、redirect 拦截、rate limit、request-id、response cap、MCP parse envelope——都从"Node 侧 + Python 侧两遍"收敛成 `_common/` 单一源 + 两份 `from _common.X import …` 导入。
- **好在哪**：review-1 §1 的 "2× 成本 + 漂移风险" 被结构性消除；后续任何新加固只改一个地方。

### 2. 控制面 / 数据面的协议表面进一步收窄到 `_common/protocol.py`
`_common/protocol.py`（273 行）集中了三组不变量：
- `REQUEST_ID_HEADER` / `read_or_mint_request_id` / `read_hop_header` / `hop_budget_from_env` 这些"读写 A2A 控制头"的函数。
- `hop_prelude`：每个入站处理器开头都要跑的 hop-budget 校验，现在是一行 `from _common.protocol import hop_prelude`。
- `write_error_envelope / write_send_reply_response / write_outbound_reply_response`：三种 `/a2a/*` 成功/失败响应体都走同一 writer，再不会出现"一侧少一个 `request_id` 字段"的漂移。
- **好在哪**：A2A 线格式原本散在两个 sidecar 的 do_POST 体里，现在搬到一个可以被单独 unit test 的纯函数集合。

### 3. review-1 §3 已解决：服务特定常量移出 `card.py`
review-1 指出 `_SERVICE_SKILLS / _SERVICE_ROLES / _SERVICE_DEFAULT_DISPLAY_PORT / _SERVICE_PLUGIN_PORT_OFFSETS` 四张表让"协议层"依赖 service 知识。当前 `card.py` 已回到 217 行的协议 schema 文件，service 相关的 skills/roles/ports 走 `service.adapter_for_record(record)` 的属性查询。`card.py` 现在只关心 AgentCard 的四字段契约——干净了一大圈。

### 4. review-1 §6 已解决：`_fetch_card_at` 的日志级别收敛
`ConnectionRefused` / `TimeoutError` 现已降为 DEBUG，仅 HTTP non-2xx / JSON 解析失败 / schema 不合法 保留 INFO。`a2a up` 在有多个停机实例时不再刷 INFO。

### 5. review-1 §7 已解决：CLI 分出 `--lookup-timeout` / `--send-timeout`
`send_via_registry` 的两个 timeout 现已分别暴露：CLI `--lookup-timeout`（默认 5s）对 registry，`--send-timeout`（即 `--timeout`）对 peer 回复。文档/help 文案明示语义。

### 6. `ReadinessCache` + `SweepTimer` 从模块 global 升级为可注入对象
review-1 §9 指出 `_GATEWAY_READY_UNTIL` 是 module-global，与"优点 6 用 closure 避 global"自相矛盾。当前两侧都已切到 `_common/readiness.py` 的 `ReadinessCache`（TTL cache state 绑对象实例）、`_common/outbound_limit.py::create_sweep_timer`（扫表器对 `handler_class` 关闭）。单元测试可直接 `cache = ReadinessCache(...)`，无需触碰模块属性。

### 7. Re-export shim 作为"测试表面兼容"的显式模式
`hermes/sidecar/inbound_limits.py` 是一个 **19 行**的 re-export shim：实现搬到 `_common/inbound_limits.py`，但测试里有 `mod._parse_content_length` / `mod._BadContentLength` / `mod.mcp_prelude` 这种属性探针，shim 让这些路径保持可寻址。
- **好在哪**：把"物理搬迁"和"公共 API 保持"分成两件事、两处证据，避免了"测试随着 refactor 一轮轮跟着改名"。每个 shim 顶部都点出"kept for any caller that still reaches for `…` by name"。

### 8. `mcp_prelude` / `hop_prelude` 的平行命名
两个 sidecar 的 `_handle_mcp` / `_handle_a2a_send` 入口现在都是一行 "*_prelude(self, ...)" 开头：
- `hop_prelude`（`_common/protocol.py`）：mint request-id、读 hop 头、hop ≥ budget → 508。
- `mcp_prelude`（`_common/inbound_limits.py`）：mint request-id、组装 rid-headers、读 JSON-RPC body、写 parse-error 信封。
- **好在哪**：handler 开头不再是 4-8 行 boilerplate；"prelude" 作为一个显式的抽象概念出现在协议模块里，读代码一眼知道"这一行把所有入站不变量做完了"。

### 9. `UpstreamError` 信封集中在 `_common/mcp.py`
两侧 MCP 上游错误现在都走 `write_upstream_error_response`；`OutboundError` 继承 `UpstreamError`，信封字段 `{code, message}` 走同一 writer。这是 review-1 §15 "协议版本握手"方向上的间接收益：错误码空间有了单一源，未来加 `X-A2A-Version` 只要动一处。

### 10. 注释里显式标记"shared between …"
`_common/*.py` 的 docstring 几乎都会写一句 "Previously lived under `hermes/sidecar/X.py`; moved into `_common` so OpenClaw can share the same …"。让 refactor 之前/之后的归属变成 code-searchable 的事实，而不是提交历史里翻。

---

## 二、设计得不够好 / 值得质疑的地方

### 1. Handler-factory 参数对称性：`ctx: Dict[str, Any]` vs `Config` 数据类
`openclaw/sidecar/server.py:261` 的工厂是：
```python
def _make_handler_class(ctx: Dict[str, Any]):
    logger = ctx["logger"]
    self_name = ctx["self_name"]
    card = ctx["card"]
    adapter = ctx["adapter"]
    # ... 共 20+ 个 ctx["..."] 解包
```
而 `hermes/sidecar/server.py:423` 是：
```python
def build_handler(cfg: Config, *, thread_store, rate_limiter, ...):
    ...
```
- Hermes 侧的 `Config`（`hermes/sidecar/config.py`，120 行）是结构化对象，字段有类型、有默认、有 env 解析聚合。
- OpenClaw 侧用字符串-key 的 dict，拼错 `"gateway_host"` 为 `"gateway_hos"` 在运行时才炸。
- 两侧最终 closure 里一样闭包掉 20 个变量，但 OpenClaw 侧丢掉了类型信息与 IDE 支持。
- **怎么办更好**：给 OpenClaw 也建一个 `openclaw/sidecar/config.py::Context`（`@dataclass`），`main()` 构造、传给工厂；handler 内部 `ctx.gateway_host` 代替 `ctx["gateway_host"]`。与 Hermes 形式对等后，两侧的 `main()` 甚至可以共享一部分 env 解析。

### 2. 超时单位的水土不服：openclaw 毫秒、hermes 秒
- `openclaw/sidecar/server.py`：`gateway_ready_deadline_ms`、`request_timeout_ms` 全 ms。
- `hermes/sidecar/config.py`：`timeout`、`ready_deadline`、`ready_probe_timeout`、`ready_poll_interval` 全秒（float）。
- 两套 env 名字同样不对齐：OpenClaw 读 `A2A_REQUEST_TIMEOUT_MS`，Hermes 读 `A2A_TIMEOUT_SECONDS`。
- 运维要排查一个"peer 接收慢"问题，看到 CLI 说 30s timeout、进 sidecar 需要乘/除 1000、再判 peer 是哪个——认知负担白给。
- **怎么办更好**：统一改浮点秒（Python 最自然）；env 名字统一为 `A2A_*_SECONDS`；保留两套命名作为 deprecated alias 一个 release 后移除。

### 3. SSRF 政策不对称：hermes 有白名单、openclaw 没有
- `hermes` 侧 `/a2a/outbound` 有 URL scheme 白名单 + redirect 拦截。
- `openclaw` 侧同名端点信任容器内 peer，不做 URL 校验。
- 注释解释是"容器内互信"，但两侧 sidecar 都能被 host 网络可达（127.0.0.1 + display_port + 1），威胁模型并不对称。
- **怎么办更好**：把 `_common/outbound_limit` 里顺手加一个 `validate_outbound_url(url)`，两侧同用；openclaw 的"信任 in-container peer"改为默认**也**开白名单，需要关的场景用 env `A2A_OUTBOUND_URL_ALLOWLIST=*` 显式 opt-out。威胁模型写在 `_common/outbound_limit.py` 顶部 docstring。

### 4. `/a2a/send` 入站字段校验的线格式分叉
- `hermes`：容忍 body 缺 `from`（老脚本互通用），回落到 `"unknown"`。
- `openclaw`：严格要求 `from` 非空字符串，缺失 → 400 + BadPayload。
- 两侧其它字段（`to`、`message`、`thread_id`）校验一致，只有 `from` 分叉。
- 这是 **wire-level 差异**，不是内部实现差异：同一条 peer 消息发到 `hermes` 成功、发到 `openclaw` 被拒——协议文档里不写则未来无人能解释为什么。
- **怎么办更好**：要么两侧都宽容（`_common/payload.py` 加 `parse_optional_sender`），要么两侧都严格；挑一个，其它 sidecar 跟上；`a2a-design-N.md` 文档里写明 `from` 的必需性。

### 5. Review-1 §4 未解决：300s 默认 CLI send timeout 仍在
`client.py:13` 的 `DEFAULT_SEND_TIMEOUT = 300`、`cli.py:333` 的 `--timeout` 默认 300 都没动。review-1 建议 CLI 层降回 60s + 文档说明——本轮未 touch。交互式 CLI 等 5 分钟的体验没变。

### 6. Review-1 §5 未解决：Registry 仍无任何鉴权
`registry.py::make_registry_handler` 仍是 open endpoint。review-1 建议加 `A2A_REGISTRY_TOKEN` opt-in，本轮未动。`--host 0.0.0.0` 下 agent 清单仍裸奔。

### 7. Review-1 §10 未解决：`docker exec cat openclaw.json` 每请求一次
`openclaw/sidecar/adapters.py::HostAdapter.read_gateway_auth` 仍走 `execFileSync("docker", ["exec", ..., "cat", path])`，每条 `/a2a/send` 都拉起一次子进程（host 模式）。review-1 建议加 mtime-based cache，本轮未动。

### 8. Test-surface coupling 迫使 server.py 保留 dead 导入
`hermes/sidecar/server.py` / `openclaw/sidecar/server.py` 顶部都 import 了若干函数（`read_or_mint_request_id`、`read_hop_header`、`REQUEST_ID_HEADER`、`hop_budget_from_env`），尽管 handler body 里已完全不使用它们——测试用例通过 `server.read_or_mint_request_id(...)` / `server.read_hop_header(...)` 的**模块属性访问**去验证协议行为。
- 结果：无法 linter-clean，`# noqa: F401` 或"导入后立即用一次"成为永久性 hack。
- 本质原因是 test 层绕过了 `_common/` 直接测 `server` 模块的符号表面。
- **怎么办更好**：在 `_common/protocol.py` 里暴露一份稳定的 "test helpers" 名字（比如 `__all__`），测试改成 `from sidecar_plugin._common.protocol import read_or_mint_request_id`；`server.py` 只留真正运行时用到的 import。或者：给两侧 `server.py` 加一个 `_test_exports.py` sibling，显式写"这些是测试用的 re-export 面"，让 dead-looking import 的动机 self-documenting。

### 9. `hermes/sidecar/server.py` 仍 877 行，明显厚于 `openclaw/sidecar/server.py` 的 755 行
拆模块后 hermes 还有 ~120 行的厚度差。扫一遍差异集中在：
- `_handle_a2a_send` / `_handle_a2a_outbound` 的 body 较长（SSRF 检查 + registry_url 覆盖 + peer cache 命中三个分支互相穿插）。
- MCP tool list 的静态 vs 动态分支判定散在 `_handle_mcp` 内。
- **怎么办更好**：把 "outbound 路径" 抽成 `hermes/sidecar/outbound_route.py` 或合并到 `_common/outbound_limit.py`（openclaw 侧已经把类似逻辑集中在 `outbound.py`）；MCP tool list 分支 fold 进 `_common/mcp.py` 的 `handle_mcp_request`，让 sidecar 的 `/mcp` handler 真正只剩 `return handle_mcp_request(payload, ...)`。

### 10. `_common/mcp.py` 已 **451 行**——最大的一个共享模块
`_common/mcp.py` 同时承担：JSON-RPC 信封编解码（`json_rpc_*`）、`UpstreamError` 类层次、tool list 构造、tool desc role 注入 env 解析、`handle_mcp_request` 总调度。
- 已经比任何单独的 sidecar handler 都大。
- 继续在这个文件里加上游错误类型/新方法，会把 review-1 §1 的"1910 行单文件"债务**搬进 `_common/`**——只是换了地址的同一个问题。
- **怎么办更好**：拆成 `_common/mcp/envelope.py`（JSON-RPC shape）、`_common/mcp/tool_list.py`（工具目录）、`_common/mcp/upstream.py`（UpstreamError + OutboundError）、`_common/mcp/__init__.py` re-export 兼容当前 import 路径。体积上目测能降到单文件 ≤150 行。

### 11. `_common/` 是扁平的 13 模块目录
当前结构：
```
_common/
  bootstrap.py        (257)
  http_response.py     (41)
  inbound_limits.py   (241)
  mcp.py              (451)
  outbound_limit.py   (257)
  payload.py           (85)
  peer_cache.py       (172)
  protocol.py         (273)
  ratelimit.py        (127)
  readiness.py         (53)
  streams.py           (57)
  thread.py           (153)
```
- 文件名层面能一眼看出主题，但没有"请求入栈路径"与"请求出栈路径"的分组：`inbound_limits + protocol + payload + ratelimit` 都是入向的，`outbound_limit + peer_cache + streams` 都是出向的，`mcp + bootstrap + readiness + thread` 是维度正交的服务能力。
- 13 个平级文件再长几个，IDE 侧栏会难以速读。
- **怎么办更好**：分 `_common/inbound/`（limits、payload、protocol headers 子集）、`_common/outbound/`（limit、peer_cache、streams）、`_common/mcp/`（见 §10）、`_common/runtime/`（bootstrap、readiness、ratelimit、thread）；每个子包 `__init__.py` re-export 关键符号，维持当前 `from _common.foo import X` 不破坏。

### 12. `_common` 的 `sys.path` 注入还是三段式脆代码
两侧 `server.py` 都有一段"walk up 4 层找 `_common/`"的探测循环：
```python
_probe = _THIS_DIR
for _ in range(4):
    if os.path.isdir(os.path.join(_probe, "_common")):
        if _probe not in sys.path: sys.path.insert(0, _probe)
        break
    ...
```
- 硬编码的 `range(4)` 是根据 `clawcu/a2a/sidecar_plugin/{openclaw,hermes}/sidecar/server.py` 到 `sidecar_plugin/` 的目录层数定的。
- 目录改一层就炸。
- 注释里解释了动机（baked image 和 source tree 两种布局），但没显式断言错误。
- **怎么办更好**：把这段 bootstrap 移到 `_common/__init__.py` 的 `find_and_inject()` 里用一次 import；找不到就显式 `raise RuntimeError("_common not reachable from …")`——比"静默跳过然后下一行 `from _common.x import Y` 失败"的错误信号好得多。

### 13. `registry_url` 覆盖语义的 missing-vs-None 隐藏分叉
两侧 `/a2a/outbound` 在 body 里读 `registry_url` 的行为：
- `allow_client_registry_url=False` 时：显式给了 `registry_url` → 被拒（预期）。
- 但 body 里 `"registry_url": null`（JSON null，显式 None）的分支两侧都走到"用默认"——不是"用 None 作为显式不使用 registry"的意图。
- 普通 Python dict 里 `body.get("registry_url")` = None 对 "缺字段" 和 "字段值是 null" 同义。
- **怎么办更好**：用 sentinel（`_MISSING = object()`），或统一在 payload parser 里把 `null` 视作 "explicit empty" 再校验；行为写进 `_common/payload.py` 的 docstring。

### 14. Log-format 不一致
- `openclaw/sidecar/logsink.py` 默认 f-string + `[a2a/openclaw] ...` 前缀 + stderr。
- `hermes/sidecar/server.py` 走 stdlib `logging` 的 `%` 格式 + `_log = logging.getLogger("a2a.hermes")`.
- 两侧 log 格式**不能被同一个 regex/logfmt parser 吞**；`clawcu logs a2a` 类命令（如果以后有）得写两套解析器。
- **怎么办更好**：`_common` 加一个 `logsink.py`（OpenClaw 已有、只差搬），两侧都走 stdlib `logging` + `%` 格式 + `[a2a/<service>]` 前缀；`LogRecord.service` 字段统一。

### 15. 缺"env 变量词典"
当前 40+ 个 `A2A_*` / `HERMES_*` / `CLAWCU_*` env 变量散在三个地方：两个 sidecar 的 config.py / ctx 构造、Adapter 注入（`hermes/adapter.py`、`openclaw/adapter.py`）、control-plane 的 `_CLAWCU_A2A_HOST_HOSTNAME` 等。
- 没有一份"所有 A2A env、含义、默认、生效模块"的索引。
- 运维排错要跨三个 grep 面。
- **怎么办更好**：`_common/envs.py`（或 README 表格）集中登记 `Name / Default / Scope / Owner-module / Since-version`；`clawcu a2a env` 子命令 dump 出当前生效值。review-1 §15 "协议版本握手" 的相邻场景。

---

## 三、没有明显好坏但值得留意的点

1. **`_common/mcp.py` 451 行**：见 §10，是下一轮拆包的最大单点候选。短期单文件能跑，但已是 `_common/` 里最大的模块，再塞就是"用 _common 当新的 1910 行 sidecar.py"。
2. **`hermes/sidecar/inbound_limits.py` 是纯 19 行 shim**：优点（见 §7）与债务（需要长期维护、否则哪天删掉会炸一堆测试）共存。建议给这类 shim 统一加 `# TODO(post-N): drop after tests switch to _common.*` 注释并登记 sunset 版本号。
3. **`_common/` 不暴露 `__all__`**：当前全靠"具体 import 哪个就能用哪个"。如果未来把它打包成独立可分发包（`clawcu-a2a-common`），没有 `__all__` / 没有版本文件会是第一个阻挡。现在不必做，但值得记一笔。
4. **Review-1 §8 / §11 / §12 / §13 / §14** 本轮未被触及（都是 polish 类）。`port_already_bound` 串行探测、`_resolve_bridge_card` 的空 skills、`CLAWCU_A2A_HOST_HOSTNAME` 无格式校验、docker tag 黑名单——都维持 review-1 的结论，不重复。

---

## 四、优先级建议

若做 review-3 迭代，按 "ROI 从高到低"：

1. **统一 OpenClaw handler-factory 为结构化 `Context`**（§1）——让两侧 sidecar 在工厂层面真正形似，顺手把 ms/秒统一（§2）。影响面：一个新文件 + `server.py` 的 20 行 ctx 解包收敛成一次 field 访问。
2. **拆 `_common/mcp.py`**（§10）——现在 450 行只是难读，800 行就是新债务；越早拆代价越小。
3. **补齐 review-1 § 4 / § 5 / § 10 三条未解决项**——都是小 PR，但都是安全/UX 可感知项。
4. **对齐两侧 SSRF 政策 + `/a2a/send` `from` 必需性**（§3、§4）——wire-level 差异必须在 docs 里写清或在 code 里消除。
5. **引入 `_common/inbound/`、`_common/outbound/`、`_common/mcp/` 子包结构**（§11）——`_common/` 再长 3 个文件之前做，之后做 API 断面会更广。
6. **整理"env 词典" + log 格式对齐**（§14、§15）——一次性工作，长期运维收益。

其它条目（test-surface coupling、registry_url missing-vs-None、shim 的 sunset 标注）建议滚进日常 review。

---

**评审人**：Claude Code
**评审日期**：2026-04-24
**覆盖面**：`src/clawcu/a2a/` 控制面 + `sidecar_plugin/_common/` 13 模块 + `openclaw/sidecar/` 7 模块 + `hermes/sidecar/` 6 模块 + 两个 Adapter
**未覆盖**：`tests/test_a2a*.py` 测试层本身（仍留作独立评审）；review-1 已覆盖且本轮未改动的 `builder.py / detect.py / bridge.py` 等控制面组件。
**与 review-1 的 delta**：review-1 §1/§3/§6/§7/§9 已结案；§4/§5/§10 仍未解决；本轮新增 15 条观察，核心主线是"双 sidecar 已形神对等，但 handler-factory 参数 / 单位 / SSRF 政策 / 日志格式四个接缝还有可见缝隙"。
