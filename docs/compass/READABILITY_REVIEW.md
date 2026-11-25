# Compass 接入层可读性整理

最新修订：界面取消总时间上限、保留 250 次主请求；修复原生执行接口兼容性。当前验证和历史记录的区别见 [宿主回归修复审核](HOST_REGRESSION_REVIEW.md)。

2026-11-19（Toronto）。本地分支 `codex/compass-modular-latest-20261119`，供审核；未提交、未推送。最新任务流程仍以 [MULTITURN_REVIEW.md](MULTITURN_REVIEW.md) 为准。

## 这次主要变得哪里好读

**运行方式没有换，换的是表达方式：原先靠记位置和猜类型，现在直接看名字。**

原来：

```python
self.pending = (text, facts, conflicts, reason)
self.pending[0]
owner.handoff_seen
```

现在：

```python
self.pending = PendingBrief(text=text, facts=facts, conflicts=conflicts, reason=reason)
self.pending.text
owner.flags.handoff_seen
```

- `PendingBrief` 在 `adapters/icode/middleware.py`。四个字段直接写明待发送文本、对应的维护事实、冲突集合、发送原因。保留同一条记录的对象身份，界面重试仍依靠身份确认投递；没有改成每次重试重造一份。
- `WorkflowFlags` 在 `runtime/run_state.py`。七个标志集中定义默认值和一句话含义，不再散落在初始化语句和另一份字段名列表里。
- `InteractiveRunState` 保存用 `asdict(flags)`，恢复按同一个 dataclass 的字段读取。旧存档中的 `flags` 对象及字段名不变，版本 1/2 继续支持；缺少的标志仍默认 false。
- 接入层和 runtime 补上参数、返回值、宿主对象、消息与回调类型。将 `c` 换成 `run_state`，展开多属性的一行赋值。`owner` 保留为“拥有这个中间件的运行状态”，现在由 `RunState` / `InteractiveRunState` 类型说明，不再靠猜。

## 七个标志分别是什么

这些标志记录不同事实，不是七个互斥阶段，不能简单合并成一个 `phase`。

| 标志 | 意义 |
| --- | --- |
| `started` | 核心需求提取和初始维护已完成。需求更新失败时会撤销。 |
| `handoff_seen` | 当前交接或需求更新对应的投递标记已确认。沿用原投递规则及其已知限制。 |
| `rules_seen` | 提交规则至少投递过一次，不需每次重复附加。 |
| `closing_seen` | 超过 200 次请求后的收尾提醒已投递。 |
| `check_done` | 本次提交检查已处理，包含预算不足而不继续调用的情况。 |
| `handoff_requested` | 曾尝试启动 Compass；即使被拒绝，也不能按普通只读问答放行。 |
| `write_requested` | 曾尝试编辑；即使被 Guard 挡住，也不能按普通只读问答放行。 |

例如：已经调用 start 但 Brief 尚未确认时，`started=True`、`handoff_seen=False`；编辑仍被挡住。普通问答三个事实可以同时为 false。这里保留原组合含义，没有新增状态机或偷偷修改有效性标准。

`requirements_pending`、任务身份和检查所属回合等界面专用状态仍由 `InteractiveRunState` 管理，没有混进独立评测入口。

## 保存为什么没有全部改成自动序列化

运行对象里还有客户端、锁、事件接口和宿主绑定，不能整个 `asdict(RunState)` 写盘。这次只把七个纯数据标志自动保存；任务、预算、用户补充和核心快照仍保留明确的边界。

新增完整 JSON 往返测试检查这些字段：任务身份、调用数、原始墙钟起点、用户补充、需求更新进度、检查回合、投递标记、证据去重和冲突。它能检测已覆盖字段的遗漏，不宣称以后加任意字段都能自动发现。

## 投递为什么仍保留一个界面子类

这里不再增加一套组合框架。共用部分和差异如下：

| 方法 | 共用 `BriefChat` | 界面 `InteractiveBriefChat` |
| --- | --- | --- |
| `process` | 收集证据，判断需要哪条 Brief，排入原生注入队列 | 先补充用户输入与维护，然后调用共用流程 |
| `observe_request` | 独立评测沿用原规则：看到请求含该文本即确认 | 只记下这次请求观察到的 `PendingBrief`，不立即确认 |
| `start_request` | 检查预算、计数、写审计记录 | 再保存任务状态，避免原生重试回滚丢掉失败尝试 |
| `record_response` | 记录完整响应、工具证据与用量字段 | 未结束的轮询不确认；完整响应对应同一条记录才确认，再调用共用记录逻辑 |

读代码可按：`PendingBrief` → `BriefChat.process` → `observe_request` → `confirm_delivery` → 界面子类的差异。没有新增中间件挂载点、重试循环或宿主历史回滚。

## 文件改动

| 文件 | 改动 |
| --- | --- |
| `runtime/run_state.py` | `WorkflowFlags`、运行状态类型、展开初始化赋值。 |
| `runtime/workflow.py` | 标志访问改为 `.flags`；变量名和类型；原流程顺序及预算门槛保留。 |
| `runtime/records.py` | 给校验、结果输出补返回类型；原来的结果校验仍执行。 |
| `adapters/icode/middleware.py` | `PendingBrief`、具名字段、消息/回调类型、展开局部变量。 |
| `adapters/icode/interactive.py` | 同步使用具名投递记录、统一 flags 保存/恢复；宿主类型和用户补充的数据结构。 |
| `adapters/icode/headless.py` | 宿主/运行状态类型与清晰赋值；原挂载顺序保留。 |
| `adapters/icode/tools.py` | 明确工具入口接收独立运行状态或界面控制器；工具名称和参数不变。 |
| `adapters/icode/interactive_model.py`、`openrouter.py` | 维护模型输入输出、profile、用量回调类型；模型请求参数不变。 |
| `adapters/icode/credentials.py` | 密钥读取入口的路径与返回值类型；认证逻辑不变。 |
| `tests/integration/compass/test_readability.py` | 本轮新增测试；包含小范围单元检查和两项原生宿主流程检查。 |

旧的五个 Compass 测试文件仅机械更新 `.flags` 访问，未修改原断言目标、放宽阈值或删除测试。本轮未执行其测试体。

无额外依赖，无新生产目录；**没有修改本轮开始时的 core、ports、来源清单、提示词文本或 iCode 原版文件**。无效的内部调用增加了清晰错误：例如没有 pending Brief 却调用确认，仍然失败，但现在明确报 RuntimeError；不影响正常运行分支。

## 验证及边界

只运行新增文件：

```bash
UV_CACHE_DIR=/tmp/compass-review-uv UV_PYTHON_DOWNLOADS=never \
uv run --no-sync --python .venv/bin/python \
pytest tests/integration/compass/test_readability.py -n0 -q
```

**9 passed**：

- 七个标志独立、字段名固定，不同实例不共享状态。
- 七个标志的 128 种组合，在版本 1/2 存档中全部往返一致；旧字段缺省继续为 false。
- 完整任务快照经过 JSON 编解码、恢复、再保存保持一致。
- 独立入口和界面各自保留原来的 Brief 确认时机，轮询不提前确认。
- 有效性判断、低预算不追加检查、250 次预算退出保持原含义。
- 真实 iCode 主会话（离线模型）流式/非流式：断线重试、追问、需求更新、修改和提交检查仍使用同一任务及预算。

另通过接入层/runtime 范围的 `ty check`、Ruff、格式和 diff 检查。冻结文件按本轮开始前哈希核对。旧测试只做访问路径迁移的 AST 比对，没有执行旧测试套件、Smart Test 或完整仓库类型检查。Python 3.14.0，原 pin 3.14.10 未动；没有在线模型调用。

宿主内部 `LoadedAgent` 和有序 middleware 列表的依赖仍存在；类型标注把这个假设写清楚，不会把它变成公开稳定插件 API。未来升级 iCode 仍需验证接入。核心长函数、原有算法及投递规则的已知限制均留待专门版本处理。
