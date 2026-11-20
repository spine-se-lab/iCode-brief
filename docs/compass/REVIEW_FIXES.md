# Compass：密钥、只读回合与接入代码整理

最新修订：界面取消总时间上限、保留 250 次主请求；修复原生执行接口兼容性。当前验证和历史记录的区别见 [宿主回归修复审核](HOST_REGRESSION_REVIEW.md)。

**最新可读性整理：具名 Brief / 流程标志、类型标注与 9 项新增测试，见 [READABILITY_REVIEW.md](READABILITY_REVIEW.md)。下方流程和历史验证记录保留。**

日期：2026-11-19（Toronto）。分支：`codex/compass-modular-latest-20261119`。本地修改，未提交、未推送，未运行付费模型。

**最新修订：已实现活动任务跨回合延续。行为、文件变动与本轮 22 个新增测试见 [MULTITURN_REVIEW.md](MULTITURN_REVIEW.md)。下方 26/3 项测试为各自历史记录。**

## 这次修了什么

### 1. CLI 密钥不会再通过环境变量传给工具

原来：`--key-file` → 读出密钥 → 写入 `OPENROUTER_API_KEY` → iCode Shell 复制环境 → 测试脚本等子进程可读取密钥。

现在：模型配置只保存 iCode 原生 `{{!command}}` 凭据引用。HTTP 认证需要密钥时，宿主运行固定的 Python 凭据读取入口，密钥由原生凭据模块保存在内存中。Compass 不再把它写入环境、生成的 YAML 或运行结果。独立维护模型仍在内存里接收密钥，保持原 OpenRouter 请求参数。

- `adapters/icode/credentials.py`：生成带 Shell 引号和模板括号转义的命令；使用当前 Python 的 `-I` 隔离模式，避免任务目录/PYTHONPATH 影子模块；每次读取仍检查 owner-only 普通文件。
- `cli.py`：初始校验凭据，创建命令引用；删除 `os.environ["OPENROUTER_API_KEY"] = key`。
- `adapters/icode/openrouter.py`：模型配置接收凭据引用，不再固定环境变量引用。
- 读取失败不打印文件内容或 traceback；运行异常仍只保存异常类型。
- `--prepare-only` 不读取凭据、不调用凭据命令、不调用模型；未指定 key-file 的预检配置保留空凭据。

**修复边界：**这是环境变量继承问题的修复，不是 OS 沙箱。与宿主同一用户运行的任意命令，仍可能主动读取该用户有权访问的凭据文件；文件权限 600 不能隔离同一用户。用户在启动环境中自行导出的其他密钥也不会被本改动自动清除。不要据此宣称完整的秘密隔离或证明历史上没有发生泄露。

### 2. 只读／普通问答回合不再因没调用 start_compass_brief 报错

| 实际行为 | 界面结束处理 |
| --- | --- |
| 没启动、没尝试交接、没有被现有 Guard 识别到修改尝试 | 正常返回；记录 `compass_skipped`，原因 `not_started_no_edit_attempt`；无维护调用、无提交检查 |
| 调用了 start_compass_brief，但无探索证据等原因未启动成功 | 仍报流程无效，不能伪装成只读完成 |
| 尝试修改而被 Guard 拦截，随后直接回答 | 仍报流程无效，工具修改不会执行 |
| 已启动，但 Brief 没确认投递 | 仍报流程无效 |
| 交接、Brief、修改均正常 | 沿用原流程及一次提交检查 |
| 原生执行失败、取消、预算耗尽 | 保留原错误/取消，不转换为只读成功 |

新增 `handoff_requested` 和 `write_requested` 标记，并随原生会话保存、恢复；重建不清除已发生的尝试。旧状态缺少这两个新字段时默认 false，原来的 started/Brief/预算字段照常恢复。

“只读”放行是根据当前接入层观察到的操作决定，不靠提示词关键词判断，不是对任意 Shell 命令无副作用的安全证明。原 Guard 的命令分类策略未变。独立评测入口始终要求有效交接，不放宽评测口径。

## 代码怎么找

按这条线阅读：

```text
界面开关 → iCode builder → interactive.py → 原版 backend.run
                                            ↓
                               middleware.py 收集证据
                                            ↓
                         tools.py 的 start_compass_brief
                                            ↓
                              run_state.py 的 handoff
                                            ↓
                       core/session.py 及原核心维护逻辑
                                            ↓
                         middleware.py → 原生 injection.queue
                                            ↓
                      workflow.py 的共用结束与提交检查
```

| 原位置／名称 | 新位置／名称 | 职责 |
| --- | --- | --- |
| artifacts.py | provenance.py | 来源验证与清单序列化小函数；不再放预算 |
| artifacts.py 中的预算 | runtime/budget.py | MAX_CALLS、MAX_SECONDS 唯一来源 |
| contracts.py | runtime/records.py | 运行输入、输出、异常及后端接口，避免与任务约定混淆 |
| runtime/session.py / Controller | runtime/run_state.py / RunState | 单轮状态、预算、交接与审计 |
| backend.py 的工具定义 | adapters/icode/tools.py | 两种入口共同的工具注册 |
| backend.py 的 ICodeBackend | adapters/icode/headless.py / HeadlessBackend | 独立无界面宿主接入 |
| adapters/icode/model.py | adapters/icode/openrouter.py | 独立评测固定 OpenRouter 策略与维护传输 |
| runtime/submission.py 及流程、收尾、冲突提醒 | runtime/prompts.py | 集中提示词；原有字符串未改变 |
| storage.py 的 read_key | adapters/icode/credentials.py | 凭据读取与原生命令引用 |
| storage.py | 原位置保留 | 只负责原生安全文件存储适配 |

`InteractiveController` 同步更名为 `InteractiveRunState`。公开命令 `python -m compass`、工具名、Agent 开关、已有状态键和产物文件名不变。本地新模块的内部 import 路径已更新，旧模块路径不提供空转发壳。

### 消除重复，但不合并不同宿主的执行方式

`runtime/workflow.py` 提供 `require_handoff()` 和 `prepare_submission_check()`。CLI 和界面都调用它们；提交检查的 15 次剩余额度门槛、提示文本、一次性标记、审计和保存只有一份实现。CLI 仍用字符串调用 headless 后端；界面仍通过原生 RunRequest 继续任务。界面的未启动放行规则只在界面适配器里。

两套维护模型实现暂时保留：独立评测固定 OpenRouter 策略，界面沿用所选 profile。合并会牵涉请求参数、重试和审计差异，本轮不为了减少文件而改变它们。

## 没改什么

- 本轮 `core/`、`ports.py` 和 `provenance.json` 字节未改变。36 个冻结模块继续通过原哈希校验；两段原始提交提示词哈希不变。
- 没有新增或修改 iCode 原版 Python 文件；之前已有的界面接线改动保留。
- 没改原生 Shell、InjectionMiddleware、重试循环、审批机制和模型 SDK。
- 没改模型、reasoning、250/11400 预算、独立评测的重试配置与核心算法。
- 未更新依赖、Python pin 或用户日常配置；未重跑 benchmark。
- 先前审核记录的晚交接/200 步收尾提醒标记问题仍未修，本次不声称解决所有流程有效性问题。

## 验证（本轮）

只执行本轮新增文件 `tests/integration/compass/test_review_fixes.py`：**26 passed**。没有运行旧测试、Smart Test、完整套件或在线模型。

```bash
UV_CACHE_DIR=/tmp/compass-review-uv UV_PYTHON_DOWNLOADS=never \
  uv run --no-sync --python .venv/bin/python \
  pytest tests/integration/compass/test_review_fixes.py -n0 -q
```

覆盖：

- 假密钥走真实原生命令解析、凭据读取子进程、HTTP Auth（HTTP 使用内存 MockTransport）；认证内容匹配，但生成配置、运行产物和 CLI 输出不含密钥。
- 原生 Shell 环境构造器启动的子进程看不到该密钥环境变量；带空格、引号、模板括号和 Shell 字符的文件名正确传递。
- 凭据权限不合规时报错且不打印内容；异常信息含假密钥时 CLI 仍只输出异常类型；预检不读密钥。
- 真实 iCode 主会话离线流式/非流式：普通问答、只读探索、交接拒绝、修改拦截、Brief 未确认，以及完整流程。
- 只读回合断线后原生重试成功；尚未启动 Compass 的只读问答之后仍能正常开始代码任务；当时各轮隔离，最新活动任务跨轮行为见多轮审核文档。
- CLI/界面相同的提交提示、15 次门槛、一次性检查；CLI 继续拒绝没有交接的运行。
- 尝试标记经 JSON 保存/恢复后仍阻止错误放行；核心来源与提交提示哈希校验。

旧 Compass 测试的 import 路径已随重命名更新；两项“未交接必失败”的历史案例改成“尝试交接失败”，符合新界面规则，**本轮没有执行这些旧案例**。

本机验证环境 Python 3.14.0；仓库 pin 3.14.10 未动。新增测试通过不代表完成全量回归、真实供应商认证或真实模型服从性验证。

## 你可以怎么验证界面

关闭此前运行的 iCode 后，从 Downloads 这份仓库重新启动。给当前主 Agent 开启 Compass：

1. 输入“阅读 README 和启动入口，解释如何启动，不修改文件”：应正常回答，不因未交接报错。
2. 再给一个实际修改任务：应探索、调用 start_compass_brief、接收 Brief 后修改。
3. 会话目录的 `compass/<任务ID哈希>/adapter-events.jsonl` 中，只读回合应有 `compass_skipped`；正式启动的回合则检查 `brief_delivered` 与 `submission_check`。

这三步是人工验证建议，未由本次离线测试冒充执行。


## 审核后补充：导入遗漏与多轮任务（2026-11-19）

审阅者报告全部 Compass 测试 80 passed、2 failed。本轮确认 `test_interactive_retry.py` 中同一个函数的两种参数都引用了旧位置 `compass.provenance.MAX_SECONDS`，已改为 `compass.runtime.budget.MAX_SECONDS`。上次迁移遗漏了函数内部导入，这是本次代码整理造成的遗漏。

仅运行新增 `test_import_migration.py`：修复前 1 failed、2 passed，准确检出上述嵌套导入；修复后 **3 passed**。检查遍历源码和 Compass 测试内的绝对 Compass import，包括函数体内导入，并验证故意构造的旧路径会被拒绝。未运行旧测试体或全部套件，因此不把审阅者的全量结果改写成“全部通过”。`records.py` 的说明同步删除旧 contracts 命名。

**当时审阅提出的问题（现已按用户确认方案实现，见多轮审核文档）：**界面现在按 invocation 新建 Compass 任务，普通后续用户回合不会继承上一任务的 G/R/K。主模型的 iCode 历史仍在，不能说整个对话历史丢了；Compass 的结构化任务约定会重新建立。建议已启动任务跨回合延续，后续输入经核心已有 `record_user_input()` 作为补充，明确换任务时再重建。该接口只让维护看到补充输入，不会自动重建 G/R/K；真正变更任务约定须走已有 `start(revise=True)`，不能混为一谈。仅“第一次启动后永远不再处理”也不合适，会遗漏后续更正。

这段描述的是当时尚未实施的状态；后续用户已确认：新会话换任务、活动任务共享预算、后续编辑回合检查。实现详见 MULTITURN_REVIEW.md，core 仍未修改。只读放行仍依据观察到的操作，不保证模型绝不会主动调用交接工具。
