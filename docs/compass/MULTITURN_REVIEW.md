# Compass 多轮对话：审核说明

最新修订：界面取消总时间上限、保留 250 次主请求；修复原生执行接口兼容性。当前验证和历史记录的区别见 [宿主回归修复审核](HOST_REGRESSION_REVIEW.md)。

**最新可读性整理：具名 Brief / 流程标志、类型标注与 9 项新增测试，见 [READABILITY_REVIEW.md](READABILITY_REVIEW.md)。下方流程和历史验证记录保留。**

2026-11-19（Toronto）。分支 `codex/compass-modular-latest-20261119`。本地修改，供审核；未提交、未推送，未调用在线模型。

## 最直观的变化

**一个任务可以聊很多轮。用户发下一句话，不再等于 Compass 换了一个任务。**

例如：

| 用户输入 | 现在怎么处理 |
| --- | --- |
| “修复空列表输入会崩溃的问题” | 创建任务，探索代码，调用 `start_compass_brief`，收到 Brief 后才能修改。 |
| “为什么这样改？” | 继续原任务，维护模型能看到这个追问；保留原要求和证据，不因追问重新探索或提取要求。 |
| “继续” | 使用原状态和剩余预算，不把“继续”当成完整任务。 |
| “空字符串也要支持” | 主模型应调用 `update_compass_requirements`；用原始任务和用户补充原文更新要求，再收到新 Brief 后修改。 |
| “修一下刚才的错别字” | 沿用已有交接和证据；如果实际尝试编辑，本轮结束时再做一次有预算限制的提交检查。 |
| `/new` 后输入另一个任务 | 使用 iCode 的新会话，新建 Compass 任务、证据记录和预算。 |

换任务使用明确的 `/new`。不会根据一句话猜测是否应该清空任务。尚未启动、没有尝试交接或编辑的普通问答，仍可正常结束；它不会建立一个必须永久延续的活动任务。关掉 Compass 时仍走原生 iCode 路径。

## 谁负责什么

- **iCode**：管理会话、消息、所选模型、工具审批、自动重试和失败收尾。
- **Compass 接入层**：把本次用户消息追加到活动任务，保存任务身份、预算和交接状态；过滤自身 Brief、流程提示、原生系统提醒及合成 continuation。
- **原版 core**：继续负责需求提取、证据、Claim、维护和 Brief。没有修改 core 的算法或文件。

```text
用户输入
  → iCode 主回合入口
  → InteractiveCompass.begin
      ├─ 当前会话有活动任务：恢复/沿用原任务
      └─ 没有活动任务或进入新会话：建立任务
  → 用户补充原文 → core.record_user_input
  → 主模型请求前：维护待处理补充、收集证据、按原策略投递 Brief
  → 主模型选择工具
      ├─ 首次探索完成：start_compass_brief → core.start
      ├─ 新增/修改要求：update_compass_requirements → core.start(revise=True)
      └─ 修改代码：收到有效 Brief 才放行
  → 本轮结束：有效性检查；需要时执行共享的提交检查
  → 保存任务状态，下一条用户消息继续用
```

### 补充输入与更新要求不是同一件事

普通追问走 `record_user_input()`，让维护模型知道用户在说什么，**不会自动改写 G/R/K**。

新增的 `update_compass_requirements` 只在界面模式注册，不接受模型自己编写的“新任务文本”。输入由接入层构造：原任务 + 按顺序保存的用户补充原文。它调用 core 原有 `start(revise=True)`，更新后维护一次并要求投递新的 Brief；旧证据不删除。

**是否属于“新增/修改要求”，仍由主模型按提示选择工具。** 这次没有新增一个语义分类模型，也没有保证模型一定会调用更新工具。如果模型漏调，用户原文仍会进入维护记录，但结构化要求不保证已更新。审核真实运行时，查看 `requirements_updated` 和随后 `brief_delivered`；本次离线测试不能证明真实模型的工具服从性。

更新工具要求单独调用。没有活动任务、没有新增输入、和其他工具并发调用时不会重建要求。更新一旦开始，先撤销编辑放行标记；更新失败或仅生成提取兜底结果时保持拦截。再次调用更新工具可以继续，不能用重新调用 start 工具绕过失败状态。

## 预算与提交检查

- 界面活动任务共用 **250 次主请求中间件尝试，无 Compass 总时间上限**。后续用户消息不刷新额度，自动重试重新进入中间件也计数。SDK 内部 HTTP 重发不另计入此数。
- **按用户最新决定，界面取消总时间上限。** 用户空闲和重试等待不会让任务过期；原生单次请求超时、取消与维护调用自身限制仍保留。独立评测仍为 250 次 / 11,400 秒。
- 切换模型或恢复保存状态，保留原任务、调用预算和用于审计的起始时间；新模型连接仍沿用 iCode 的选择。
- 首次有效交接后沿用原提交检查。之后每个有编辑尝试或成功要求更新的用户回合，最多安排一次同样的检查；至少剩余 15 次调用才继续执行（界面没有总时间截止）。普通提问/“继续”不会仅因新回合再安排一次检查。
- 同一个回合的自动重试不重复重置检查标记。后续回合再编辑才重新启用。
- 独立评测 CLI 的入口、参数、严格校验、一次性检查和主流程均保持不变。

## 失败为什么还需要一处 iCode 接口

新增恢复测试发现：Compass 在 backend 已经完成后直接抛出有效性异常，会跳过 iCode 正常的失败收尾，后续输入可能还被上一轮异常挡住。

现在转换为原生 `Failed` 结果，保留该次实际 `handle / usage / effects`，明确标记为终止性校验失败，不当成网络异常自动重试。`bindings.py` 增加一个 `report_policy_failure()`，用于 backend 已关闭后报告错误、设置失败状态，让原生 finalizer 正常结束该回合。没有重建原生 retry loop，也没有伪造成功结果或清零工具副作用。

这项修正针对**完成后的 Compass 有效性校验失败**；不把它描述成所有异常路径的全面改造。

## 本轮文件变动

| 文件 | 原因 |
| --- | --- |
| `src/compass/adapters/icode/interactive.py` | 跨用户回合保留任务；记录/去重用户补充；需求更新；失败恢复；状态版本 2，并兼容版本 1。 |
| `src/compass/adapters/icode/tools.py` | 界面专用的 `update_compass_requirements`。独立评测仍只有原 start/inspect 工具。 |
| `src/compass/runtime/prompts.py` | 增加界面多轮提示和需求更新的输入说明；原流程文本及两段提交提示未改。 |
| `src/compass/runtime/run_state.py` | 提供记录编辑尝试的小方法，界面可据此启动本轮提交检查；基础行为不变。 |
| `src/compass/adapters/icode/middleware.py` | 编辑 Guard 调用上述方法；原证据收集、命令分类和独立评测的放行规则不变。 |
| **`src/chrys/orchestration/engine/run/bindings.py`** | **本轮唯一额外修改的 iCode 原版 Python 文件**：增加完成后校验失败的报告接口。 |
| `tests/integration/compass/test_multiturn.py` | 本轮新增的 22 个离线测试。 |
| `test_interactive.py`、`test_review_fixes.py` | 两处原有直接调用适配器的测试，改为断言 `Failed` 结果和错误报告；不再期待绕过原生收尾直接抛异常。本轮未执行这些旧案例。 |
| README、TUI_REVIEW、REVIEW_FIXES、双语 settings 文档 | 删除“每条新消息就是新任务”的当前说明，保留历史验证记录并指向本文。 |

`src/compass/core/`、`ports.py`、`provenance.json`、依赖和独立评测入口没有修改。之前密钥修复仍保留。

## 本轮验证

只运行本轮新增文件，没有运行旧套件、Smart Test 或在线模型：

```bash
UV_CACHE_DIR=/tmp/compass-review-uv UV_PYTHON_DOWNLOADS=never \
uv run --no-sync --python .venv/bin/python \
pytest tests/integration/compass/test_multiturn.py -n0 -q
```

**22 passed**。包括：真实 iCode 主会话的流式/非流式多轮；本地/服务端历史重试；模型重建；原生 `/new`；跨轮预算；更新失败后的重试恢复；提取兜底仍拦截编辑；真实注入队列消费时捕获补充；自身/系统/合成消息过滤；版本 1/2 恢复；原生失败结果的用量和副作用保留；提交检查内的首次编辑不导致下一轮重复检查，检查内要求更新失败仍正确失败。

测试模型是离线 mock，不发生付费调用。本机 Python 3.14.0，原 pin 3.14.10 未动。新增测试通过不是全量回归结论，也不证明真实模型每次都能正确选择更新工具。修改文件另做 Ruff、格式和 diff 检查；冻结文件与本轮开始前逐文件哈希核对。

## 在 Downloads 里手动验证

关闭旧进程，从 `/Users/yisenxu/Downloads/iCode-brief` 启动 `uv run icode`；在 `/agents basic` 开启 Compass。

1. 给一个实际修复任务，观察探索、start 工具和 Brief。
2. 问“为什么这样改”，再输入“继续”：检查同一个任务目录、原要求仍在，调用数继续累加。
3. 补充一个具体新要求：观察 `update_compass_requirements`、更新后的 Brief，再修改代码；不应要求先重做整个探索。
4. 输入 `/new` 后给另一个任务：应创建新的任务目录。

任务证据在 iCode 会话目录的 `compass/<任务ID哈希>/`。`adapter-events.jsonl` 看 `user_followup`、`requirements_updated`、`brief_delivered`、`submission_check`；用户补充原文及预算随 `compass_interactive` 保存。task_id 保持稳定，invocation_id 随用户回合变化。上述手动步骤是验证建议，本次没有冒充执行真实模型验证。
