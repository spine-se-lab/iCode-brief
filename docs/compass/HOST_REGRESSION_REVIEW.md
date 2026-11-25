# Compass 宿主兼容性修复与界面预算审核

更新时间：2026-11-19。本次只更新本地审核代码，未提交或推送 GitHub，未调用付费模型。

## 这次解决了什么

原版测试报错是我们接入代码引入的兼容性问题：`runner.py` 原来调用 `backend.run()`，后来无条件要求执行器提供新增的 `run_pass()`。原版替身遵守原接口，因此 28 个测试失败。此次恢复原接口，没有给原版测试替身补方法掩盖问题。

现在每次执行时：没有挂载 Compass，就直接走原来的 `backend.run(request)`；挂载了 Compass，才调用 `compass.run(request)`。原生重试、steer、结果记录与收尾流程保留。

另外一个 Compass 测试使用了写死的会话 ID `s`，与存档身份不一致。改为使用真实存档的 session ID；新增正反测试，确认相同会话保留预算与证据，不同会话不恢复旧任务。没有放宽身份校验。

## 用户确认后的预算

| 入口 | 主请求上限 | Compass 总时间上限 |
| --- | --- | --- |
| iCode 界面 | 同一任务累计 250 次尝试 | 无 |
| 独立评测 CLI | 每题 250 次尝试 | 11,400 秒 |

例如任务开始后离开界面四小时，再输入“继续”，不会仅因时间到期而失败；如果已经用掉 250 次主请求，仍然停止。后续消息、模型切换和恢复存档不会刷新调用额度；新任务使用 `/new`。

界面仅取消 Compass 外层的任务计时器。原生单次请求超时、取消、自动重试及维护模型自己的调用限制不变。存档中的起始时间继续保留用于审计，不再作为界面过期条件。提交检查仍要求至少剩余 15 次调用。

## 本轮源代码改动

| 文件 | 改动和原因 |
| --- | --- |
| `src/chrys/orchestration/engine/run/runner.py` | 可选进入 Compass；未启用时沿用原 backend 接口，包括没有 Compass 属性的旧执行器。 |
| `src/chrys/orchestration/engine/run/bindings.py` | 删除不再需要的 `run_pass()`；保留已有的可选 Compass 绑定和失败报告接口。 |
| `src/compass/adapters/icode/interactive.py` | UI 的 `remaining()` 表示无截止时间，移除界面外层 `asyncio.timeout`；共用的调用数检查继续生效。 |

冻结的 `src/compass/core/`、`ports.py`、`provenance.json`、独立评测 CLI、运行预算常量、共享 RunState/workflow、提示词与模型连接代码均未改动。

测试方面，新增 `test_host_compatibility.py` 的 9 个用例；修正旧恢复测试的 session ID；将旧 UI 墙钟测试更新为用户明确选择的新策略。没有改动原版 iCode 的测试文件。

## 验证结果

| 范围 | 修复前 | 最终结果 |
| --- | --- | --- |
| 原版 retry_lifecycle + lifecycle_vision | 28 失败、39 通过 | 67 通过 |
| 原版 lifecycle_hooks | 本次首次复现时 57 通过 | 57 通过 |
| 全部 Compass 测试 | 本地实际收集 115 个：1 失败、114 通过 | 124 通过（新增 9 个） |
| 上述范围合计 | — | **248 通过，29.09 秒** |

最终命令：

```bash
UV_CACHE_DIR=/tmp/compass-review-uv UV_PYTHON_DOWNLOADS=never \
uv run --no-sync --python .venv/bin/python pytest \
  tests/integration/compass \
  tests/orchestration/engine/run/test_retry_lifecycle.py \
  tests/orchestration/engine/run/test_lifecycle_vision.py \
  tests/orchestration/engine/run/test_lifecycle_hooks.py -n0 -q
```

新增验证覆盖：关闭/启用/旧执行器三种接入，普通执行与 steer，相同/不同会话恢复，过期时间下 UI 可继续而 CLI 仍停止，以及 UI 达 250 次仍退出。原生重试测试同时验证重试等待不会触发 UI 总时间超限。

这是针对本次报告的兼容性回归，明确扩大到了全部 Compass 与受影响的原版测试，而非继续只测新增文件。没有运行整个 iCode 测试仓库。测试使用离线模型，不证明真实模型一定调用需求更新工具。Python 为 3.14.0，未改变仓库的 3.14.10 pin。

静态检查：本次修改的 Python 与 Compass 测试 Ruff 检查、格式检查通过；宿主 runner/bindings 与 Compass 接入层/runtime 类型检查通过；diff 空白检查通过。源文件在测试运行期间没有继续修改，最终同步到 Downloads 后以哈希核对。

旧审核文档保留各轮当时的测试记录；其中“本轮未重跑”“时间预算包括空闲”描述的是旧轮次。当前行为和验证以本文为准。
