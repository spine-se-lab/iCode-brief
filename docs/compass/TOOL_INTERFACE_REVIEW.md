# Compass 三工具接入审核（2026-11-20）

最新补充：[独立持久化审核](PERSISTENCE_REVIEW.md)、[原文捕获与探索后更新审核](CAPTURED_REQUIREMENTS_REVIEW.md)。下文三工具接口已同步更新。

## 本次结论

Compass 在主会话中提供工具，不接管 iCode 的执行循环。`runner.py` 和 `bindings.py` 中先前加入的 Compass 分支已撤回，与本次迁移基线 main（`90a3fb99c4d4ab476bdaf595bdace183e54dd7df`）一致。相对于旧开发提交仍会看到删除 diff，那是在撤回旧侵入。

## 三个工具

| 工具 | 输入 | 行为 |
| --- | --- | --- |
| `inspect_compass_brief` | `refs`，如 `["goal", "R1", "K1", "C-001", "E-001"]` | 按 ID 查询；空列表返回 Brief。不调用模型。 |
| `update_compass_requirements` | 无参数，自动读取捕获的用户原文 | 第一次生成 Goal/Requirements/Constraints；探索后可再次更新。用户输入和仓库上下文都未变时跳过。 |
| `update_compass_brief` | 无 | 整理证据；若缺 Goal 或 Requirements，先用用户原文补生成，再让 core 判断维护条件。未达到时返回当前 Brief；达到时执行原维护流程。 |

用户原文由主会话 Extension context 回调捕获，后续回合的原文按顺序保留，重试按回合 ID 去重。模型无需转述任务参数。是否调用工具由主模型决定。工具异常使用原生工具错误返回，不额外把整个回合判为失败。

## 工具说明如何进入上下文

通过现有 `@tools.context` 返回以下三句话，由 iCode 原生 Extension context 在首次模型请求前提供；工具自身的参数说明仍由工具 schema 提供。原生 catalog 负责复用未变化的说明，不在每个模型请求上手工重复 append。这里不改 runner，也不注入新的执行门禁。

> Use inspect_compass_brief(refs) to look up Goal, Requirement, Constraint, Claim or Evidence by ID; pass [] for the current Brief. For coding tasks, call update_compass_requirements() initially to create requirements from captured user text, then again after repository exploration or new user requirements to update them. After collecting new evidence, call update_compass_brief() to check whether core policy calls for maintenance and return the current Brief; missing Goal/Requirements are initialized from captured user text first, but a Brief update is not forced.

普通问答不要求调用这些工具。说明只指导模型选工具，不强制接管回合。

本次只运行新增 `test_tool_usage_context.py`：1 项通过，验证第一次工具调用前模型已看到说明、后续回合不重复堆积相同说明，以及说明投递本身不调用维护模型。未运行旧测试。

## 实际调用顺序

1. 开启 Agent 的 compass Extension，宿主 builder 为工具提供当前选中的模型和只属于主会话的原文捕获回调；Compass 自己保存会话文件。捕获本身不调用模型。
2. agent 可以直接读文件、改代码、回答用户。没有先交接才允许修改的门禁。
3. 原生 `after_tool` 仅记录读写、搜索和 shell 的真实结果；不会因证据达到阈值自动调用维护模型。
4. 调用任一 Compass 工具时，把待处理记录写入 Evidence。查询不会提取任务或维护 Claim。
5. 开始任务时调用要求工具生成初版；探索后再次调用，使用相同用户原文与新增仓库上下文更新。仍走 `start` / `start(revise=True)`，去重键已改为用户文本与仓库上下文的组合。
6. 调用 Brief 工具，先确认 Goal 和 Requirements 均存在；缺任一项时复用要求工具的提取路径补生成，再使用原有触发器 replay/preview 判断。只有需要维护才调用 `maintain`。返回结果另含 `requirements_initialized`，区分补生成与 Brief 维护。
7. Brief 通过普通工具结果进入模型上下文。任务结束仍由原版 iCode 决定。

## 缺失要求的兜底初始化（最新补充）

Agent 漏掉 `update_compass_requirements()` 也能直接调用 `update_compass_brief()`：从已捕获的用户原文和实际仓库证据补生成 Goal/Requirements，再决定是否维护 Claims/Brief。两者已经完整时不会重复生成；只有 Constraints 而没有 Requirements 也不算完整任务。

显式要求更新与兜底生成共用 `_extract_requirements`，不重复实现提取逻辑。core 的去重判断进一步要求 Goal/Requirements 实际仍存在，避免旧缓存令缺失记录无法修复。不存在用户原文时返回明确工具错误，不编造任务；提取及原有 fallback 仍未形成完整要求时，不继续 Claim 维护。

查询 `inspect_compass_brief` 不触发生成、不调用模型。普通工具也不自动触发提取；兜底发生在 Brief 工具进入核心维护流程之前。

新增 `test_missing_requirements.py` 的 7 项测试通过，覆盖跳过要求工具（零证据与达到维护阈值）、缺 Goal、缺 Requirements、纯查询、无原文及来源校验。旧接口断言已同步新行为，但本轮只运行新增文件，没有运行旧测试或全量测试。

## 保留与边界

- core 仅 `session.py` 的要求提取去重键及其恢复/存档更新；其他 core 文件、ports、维护阈值不变。来源记录保留原始哈希，并登记这项授权变更。
- 仍需 `builder.py` 提供模型、资源关闭及主回合身份（旧状态导入已于 2026-11-23 删除）；新的持久化不再依赖它。`serializers.py` 的 Compass 字段已撤回。模型接线暂时保留，不能宣称完全无宿主改动。
- 被动记录使用队列，不占用正在运行的维护模型锁。记录失败保留宿主原始结果并写警告；不能把缺失记录当成完整证据。
- shell 退出码仅在宿主结果明确提供时记录；缺失时为 unknown，不假造成功。
- 原会话存档的 ledger 可迁移，旧的预算和交接标记不再控制工具。子调用不借用主会话状态。
- 不注册 `start_compass_brief` 到新的产品 Extension；独立评测入口仍保留该工具及严格流程。
- 旧 `interactive.py` 和旧包装器测试已于 2026-11-23 删除（见 [接线缩减审核](SMALL_HOST_SEAM_REVIEW.md)）；文中"旧 ledger 迁移"用例随旧 history 导入一起删除。

## 验证

本轮新增 `tests/integration/compass/test_tool_interface.py`：13 项通过。覆盖原生主会话三工具注册、不交接也能修改/结束、创建与更新、按 ID 查询、触发阈值与重复调用、存档恢复、原生重试、子调用隔离、记录失败保留结果、并发记录和旧 ledger 迁移。模型全部模拟，未发起付费调用。

变更代码的 lint、格式和类型检查通过；等待点清单仅删除已撤回的两个 Compass 等待点，保留原生执行等待点。

按约定未执行旧测试及全量测试，也未完成旧包装器测试的规格迁移。冻结复现实验分支未修改；本次未提交、未推送。
