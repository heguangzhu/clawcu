# ClawCU v0.2.11

🌐 Language:
[English](RELEASE_v0.2.11.md) | [中文](RELEASE_v0.2.11.zh-CN.md)

发布日期：2026 年 4 月 22 日

> `v0.2.11` 让 ClawCU 正式区分"逻辑服务版本"和"实际运行镜像"：`--version` 继续必填，`create` 与 `upgrade` 新增可选 `--image`，而且选中的镜像会一路保留到后续 `recreate`、孤儿恢复和 `rollback`。

* * *
## 亮点

- **`create` 与 `upgrade` 支持可选 `--image`**
  - `--version` 继续作为实例记录里的必填逻辑版本标签。
  - `--image` 变成实际 Docker runtime artifact 的显式覆盖。
  - 两者同时传入时，ClawCU 记录 `--version`，运行 `--image`。

- **runtime image 链路持久化**
  - 选中的 `image_tag` 现在会保存在实例状态和 `.clawcu-instance.json` 中。
  - `retry` 与 `recreate` 会复用该镜像，而不是悄悄按版本重新推导。
  - 只要元数据存在，孤儿 datadir 恢复后也会沿用同一条自定义镜像路径。

- **回滚恢复的是历史镜像，不只是历史版本**
  - `upgrade` / `rollback` 历史现在会同时记录 `from_image` / `to_image` 和 `from_version` / `to_version`。
  - `rollback` 不再假设"旧版本就对应官方默认镜像"，而是恢复那次转换之前真实运行过的 runtime artifact。

- **379 条测试**（`v0.2.10` 为 366），新增覆盖自定义镜像创建、同版本换镜像升级、`recreate` 复用镜像、孤儿恢复镜像链、`rollback` 恢复历史镜像等路径。

* * *
## 为什么要把 version 和 image 分开

很多用户其实同时需要两层事实：

1. 自己想表达的**逻辑服务版本**（比如 `2026.4.10`）；
2. 实际生产里要跑的**Docker 镜像**（比如 `registry.example.com/openclaw:2026.4.10-tools`）。

在 `v0.2.11` 之前，ClawCU 是强 version-centric 的：版本既是用户看的标签，也是运行 artifact 的来源。对官方镜像这完全没问题，但对自定义 runtime image 就不够顺手。用户也许只是想在镜像里多装点工具或依赖，却没有一条一等公民的路径表达"逻辑上还是 `2026.4.10`，只是镜像换成我自己的构建"。

`v0.2.11` 现在把这件事明确了：

```bash
clawcu create openclaw \
  --name writer-tools \
  --version 2026.4.10 \
  --image registry.example.com/openclaw:2026.4.10-tools
```

版本继续是 `list`、`inspect`、升级历史里讨论的对象；镜像则是最终真正启动的 artifact。这样既保住了原有 UX，又把自定义镜像从“旁门左道”变成了正式支持的路径。

* * *
## 为什么镜像链必须持久化

如果 `create` / `upgrade` 接受了 `--image`，但后续生命周期动作不继续尊重它，那这个参数的价值只剩一半。

一个非常真实的场景会立刻出问题：

1. 先用自定义镜像创建实例；
2. 后面执行 `clawcu recreate writer-tools`；
3. 结果因为系统又按 `version` 重算镜像，悄悄掉回官方默认镜像。

这正是那种“平时看着都正常，重启那天才出事”的风险。

所以 `v0.2.11` 把镜像覆盖一路接通：

- `retry` 会复用创建失败实例保存下来的 runtime image；
- `recreate` 会复用受管实例当前保存的 runtime image；
- 孤儿恢复会在有 `.clawcu-instance.json` 时读取其中的 `image_tag`；
- `rollback` 会恢复该历史转换记录下来的镜像，而不是重新推一个默认值。

这样一来，自定义镜像不再只是启动时的一次性覆盖，而是实例状态的一部分。

* * *
## 兼容性

`v0.2.11` 是 `v0.2.10` 的无痛升级。

- 无破坏性 CLI 变更。
- `create` 与 `upgrade` 依然要求 `--version`。
- `--image` 是纯新增、可选参数。
- 没有自定义镜像历史的老实例，行为与之前完全一致。
- 没有元数据的老孤儿 datadir，依然通过 `clawcu recreate <orphan> --version <v>` 恢复。

这次主要的增量 schema 变化，是把 runtime image 更明确地写入实例元数据里，方便后续生命周期动作复原同一条 artifact 链。

* * *
## 推荐工作流

对官方镜像用户来说，工作流不变：

```bash
clawcu create openclaw --name writer --version 2026.4.10
clawcu upgrade writer --version 2026.4.11
```

对自定义 runtime image，建议显式保留版本标签，只覆盖实际镜像：

```bash
clawcu upgrade writer-tools \
  --version 2026.4.11 \
  --image registry.example.com/openclaw:2026.4.11-tools
```

之后的 `recreate`、孤儿恢复和 `rollback` 都会自动沿着这条记录下来的镜像链继续执行。

* * *
## 结语

`v0.2.10` 把默认 `list` 路径做得更快、更诚实；`v0.2.11` 则把自定义 runtime 这条路径也做到了同样的水位：用户意图更明确、状态更耐久、回滚也更可信。

下一步仍然是 `0.3.0`：统一的 `--output {table|json|yaml}` 协议、provider bundle 溯源、以及把 active-provider 升为一等字段。
