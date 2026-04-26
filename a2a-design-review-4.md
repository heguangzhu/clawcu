# A2A 分支设计评审（review-4）

**评审范围**：`a2a` 分支相对 `main`（merge-base `ea93e8d`）的全部新增/变更。相对 review-3（5882a01）新增 2 个 commit：
- `7a48bac` — `feat(provider): carry hermes auth.json through collect/apply`
- `db1cdfc` — `fix(a2a): bootstrap two MCP config shapes — hermes flat, openclaw nested+streamable-http`

**评审维度**：模块划分、sidecar 双胞胎对等性、协议契约、共享层边界、测试表面、开发/运维体验；重点追踪 review-3 §四"明确不保留'高优先级'分类"之后是否又冒出新高优先级项。

**评审方式**：通读本轮两条 commit 的全部 diff（7 文件、+344 / -59 行）；对照 review-3 §二 / §三 每条观察在当前树上的状态；跑 `tests/test_sidecar_bootstrap.py`（24）+ `tests/test_a2a.py`（246）共 270 测试，全部通过。

---

## 一、设计得好的地方（相对 review-3 新增）

### 1. 两条 commit 都是"review-3 没抓到的高优先级事故"的正面解决（review-3 §四 清零后冒出的新高优）

#### a. MCP auto-wire 的 schema 分叉被暴露并修复（`db1cdfc`）

review-3 没进 `_common/bootstrap.py` 的细节，是因为该模块属于"sidecar 启动时补 MCP 入口"的边缘胶水层；**实际运行下它是 LLM 自主编排路径的唯一入口**。本轮线上 E2E 才把两个 bug 同时翻出来：

- **Hermes 读 `mcp_servers`（扁平顶层、带下划线）**，但旧 `bootstrap.py` 写的是 OpenClaw 形状 `mcp.servers.a2a` → Hermes gateway 的 MCP 客户端（`hermes/tools/mcp_tool.py::_load_mcp_config`）只看一层 `config.get("mcp_servers")`，于是**LLM 从来没有看见 `a2a_call_peer` 工具**，A2A 协议栈两端 `/a2a/send` 全通、但自主编排分支静默瘫痪。
- **OpenClaw 的 MCP bundle 客户端默认 SSE**（`docs/cli/mcp.md`），而 sidecar 只服务 streamable-http → gateway 日志每次启动刷 `[bundle-mcp] SSE error: Non-200 status code (404)`，`a2a_call_peer` 同样不可见，行为与 Hermes 一致但根因不同。

修复的形状现在是：
```python
def _primary_path(fmt):       # yaml → ["mcp_servers", "a2a"]; json → ["mcp", "servers", "a2a"]
def _desired_entry(fmt, url): # yaml → {"url": url}; json → {"url": url, "transport": "streamable-http"}
def _legacy_paths(fmt):       # yaml 会迁走老的 ["mcp", "servers", "a2a"]; json 为空
```
`plan_bootstrap(fmt=...)` 分 yaml/json 两路；老部署的 config.yaml 里残留的 `mcp.servers.a2a` 在下次 sidecar 起的时候被 `_pop_at_path()` 自动清掉，运维不需要手改。

**好在哪**：
1. 补丁没走"再开一个 hermes-specific bootstrap"的路——仍是同一个 `_common/bootstrap.py`，按 `fmt` 一次分叉；两侧共享的单元测试表面保留。
2. 迁移策略显式——`_legacy_paths()` 是一个可枚举的 list，如果未来再加第三种 host，只要加一行 `return [...]`；不会引入"隐式靠覆盖"的 upgrade 路径。
3. 两侧"期望形状"全部在 `_desired_entry()` 里、一屏读完；对 `transport` / `url` / 嵌套深度的所有假设集中一处。

#### b. Hermes Codex `auth.json` 随 provider bundle 流动（`7a48bac`）

review-3 对 `provider collect/apply` 没做覆盖（不在 A2A 分支范围内），但 A2A 分支的多实例测试矩阵**依赖** provider apply 在新起的 hermes 实例里跑出活的 Codex provider——这一点之前只能靠运维 `cp ~/.clawcu/src/auth.json ~/.clawcu/dst/auth.json` 手动兜底。本轮补上：

- `core/storage.py` — `provider_auth_json_path()` 第三个持久层路径；`load_provider_bundle` 读出 `bundle["auth_json"]`；`save_provider_bundle` 写入。
- `hermes/adapter.py::scan_model_config_bundles` — 扫源实例 datadir 下 `auth.json`、挂进 bundle。
- `hermes/adapter.py::apply_provider` — 目标实例 datadir 下写入 `auth.json`，restore 的地方显式注释"without this the target instance's hermes gateway would 500 with 'No Codex credentials stored'"。
- `core/service.py::_provider_bundle_equals` 的 keys tuple 加 `"auth_json"`——"是否触发 update"信号跟上；避免"auth 变了但 bundle 等价判断说相同"。
- `tests/test_hermes.py::test_collect_and_apply_hermes_codex_auth_json` 覆盖完整 round-trip。

**好在哪**：三个持久层点（存/读/等价比较）加 adapter 的两端、加一个回归测试都在同一条 commit 里，没有拆得七零八碎；future reader `grep auth_json` 就拿到全部证据。

### 2. `_common/bootstrap.py` 从 251 → 325 行仍然合理

新增 74 行几乎全用于：docstring（文档化"为什么要双 shape"，含两个具体错误信息）+ `_primary_path` / `_legacy_paths` / `_desired_entry` / `_pop_at_path` 四个小 helper。`plan_bootstrap` 主逻辑从 "compute desired → compare → diff → deepcopy" 一路保留同一形状，只是把"哪条路径 / 哪份 entry"外化成纯函数。模块仍是纯函数 + 一个 side-effect-only `run_bootstrap`，单测可做到零 patch。

### 3. 测试命名把"host 分叉"显式化

`test_sidecar_bootstrap.py` 新增的测试全部显式写 host 名：`_yaml_writes_flat_mcp_servers_key` / `_yaml_migrates_stale_nested_entry` / `_yaml_remove_cleans_both_paths` / `_rewrites_when_transport_hint_missing` / `_run_bootstrap_yaml_writes_flat_mcp_servers` / `_run_bootstrap_yaml_migrates_stale_nested`。读测试名就知道是哪一条 host-specific 契约在锁，不再需要进 fixture 读 fmt。

---

## 二、设计得不够好 / 值得质疑的地方

### 1. review-3 §二-新 4 未解决：双 sidecar `sys.path.insert` bootstrap 仍是 3 段式脆代码

两侧 `server.py` 开头仍走 walk-up 探测循环找 `_common/`，目录层数改一层就静默跳过。本轮没动。review-3 建议的"两侧 entrypoint.sh 显式 `export PYTHONPATH` + pure-python 场景显式 `raise RuntimeError`"仍是最便宜的出路。**归为中优先级**。

### 2. review-3 §二-新 7 未解决：SSRF 白名单仍寄生在 `_common/peer_cache.py`

`_common/peer_cache.py` 仍 216 行，仍同时扛"注册表响应缓存"和"outbound URL 校验 + allowed schemes 白名单"。review-3 建议下一轮抽 `_common/ssrf.py` 或并入 `_common/outbound_limit.py`。本轮没动。**归为中优先级**。

### 3. 新：bootstrap 迁移路径不清理空的中间节点

`db1cdfc` 的 `_pop_at_path` 只删最末级：若老 yaml 里只有 `mcp.servers.a2a` 一个 entry，迁完以后留下一个空 `mcp: {servers: {}}`。功能上不影响 Hermes（它只读 `mcp_servers`），但文件里多了一段语义死代码，下一次运维 `cat config.yaml` 会困惑"为什么 `mcp:` 节点还在"。**归为低优先级**：加一个"父节点空则递归 pop"的小 helper 即可，独立改动 < 30 行。

### 4. 新：`auth.json` 在 provider apply 时的文件 mode 不保留

`hermes/adapter.py::apply_provider` 用 `Path.write_text` 写 `auth.json`，mode 走当前 umask（通常 0644）。Hermes 本身写 `auth.json` 时有没有更严格的 0600？——我没深挖，但这是 OAuth refresh token，**原则上**应 0600。当前实现不会引入新的攻击面（`~/.clawcu/` 全在 user 目录下），但**语义上**回退到"比原始更宽松"，值得在下一轮用 `os.chmod(auth_json_path, 0o600)` 显式收窄。**归为低优先级**：user-scoped 目录下不是现实威胁，但一旦谁把 clawcu 目录同步到共享盘就变成问题。

### 5. 新：`provider_bundle_equals` 的 keys tuple 是硬编码的魔法字符串

`core/service.py:2903`：
```python
keys = ("metadata", "auth_profiles", "models", "config_yaml", "env", "auth_json")
```
持久层对于"bundle 由哪些 key 组成"的唯一事实是**散在 storage.py / adapter.py / service.py 的若干处 `bundle["..."]`**。本轮加 `auth_json` 要同时改三个地方，漏一处就出 silent 行为分叉（本轮如果漏改 `_provider_bundle_equals`，就会出现"auth 更新了但 bundle 等价判断仍说相同，因此不 emit update"）。**归为中优先级**：下一次再加 key（例如 mcp_config、context files）之前，应该先把 bundle schema 集中到一个常量/dataclass。

### 6. review-3 §二 §1–§6 全部延续（未解决但均为中/低优先级）

- §1 `server.py` 顶部 dead import 为测试可寻址：未动。中优先级。
- §2 hermes `server.py` 仍 882 行：未动。低优先级。
- §3 `_common/` 12 模块扁平 + `mcp/` 子包：未动。低优先级（压力已由 `mcp/` 子包化解除）。
- §5 `/a2a/outbound` `registry_url: null` vs 缺字段同义：未动。低优先级。
- §6 `hermes/inbound_limits.py` 19 行 shim 没有 sunset 标记：未动。低优先级。

---

## 三、没有明显好坏但值得留意的点

1. **review-3 对"运行时路径"没有覆盖是这一轮的反思点**：`bootstrap.py` 在每个 sidecar 启动前会改一次宿主 LLM 的 MCP config 文件，它的输出决定了"LLM 自主编排能不能走通"。review-3 把它当做"配置胶水"略过，漏了 hermes/openclaw schema 分叉与 transport 默认值差异。**后续每次 review 都应显式把"bootstrap 是否可达 + 两侧 LLM 是否能看到 a2a_call_peer 工具"作为一条 E2E smoke 检查**，不只是 `/a2a/send` + `/mcp` 双通。

2. **provider bundle schema 与 A2A 分支的耦合点值得留意**：A2A 分支的多实例测试矩阵依赖 provider apply 出活的实例。`auth_json` 本轮解决了"hermes Codex"这一类；如果将来 openclaw 侧也出现"apply 后需要额外 credentials 文件"的情况，应同样走 bundle key 路径而不是 datadir 外部挂载。

3. **`tests/test_sidecar_bootstrap.py` 已经覆盖两种 host / 两种 transport / 迁移 / 幂等**：下一次改 `bootstrap.py` 之前，先看这份测试矩阵，能避免复现本轮"lay out 改了但 yaml 侧无人测试"的过度自信。

4. **docs/a2a-envs.md 仍没写关于 MCP auto-wire 的 env**（`A2A_SERVICE_MCP_CONFIG_PATH` / `A2A_SERVICE_MCP_CONFIG_FORMAT`）—review-3 §三-2 建议的"CI check"本轮也没做。**归为低优先级**：一次性补文档即可。

---

## 四、优先级建议

**本轮完成的高优先级工作**：
1. `db1cdfc` — sidecar MCP auto-wire 的 host/transport 分叉（review-3 漏抓），**修复 LLM 自主编排路径**。
2. `7a48bac` — provider apply 的 `auth.json` 复制（review-3 未覆盖），**修复 hermes Codex 实例在新部署里无法服务请求**。

**本轮结束时剩余的高优先级条目**：
> **不存在**。

**中优先级（建议下一轮一起做掉）**：
- §二-1（sys.path.insert 显式 fail）+ §二-2（SSRF 从 peer_cache 分离）——review-3 就已建议打包，本轮仍然合适。
- §二-5（provider bundle schema 抽到常量/dataclass）——新加的，避免下一次加 key 时再漏改 `_provider_bundle_equals`。

**低优先级（housekeeping 级别）**：
- §二-3（bootstrap 迁移空父节点清理）
- §二-4（`auth.json` 保留 0600 mode）
- §二-6 所有 review-3 延续条目

**明确不保留"高优先级"分类**。本轮"冒出"的两条高优都已落地；review-3 的"清零"结论事后看是不完整的（漏覆盖 bootstrap 运行时路径 + provider apply 与 A2A 分支的耦合），但重新覆盖后再次清零。

---

**评审人**：Claude Code
**评审日期**：2026-04-24
**覆盖面**：本轮新增 2 commit 的全部 diff（7 文件、+344 / -59 行）+ review-3 §二/§三 全部条目的当前状态追踪。
**未覆盖**：`tests/test_a2a*.py` 测试层本身的设计；review-1/2/3 已结论、本轮未改动的模块（`builder.py / detect.py / bridge.py / card.py` 等）。
**与 review-3 的 delta**：review-3 §四"无高优先级"结论**被两条新冒出的高优打破**，本轮全部处理完毕；review-3 §二 §二-新 §三 全部条目延续状态单独追踪（见 §二）；新增 3 条观察（§二-3 空父节点、§二-4 auth.json mode、§二-5 bundle schema 抽离）。
