# ClawCU v0.2.10

🌐 Language:
[English](RELEASE_v0.2.10.md) | [中文](RELEASE_v0.2.10.zh-CN.md)

发布日期：2026 年 4 月 19 日

> `v0.2.10` 是 `v0.2.9` 之上的一次打磨：`clawcu list` 的"Available versions"页脚现在按天缓存，不再每次都打 registry；registry 不通时还会把本地 Docker 镜像作为离线候选展示出来——让你还能看到能动的东西。

* * *
## 亮点

- **Available versions 按天缓存**
  - 成功的 registry 拉取会缓存到 `<clawcu_home>/cache/available_versions.json`，按 service + `image_repo` 作 key。
  - 同一天内后续的 `clawcu list` 直接走缓存，不做网络往返。
  - 本地日期跨天后、或 `image_repo` 改了（比如 `clawcu setup` 重新配）都会自动失效。
  - 失败**永远不缓存**，临时网络抖动不会粘住 24 小时。

- **离线回退到本地 Docker 镜像**
  - registry 拉取失败（断网、DNS、auth 挂了）或 `--no-remote` 时，页脚现在会 `docker image ls <repo>` 拿本地 tag，放在错误行下方作为离线候选。
  - 预发布（`-beta`、`-rc`、`-alpha`）和 `latest` 被过滤，与 remote 一致。
  - 红色错误行**仍然显示**——用户得知道自己看到的是"离线回退"，不是权威视图；但旁边现在有能用的候选。

- **366 条测试**（`v0.2.9` 是 360），覆盖：缓存命中、跨日重拉、`image_repo` 变更失效、失败不缓存，以及两条本地回退分支。

* * *
## 为什么按天缓存

`clawcu list` 是一天跑很多次的命令——"现在跑着啥，各自在哪"。`v0.2.9` 在这条命令上挂了 Available versions 页脚，让用户扫 list 的时候顺便看到升级候选，UX 上确实是赢的。但代价是：每次调用都会打两次 registry 网络（每个 service 一次），每次都付 DNS + TLS + HTTP。

网好的时候是几百毫秒，网慢的时候就明显了，网差的时候变成"今天 list 为啥这么卡？"。

按天的粒度正好契合"用户真正多久关心一次新版本"。没人发个 patch release 还指望用户一小时内看到。每天刷新一次是 freshness 和 speed 之间的最佳权衡：

```
$ time clawcu list              # 今天第一次调用 —— 打 registry
real    0m2.412s

$ time clawcu list              # 第二次 —— 走缓存
real    0m0.198s
```

### 缓存失效规则

缓存 key 是 **`(service, image_repo)`**，不只 `service`。这个重要——用户可以通过 `clawcu setup` 改镜像仓库地址，改了之后旧缓存就不能再给了。判断极其简单：`cached_entry.image_repo != current_image_repo` 就重新拉。

缓存条目还带 `fetched_date`，只要不是今天的本地日期就重拉。

没了。没有显式的失效开关，没有秒级 TTL，没有 "强制刷新" flag。`--no-remote` 依然可以完全绕过这条路径，用作确定性的离线渲染。

* * *
## 为什么要本地镜像回退

`v0.2.9` 的页脚在失败时只有一种显示：一行红色 "registry 不通"，然后就没了。用户拿不到任何信息。

```
$ clawcu list
... [实例表]

Available versions (top 10 by semver, newest first)
  openclaw  Network is unreachable
  hermes    Network is unreachable
```

技术上对，但实用上毫无意义。用户笔记本上本来就有一堆本地 Docker 镜像——这些镜像恰恰就是 `clawcu upgrade` 在镜像已存在时会直接用的。它们就是*此刻*可用的升级候选，不需要任何网络。

`v0.2.10` 把它们露出来：

```
$ clawcu list               # registry 不通
... [实例表]

Available versions (top 10 by semver, newest first)
  openclaw  Network is unreachable
            local images: 2026.4.12, 2026.4.10, 2026.4.8, 2026.4.5
  hermes    Network is unreachable
            local images: 2026.4.13
```

### 为什么两者都要显示，为什么只在失败时回退

- 远程错误**保留**：用户需要知道自己看到的是"离线回退"，不是权威视图。悄悄降级反而会掩盖真实问题（"为啥我的升级列表不涨了？"）。
- 本地回退**只在 remote 没产出版本时才触发**：registry 正常时就拿真的，成功路径下没必要再去查本地 docker——更省，噪音更少。
- `--no-remote` **也会显示本地镜像**：用户主动离线，但磁盘上还有状态要关心；有东西可看永远好过空行。
- 过滤规则与 remote 一致：不显示预发布，不显示 `latest`。这是"安装候选"视图，`latest` 是个漂浮 tag，放进版本比较列表没意义。

* * *
## 兼容性

`v0.2.10` 是 `v0.2.9` 的无痛升级。

- 无破坏性 CLI 变更。
- `list --json` 的 payload 实例数组契约不变；版本页脚依然只在文本模式下渲染。
- `list_service_available_versions`（service 层 API）在每个 entry 上新增 `local_versions` 字段——纯加，现有调用者照常工作。
- 升级后第一次运行时缓存文件尚不存在；第一次 `list` 会跑一次新鲜拉取把缓存种上。正常。

* * *
## 推荐工作流

沿用 `v0.2.9`：

- 先在 clone 上升级，验证通过再升级主实例。
- 每次升级前都有快照；`rollback` 从真实备份恢复。
- `list --removed` → `recreate <orphan>` 做孤儿恢复。

`v0.2.10` 只是让默认 list 路径在热路径上更快，在冷路径上更诚实。

* * *
## 结语

`v0.2.9` 加上 Available versions 是因为"一眼看到升级候选"是真实需求。`v0.2.10` 把"功能存在"和"功能手感对"之间的缺口补上了：每次调用不再交税，registry 挂了也还有能说的东西。

下一步进入 `0.3.0`：统一的 `--output {table|json|yaml}` 协议、provider bundle 溯源、把 active-provider 升为一等字段。
