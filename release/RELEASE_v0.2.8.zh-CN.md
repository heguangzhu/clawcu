# ClawCU v0.2.8

🌐 Language:
[English](RELEASE_v0.2.8.md) | [中文](RELEASE_v0.2.8.zh-CN.md)

发布日期：2026 年 4 月 19 日

> `v0.2.8` 补上了 `v0.2.0` 无法表达的那一环：**孤儿实例生命周期闭环**。当一个托管实例的记录丢失或被删除时，它的 datadir 不再是磁盘上一块死角——ClawCU 现在可以列出它、重建它、或彻底清理它，而且享受与普通实例同等的安全保障。

* * *
## 亮点

- 孤儿实例生命周期
  - `clawcu list --removed` 显示 `CLAWCU_HOME` 下那些 instance 记录已消失的 datadir。
  - `clawcu recreate <orphan> [--version <v>]` 从残余状态恢复受管实例；只要 datadir 中有 `.clawcu-instance.json`，端口 / 版本 / 元数据可完整还原。
  - `clawcu remove <orphan> --removed` 从 `list --removed` 里彻底删除孤儿 datadir。

- 自描述 datadir
  - 每个实例现在会在 datadir 里写一份 `.clawcu-instance.json`，与运行时状态并列。
  - `list --removed` 读这份元数据就能还原 service / version / port，不再一律显示 `-`。
  - `v0.2.6` 之前创建的老 datadir 没有这份 sidecar，仍能被列出，只是缺失字段显示 `-`。

- `list` 和 `remove` 的标志语义更安全
  - `list` 现在会显式拒绝冲突组合（`--local --removed`、`--source managed --removed`、`--source all --removed` 等），给出一行错误说明，而不是静默挑一个赢家。
  - `remove --removed` 会拒绝 `--delete-data` / `--keep-data`（在"永久删除"语义下这两个毫无意义），而不是静默忽略。

- 更精准的错误提示
  - `clawcu remove <unknown> --removed` 提示 "Run `clawcu list --removed` to see recoverable leftovers"，而不是通用的"找不到实例"。

- 355 条测试
  - 测试套从 `v0.2.0` 时的 170+ 增长到 355 条，全部通过；新增覆盖孤儿生命周期边界、冲突标志矩阵、元数据辅助恢复路径。

* * *
## 孤儿生命周期问题的由来

在 `v0.2.8` 之前，如果一个实例的记录丢失了——直接编辑 registry、还原一份备份、或一次失败的 `create` 遗留了数据——`~/.clawcu/<name>` 下的 datadir 就会留在磁盘上，但对 ClawCU 而言形同隐身：

- `clawcu list` 不显示它
- `clawcu recreate` / `upgrade` / `rollback` 都无法定位它
- 唯一可行的清理方式是 `rm -rf`，同时也丢弃了本可恢复的状态

`v0.2.8` 把这类 datadir 上升为一等概念：孤儿（orphan）。它们不受管，但被 ClawCU 知悉。

### 发现

```
clawcu list --removed
```

列出 `CLAWCU_HOME` 下所有没有活动记录对应的 datadir。每条都报告它的 service（能读到 `.clawcu-instance.json` 时直接读，否则从 datadir 布局推断）、持久化的 version、以及持久化的 port。

### 恢复

```
clawcu recreate <orphan>
```

从孤儿 datadir 重建一个受管实例。只要 `.clawcu-instance.json`（`v0.2.6` 引入）在场，ClawCU 可以零输入还原 service / version / port——恢复后的实例沿用记录丢失之前的端口。

对老版本遗留的 datadir，service / version 的推断是尽力而为，可通过 `--version <v>` 显式钉住目标：

```
clawcu recreate <orphan> --version 2026.4.9
```

### 永久删除

```
clawcu remove <orphan> --removed [--yes]
```

彻底清理孤儿 datadir。`--removed` 是永久删除未受管 datadir 的唯一路径；它**故意**不接受 `--keep-data` / `--delete-data`——这个 flag 的全部意义就是永久删除，再加修饰词只会互相矛盾。

* * *
## `.clawcu-instance.json` —— 自描述 datadir

ClawCU 现在会在每个实例的 datadir 里落一份元数据边车：

```
~/.clawcu/<instance>/.clawcu-instance.json
```

其中记录了未来重建实例引用所需的全部信息：

- service（`openclaw` / `hermes`）
- version / tag
- port
- created-at 时间戳

这就是 `v0.2.8` 的孤儿恢复能保住端口的根本原因。生命周期层在 `create` / `clone` / `upgrade` / `recreate` 各路径上都会写入这份 sidecar，因此 `v0.2.6` 之后诞生的 datadir 天然自描述。

`v0.2.6` 之前的老 datadir 没有 sidecar。`list --removed` 仍然显示它们，只是在无法还原的列上显示 `-`，`recreate` 时需要显式传 `--version`。

* * *
## `v0.2.0` 之后累积的小 UX 打磨

`v0.2.8` 还携带了几个小但承重的 UX 改动，是 `v0.2.x` 周期里逐步累积下来的：

### `list` 标志冲突显式报错

`clawcu list` 有多种选择 source 的方式：`--source`、`--local`、`--managed`、`--all`、`--removed`。以前不相容地组合会悄悄走默认行为；现在会打印一行错误：

> Error: --removed cannot be combined with --local/--managed/--all; drop one of them.

并以非零状态退出。

### `remove --removed` 显式拒绝冗余标志

因为 `--removed` 已意味着"永久删除这个孤儿 datadir"，再叠加 `--keep-data`（保留数据）或 `--delete-data`（也删除，但适用于已跟踪实例）毫无意义。`v0.2.8` 会直接报错而不是静默吃掉。

### 找不到实例时的提示分流

通用的 "Instance 'X' was not found" 过去即使用户目标是孤儿也仍然推荐 `clawcu list`。现在会识别 "Removed instance 'X' was not found" 的形状，把用户指向 `clawcu list --removed`。

* * *
## 兼容性

`v0.2.8` 是 `v0.2.0` / `v0.2.6` / `v0.2.7` 的直接升级，零迁移成本。

- 现有受管实例照常运行，无须迁移。
- 在 `v0.2.8` 上执行 `create` / `clone` / `upgrade` / `recreate` 时会写入 `.clawcu-instance.json`，后续孤儿恢复自动受益。
- `v0.2.6` 之前遗留的 datadir 仍可恢复，只是 `recreate` 时需要补上 `--version <v>`。

没有任何破坏性 CLI 变动。

* * *
## 推荐工作流

`v0.2.0` 给出的建议路径依然适用——clone、在 clone 上 upgrade、验证、必要时 rollback。`v0.2.8` 在生命周期图上新增一个可达状态：

- 如果实例记录丢了：`list --removed` → `recreate` 把它拉回来，或 `remove --removed` 让它彻底消失。

两条路都明确、可脚本化、受 snapshot 保护。

* * *
## 结语

`v0.2.0` 让 ClawCU 从"OpenClaw 辅助器"蜕变成多服务平台。以 `v0.2.8` 结束的 `v0.2.x` 周期，让这个平台能从半残状态里自己走出来，而不必让用户去 `rm -rf` 赌一把。

下一步进入 `0.3.0`：所有读命令统一 `--output {table|json|yaml}` 协议、provider bundle 的溯源能力、以及把 active-provider 升级为一等字段。
