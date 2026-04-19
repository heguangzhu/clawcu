# ClawCU v0.2.9

🌐 Language:
[English](RELEASE_v0.2.9.md) | [中文](RELEASE_v0.2.9.zh-CN.md)

发布日期：2026 年 4 月 19 日

> `v0.2.9` 是 `v0.2.8` 之上的一次 UX 专项补丁：`clawcu list` 现在会告诉你"可以升级到哪个版本"，而 `clawcu <cmd>` 不带参数时终于表现得像用户期待的那样——直接打印 help，而不是一行晦涩的错误。

* * *
## 亮点

- `clawcu list` —— 可用版本页脚
  - 在实例表格下方追加一个紧凑的"Available versions"区块。
  - 每个服务（OpenClaw、Hermes）各列出最新 10 个**稳定**版本，**最新在最前**。
  - 预发布版本（`-beta`、`-rc`、`-alpha`）会被过滤——页脚是"安装候选"视图，不是测试者视图。想看完整 tag 请用 `upgrade --list-versions`。
  - 从各服务配置的 image registry 尽力而为地拉取，失败会就地提示，不会出现静默空行。
  - 对 `--json`（脚本继续按实例数组协议返回）、`--agents`、`--removed` 会跳过。
  - 新增 `--no-remote` 标志，用于严格离线渲染（CI、隔离网络、慢网环境）。

- 空参数现在直接打印 help
  - 对带必选参数的命令，`clawcu <cmd>` 不带任何参数时现在会打印完整 help 并退出 0。
  - 以前这个场景输出一行晦涩的 `Usage: ... Try --help` 并退出 2——把"这个命令要传什么？"这样合理的查询当成失败调用。
  - 若用户传了部分参数但漏了必选项（partial invoke），仍然保持 POSIX 的退出 2，但现在会在 `Missing option` 错误前先打印完整 help，让用户一次看到所有 flag。

- 360 条测试
  - 测试套从 `v0.2.8` 的 356 条增长到 360 条，新增覆盖可用版本页脚路径（remote 成功 / remote 禁用 / JSON 跳过）以及"空参数 vs 部分参数"的 UX 分流。

* * *
## 为什么把"Available versions"放进 `list`

在 `v0.2.9` 之前，想知道升到哪个版本需要针对某个实例单独查：

```
clawcu upgrade writer --list-versions
```

这能用，但要求用户以"某个具体实例"的视角去思考。事实上，用户扫 `clawcu list` 的时候常常问的是一个更泛的问题——*OpenClaw 最新是啥？Hermes 呢？*

`v0.2.9` 就在他们已经在看的位置回答这个问题：

```
$ clawcu list
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ NAME            ┃ SERVICE  ┃ VERSION   ┃ PORT  ┃ STATUS  ┃ ACCESS          ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ writer          │ openclaw │ 2026.4.1  │ 18799 │ running │ 127.0.0.1:18799 │
│ analyst         │ hermes   │ 2026.4.13 │ 9129  │ running │ 127.0.0.1:9129  │
└─────────────────┴──────────┴───────────┴───────┴─────────┴─────────────────┘

Available versions (top 10 by semver, newest first)
  openclaw  2026.4.15, 2026.4.14, 2026.4.12, 2026.4.11, 2026.4.10, 2026.4.9,
            2026.4.8, 2026.4.7, 2026.4.5, 2026.4.2
  hermes    2026.4.16, 2026.4.13, 2026.4.8, 2026.4.3, 2026.3.30
```

### 设计取舍

- **默认打开。** 大多数用户每天运行 `list` 几次而不是几百次——每个 registry 4 秒左右的尽力而为查询，换"永远不会错过新版本"值得。
- **只列稳定版。** 页脚服务于日常升级决策。想跑 beta 的测试者仍可用 `upgrade --list-versions`。
- **最新在最前。** 契合人类扫描习惯：你最可能想要的版本在最左，而不是在行尾。
- **`--no-remote` 给 CI 用。** 在 registry 不可达或希望输出完全确定时，彻底跳过网络查询。
- **JSON 保持稳定。** 调 `clawcu list --json` 的脚本看到的仍然是一如既往的实例数组。

* * *
## 为什么空参数要打印 help

`v0.2.9` 之前的默认行为：

```
$ clawcu create hermes
Usage: root create hermes [OPTIONS]
Try 'root create hermes --help' for help.

Error: Missing option '--name'.
```

这实际上是把一个非常合理的用户提问（*"这个命令要传什么？"*）当成失败调用处理：吐出三行接近噪音的内容，并迫使用户再敲一遍命令才能看到他们真正想要的 help。

`v0.2.9` 把这种情况拆成两条路径：

### 空参数 → 打印 help，退出 0

```
$ clawcu create hermes
                                                                                
 Usage: clawcu create hermes [OPTIONS]                                          
                                                                                
 Create and start a Hermes instance.                                            
                                                                                
╭─ Options ──────────────────────────────────────────────────────────────────╮
│ *  --name          TEXT     Managed instance name. [required]              │
│ *  --version       TEXT     Hermes version or git ref. [required]          │
│    --port          INTEGER  Host port.                                     │
│    --datadir       TEXT     Data directory.                                │
│    --cpu           TEXT     CPU limit. Default 1.                          │
│    --memory        TEXT     Memory limit. Default 2g.                      │
│    --help                   Show this message and exit.                    │
╰────────────────────────────────────────────────────────────────────────────╯
```

没传参数就是"告诉我这个要传什么"——那就告诉他。退出 0。Typer 原生的 `*` 标记仍然在最左侧列标注必选项，用户一眼就知道哪些是必填。

### 部分参数 → help + 定向错误，退出 2

```
$ clawcu create hermes --name demo
[完整 help]

Error: Missing option '--version'.
```

用户若已经传了部分参数但漏了必选项，那显然是在认真地调用这个命令——所以保留 POSIX 风格的退出 2（脚本仍能捕捉失败），但同时把完整 help 打印出来，用户能看到**所有可用的 flag**，而不是盯着"哦 `--version` 漏了——那就只是 `--version` 吗？还有别的吗？"。

* * *
## 兼容性

`v0.2.9` 是 `v0.2.8` 的无痛升级。

- 无破坏性 CLI 变更。
- 现有受管实例照常运行，无须迁移。
- 调 `clawcu list --json` 的脚本看到的输出不变（版本页脚仅在文本模式下渲染）。
- 调任何带必选参数命令的脚本在"传了部分参数但漏一个"时仍然退出 2；只有**空参数**这条路径从退出 2 改成退出 0。

* * *
## 推荐工作流

沿用 `v0.2.8`：

- 先在 clone 上升级，验证通过再升级主实例。
- 每次升级前都有快照；`rollback` 从真实备份恢复。
- `list --removed` → `recreate <orphan>` 做孤儿恢复。

`v0.2.9` 的 list 页脚只是把**第一步**——"要升到哪个版本"——默认就展示出来。

* * *
## 结语

`v0.2.8` 闭合了孤儿生命周期。`v0.2.9` 是在做"别挡着用户"：少敲几个字就能看见版本候选，不再有空参数时的晦涩一行错误。

下一步进入 `0.3.0`：统一的 `--output {table|json|yaml}` 协议、provider bundle 溯源、把 active-provider 升为一等字段。
