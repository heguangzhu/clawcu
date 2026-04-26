# A2A 分支设计评审（review-3）

**评审范围**：`a2a` 分支相对 `main`（merge-base `ea93e8d`）的全部新增/变更，约 **67 个文件、+18 230 / -7 行、97 个 commit**。相对 review-2，新增 9 个 commit（其中 8 条是 review-2 §四 优先级项的实装，1 条是 env 词典文档）。
**评审维度**：模块划分、sidecar 双胞胎对等性、协议契约、共享层边界、测试表面、开发/运维体验。
**评审方式**：通读 `src/clawcu/a2a/` 控制面 5 模块 1196 行、`sidecar_plugin/_common/` 12 模块 1 子包（共 2326 行）、`openclaw/sidecar/` 8 模块 1736 行、`hermes/sidecar/` 6 模块 1392 行；对照 review-2 十五条"值得质疑"项当前状态。测试表面：793 passed。

本文延续 review-1 / review-2 的口径——只谈**设计**，不重复已在 `Review-N P?-?` 注释里自报的 bug/加固；显式追踪 review-2 每条"值得质疑"项的处理结果。

---

## 一、设计得好的地方（相对 review-2 新增）

### 1. Handler-factory 两侧形参对称（review-2 §1 已解决）
`openclaw/sidecar/context.py` 新增 `@dataclass Context`（46 行、12 字段）。`_make_handler_class(ctx: Context)` 把 20 行 `ctx["..."]` 字符串 key 解包改成 `ctx.logger` / `ctx.gateway_host` 的属性访问，拼错变成编译期错误。`main()` 一次性构造 `Context(logger=..., ...)` 取代 dict 字面量；hermes 侧 `build_handler(cfg: Config, ...)` 的结构化风格两侧对齐。`Context` 保留 `Any` 类型字段给 logger/adapter/rate_limiter/thread_store（protocol-shaped 运行对象），文件级 docstring 解释了"为什么不用 Protocol 类型"——让 refactor 的权衡在代码里可读。

### 2. 超时单位归一到秒（review-2 §2 已解决）
openclaw 新增 `_ms_from_seconds_env()` helper，`A2A_REQUEST_TIMEOUT_SECONDS` / `A2A_GATEWAY_READY_DEADLINE_S` 为首选 env，并读取旧的 `*_MS` 作为 deprecated alias（两者都给则 `_SECONDS` 胜出）。hermes 侧 `A2A_TIMEOUT_SECONDS` 未动。运维现在看到 "30 秒 timeout" 不需要再乘除 1000。docs/a2a-envs.md 登记了 deprecated alias 列表与迁移指引。

### 3. SSRF 白名单共享到 `_common/peer_cache.py`（review-2 §3 已解决）
`_common/peer_cache.py` 暴露 `validate_outbound_url()` + `BadOutboundUrl` + `_OUTBOUND_URL_ALLOWED_SCHEMES`（http/https only），两个 sidecar + openclaw 的 `http_client.py::parse_http_url` 都改用这一份。hermes `peering.py` 用 `# noqa: F401,E402` 重导出 leading-underscore 别名，保留了旧测试路径 `peering._BadOutboundUrl` / `peering._validate_outbound_url`。openclaw 侧默认**也**开 SSRF 校验——"容器内互信"的隐式假设消除。

### 4. `/a2a/send` `from` 字段两侧都必填（review-2 §4 已解决）
hermes 的老旧回落"缺字段 → from='unknown'"被移除，改用 `require_non_empty_string(payload, "from")`。协议契约现在 wire-level 对齐：同一条消息给任意 sidecar 都能做同一 400/200 判定。

### 5. CLI `--timeout` 默认降到 60s（review-2 §5 + review-1 §4 已解决）
`DEFAULT_CLI_SEND_TIMEOUT = 60.0` 取代老的 300s；`client.py` 内部的 `DEFAULT_SEND_TIMEOUT = 300` 仍存在但只对非 CLI 调用（脚本/程序化）生效。交互式 CLI 不再默认等 5 分钟。

### 6. Registry 可选 bearer token（review-2 §6 + review-1 §5 已解决）
`registry.py::_check_bearer` 用 `hmac.compare_digest` 做常量时间比较；`A2A_REGISTRY_TOKEN` opt-in：未设置 = 行为不变（无鉴权），设置 = 所有读请求必须带 `Authorization: Bearer <token>`。`client.py::_registry_token_from_env()` 让 CLI 自动带 token，运维不需要手动注入。

### 7. HostAdapter 子进程调用加 TTL cache（review-2 §7 + review-1 §10 已解决）
`openclaw/sidecar/adapters.py::HostAdapter` 新增 `_cached_exec()` + `invalidate_cache()`，`A2A_HOST_ADAPTER_TTL_S=60` 默认。`read_file("openclaw.json")` / `get_env("A2A_MODEL")` 不再每条 `/a2a/send` 拉起一次 `docker exec`。host-mode 下的 per-request 子进程成本被摊平到每分钟一次。

### 8. `_common/mcp.py` 拆为 `_common/mcp/` 子包（review-2 §10 已解决）
原 451 行单文件拆成四份：`envelope.py`（48 行，JSON-RPC shape + 错误码）、`upstream.py`（64 行，UpstreamError + 信封 writer）、`tool_desc.py`（145 行，工具目录 + env flag）、`dispatcher.py`（251 行，业务核心）。`__init__.py` re-export 所有旧公有名——外部 `from _common.mcp import ...` 零改动，单测与两个 sidecar 保持不变。`_common/inbound_limits.py` 只需要 `ERR_PARSE` + `json_rpc_error`，现在不用再把调度器 + outbound-key 路径拉进它的 import 图。**好在哪**：review-1 §1 的"1910 行单文件"债务没有变相搬进 `_common/` 的迹象。

### 9. openclaw sidecar 日志对齐 hermes 格式（review-2 §14 已解决）
两侧 sidecar 每一行输出现在都形如 `<ISO-UTC> <LEVEL> a2a-sidecar: <msg>`：
- hermes 走 stdlib logging 的 `"%(asctime)s %(levelname)s a2a-sidecar: %(message)s"`。
- openclaw 的 `Logger`（保留变参 API 方便 21 处 `logger.info(...)` 零改动）内部统一加前缀。
- 共用 regex `^\S+\s+(INFO|WARN|ERROR)\s+a2a-sidecar:\s+.*$` 能 parse 两侧。
一次性小 commit（29 行改动 + 一处测试 assertion 跟上新格式）。

### 10. `docs/a2a-envs.md` 作为 env 词典（review-2 §15 已解决）
40+ `A2A_*` / `CLAWCU_A2A_*` env 变量第一次集中登记：按 identity / upstream / timeouts / rate limit / registry / mcp / threading / logging / build / control-plane 分组，列出 Default / Scope / Purpose；deprecated alias 单独成节（`_MS → _SECONDS`、`A2A_NAME → A2A_SELF_NAME`）。运维排错从"跨三个 grep 面"收敛到"查一份表"。

---

## 二、设计得不够好 / 值得质疑的地方

### 1. review-2 §8 未解决：server.py 顶部 dead import 仍用于测试可寻址
`hermes/sidecar/server.py` / `openclaw/sidecar/server.py` 顶部仍 import `read_or_mint_request_id` / `read_hop_header` / `REQUEST_ID_HEADER` / `hop_budget_from_env`，handler body 已不直接使用。保留动机是测试通过 `server.read_or_mint_request_id(...)` 验证协议行为；删掉会让十几个测试炸。review-2 提出的两种出路（`_test_exports.py` sibling / 测试改走 `_common.protocol`）都未采纳。**建议归为中优先级**：不影响运行，只影响 linter cleanness；测试层重写比 `_common/` reorg 代价低，但本轮没人动它。

### 2. review-2 §9 未解决：hermes `server.py` 仍偏厚（现 882 行）
openclaw 侧 `server.py` 785 行（去掉 context.py 之后），hermes 882 行，两侧仍差 ~100 行。差距集中在：
- `_handle_a2a_send` / `_handle_a2a_outbound` 两个 handler 的 body 长（SSRF 检查 + registry_url 覆盖 + peer cache 三分支互相穿插）。
- MCP tools/list 静态-vs-动态判定散在 `_handle_mcp`。
review-2 建议抽 `hermes/sidecar/outbound_route.py` 或把 `tools/list` 静态-vs-动态逻辑折进 `_common/mcp/dispatcher.py`（让两侧 `/mcp` handler 只剩一行 `return handle_mcp_request(...)`）。**建议归为低优先级**：882 行不算灾难，且拆完两侧还是要各自保留一份"把 ctx/cfg 展开成 handler 参数"的胶水层；再拆是 taste 不是硬伤。

### 3. review-2 §11 未解决但压力下降：`_common/` 仍是扁平 12 模块 + `mcp/` 子包
当前结构：
```
_common/
  bootstrap.py        257
  http_response.py     41
  inbound_limits.py   241
  mcp/               (子包, 4 模块, 508 行)
  outbound_limit.py   257
  payload.py           85
  peer_cache.py       216  (+44, 吸收了 SSRF 白名单)
  protocol.py         273
  ratelimit.py        127
  readiness.py         53
  streams.py           57
  thread.py           153
```
review-2 §11 的论据是"13 个扁平文件会越堆越乱，趁早切 inbound/outbound/mcp/runtime 四个子包"。现在的实情：
- `mcp/` 子包化后最大的单点文件是 `protocol.py` 273 行，不再有 "mcp.py 独自 451 行" 的膨胀风险。
- 12 个同级文件 IDE 侧栏仍可速读（一屏内），没有命名冲撞。
- 做 inbound/outbound/runtime 三个 subpackage 意味着 20+ 个 `from _common.xxx` 导入路径全部改名，测试层`hermes/sidecar/inbound_limits.py` 这类 19 行 shim 也要跟着改——换来的只是树形结构美观，没有新能力。
**建议归为低优先级**：等到 `_common/` 再长 3 个模块、或出现真的跨模块共享私有符号时再做；把它做到"高 ROI"名单里当前是 premature abstraction。

### 4. review-2 §12 未解决：双 sidecar `sys.path.insert` bootstrap 仍是 3 段式脆代码
两侧 `server.py` 开头都是 walk-up-4-levels 探测循环找 `_common/`，目录层数改一层就静默跳过。review-2 建议挪到 `_common/__init__.py::find_and_inject()`——但这里有鸡生蛋问题（`_common` 还没在 path 里，哪来的 `_common/__init__.py` 可调用）。实际上更干净的出路是两侧 entrypoint.sh 里直接 `export PYTHONPATH`；pure-python 开发/测试仍需要 `sys.path` hack 一下，但可以把它简化为显式 `raise RuntimeError` 而不是静默跳过。**建议归为中优先级**：不是 data-plane issue，但首次部署时如果路径猜错会出现"从 _common 找不到 X"这类让人抓狂的错误信息。

### 5. review-2 §13 未解决：`/a2a/outbound` 的 `registry_url: null` 与缺字段同义
两侧都把 body 里 `{"registry_url": null}` 和 `{}`（缺字段）当作"用默认 registry"处理。如果客户端的意图是"显式不使用 registry，直接给我回 400"，当前 payload 表达不出来。**建议归为低优先级**：review-2 自己也把这条放在"没有明显好坏但值得留意"节，既没发生真 bug 也没测到。列入"将来做 JSON Schema 时顺手处理"的候选。

### 6. `hermes/sidecar/inbound_limits.py` 19 行 shim 没有 sunset 标记
review-2 §二-2 已指出这类 shim 是 refactor 过程中的两面性资产：保持测试寻址 + 长期维护成本。本轮没加统一的 `# TODO(post-review-N): drop once tests switch to _common.*` 注释或版本号登记；哪天删它还得重新考古测试改了没。**建议归为低优先级**：单文件维护成本很低，但值得在下一轮 housekeeping 加一致的 sunset 标注。

### 7. `_common/` 仍无 `__all__` 和版本号（review-2 §三-3 延续）
`_common/mcp/__init__.py` 是本轮唯一有 `__all__` 的文件（为拆子包时显式 re-export）。其它 11 个 `_common/*.py` 仍靠"具体 import 哪个就能用哪个"。如果未来把 `_common/` 做成可分发包 `clawcu-a2a-common`，没有 `__all__` / 没有 `__version__` 文件是第一个阻挡。本轮不做没有 immediate cost，**归为低优先级**。

### 8. `_common/peer_cache.py` 职责略分叉
本轮为了把 SSRF 白名单放在"两个 sidecar 都能共享的地方"，挑了 `_common/peer_cache.py`（长到 216 行）——但 SSRF 白名单与 peer-list TTL 缓存在概念上没强相关。`peer_cache.py` 现在同时扛着"注册表响应缓存"和"outbound URL 校验"两件事。**建议归为中优先级**：下一轮要么把 SSRF 抽进 `_common/outbound_limit.py` 或独立的 `_common/ssrf.py`，让 `peer_cache.py` 回到单一职责。

---

## 三、没有明显好坏但值得留意的点

1. **`_common/mcp/` 子包现为内部样板**：其它日后需要拆的 `_common/*.py` 文件（如果有）可以照抄这个包结构 + `__init__.py` 重导出 + 外部 import 路径不变的三件套。值得在贡献者指南里点名。
2. **`docs/a2a-envs.md` 是手写的，未来 env 增删需要同步更新**：`_common/envs.py` 或 `clawcu a2a env` dump 的自动化思路本轮没做；考虑下一轮给它加个 CI check（grep 源码里的 env 名字 vs 文档列表，diff 失败就 fail）。
3. **openclaw Logger 保留了变参 API**：与 hermes 的 stdlib `logging.getLogger(...).info("%s", x)` 风格仍有差异，统一成 stdlib 是可选项；保留变参是因为 21 处 call site 的变更 ROI 不够。
4. **review-1 §8 / §11 / §12 / §13 / §14 仍未处理**（端口串行探测、空 skills、hostname 格式校验、docker tag 黑名单）——都是 polish 类，延续结论。

---

## 四、优先级建议

本轮完成 review-2 §四 的 全部 6 条"高优先级建议"（§1、§2、§10、review-1 §4/§5/§10、§3/§4、§14、§15）。剩余 7 条观察全部落在 **中/低优先级**，理由都写在 §二 各条里。

**明确不保留"高优先级"分类**：
- §11（`_common/` 子包 reorg）降为低优先级：mcp 拆子包后压力解除，趁早做的论据失效。
- §9（hermes server 882 行）降为低优先级：拆剩下 ~100 行差距是 taste 不是硬伤。
- §8（server.py dead import）、§12（sys.path hack）、§三-新 7（peer_cache 职责）归为中优先级：都是可维护性改进，不影响运行。
- 其它（§13、§14-shim sunset、§三-新 7 `__all__`）归为低优先级，滚进日常 housekeeping。

**若要做 review-4 迭代**，建议从"中优先级"里挑 §二-新 4（sys.path bootstrap 显式 fail）+ §二-新 7（SSRF 从 peer_cache 分离）这两条小改动一起打包：都是模块边界/错误信息改善，不跨 sidecar，代价小于一天。

---

**评审人**：Claude Code
**评审日期**：2026-04-24
**覆盖面**：`src/clawcu/a2a/` 控制面 + `sidecar_plugin/_common/` 12 模块 + `mcp/` 子包 + 两个 sidecar + `docs/a2a-envs.md`
**未覆盖**：`tests/test_a2a*.py` 测试层本身；review-1 已覆盖且本轮未改动的 `builder.py / detect.py / bridge.py` 等控制面组件。
**与 review-2 的 delta**：review-2 §一 延续；§二 共 15 条——§1/§2/§3/§4/§5/§6/§7/§10/§14/§15 全部解决（review-2 §四 优先级列表清零）；§8/§9/§11/§12/§13 延续为中/低优先级。新增 3 条观察（§二-新 7 peer_cache 职责、§三-新 1 子包样板作参考、§三-新 2 env 词典自动化）。review-1 遗留的 §4 / §5 / §10 同步结案。
