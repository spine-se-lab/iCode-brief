# Compass 界面接入审核

最新修订：界面取消总时间上限、保留 250 次主请求；修复原生执行接口兼容性。当前验证和历史记录的区别见 [宿主回归修复审核](HOST_REGRESSION_REVIEW.md)。

**最新可读性整理：具名 Brief / 流程标志、类型标注与 9 项新增测试，见 [READABILITY_REVIEW.md](READABILITY_REVIEW.md)。下方流程和历史验证记录保留。**

**最新多轮修订：[MULTITURN_REVIEW.md](MULTITURN_REVIEW.md)。已启动任务跨回合延续，`/new` 才新建任务；本轮新增 22 项测试通过。**

**此前 2026-11-19 修订：只读／普通问答未启动 Compass 时正常返回；CLI 密钥不再进入进程环境。新增文件映射与 26 项新测试见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。下方早期测试数为历史记录，本轮未重跑。**

当前在 `codex/compass-modular-latest-20261119` 分支本地修改，未提交或推送。此前的 `src/compass` 分层保留。最新修订恢复 iCode 原生自动重试，覆盖初版“Compass 开启时禁用宿主重试”的设置。

## 怎么打开

1. 从这个仓库启动 iCode：`uv run icode`。
2. 输入 `/agents basic`，选择要使用的主 Agent。
3. 勾选 **Compass**，点击保存。默认不勾选。
4. 模型沿用 iCode 的选择。如果希望跟随会话里的模型切换，保留“Use active model profile”；也可以沿用原生的 Agent 模型覆盖设置。
5. 给主 Agent 一个代码任务。它先探索仓库，再调用 `start_compass_brief`，收到 Brief 后修改代码；第一次完成后，预算允许时自动执行一次原有提交检查。

这是原生主 Agent 的开关，保存为其 YAML 内的：

```yaml
metadata:
  compass_enabled: true
```

取消勾选会删除该项；关闭时没有 Compass 工具、维护模型调用或提交检查。外部 ACP Agent 不显示此开关。子 Agent 和 workflow node 不会自动启用 Compass；本轮接入主会话，不改它们的运行机制。

界面入口使用现有 iCode 配置目录。**不会设置 CHRYS_CONFIG_DIR，也不会创建或替换 CompassCode 模型配置。** `python -m compass` 的独立评测入口仍保留其原来的隔离配置方式。

## 模型怎么用

主请求直接使用 iCode 已经解析好的 `active_profile`。模型、端点、Key、请求头、reasoning、输出上限等沿用界面配置，不在界面接入中固定为 OpenRouter、DeepSeek 或 max。

Compass 的需求提取和维护也通过原生 `create_client(active_profile)` 创建独立连接：同一个模型与凭据，不要求第二份 Key。维护仍要求 JSON 对象、temperature=0、使用原维护输出预算。OpenRouter / DeepSeek 的维护调用关闭 reasoning；其他提供方的兼容性取决于其原生适配器及模型对这些参数的支持。本轮未调用在线提供方验证。

切换模型后，原生会话重建会关闭旧维护连接，并使用新 profile 创建连接。不会回退到固定模型。主请求保留界面流式设置，Brief 在请求前注入，完整响应结束后再记录响应证据。

## 保持与适配的边界

没有修改 `src/compass/core/`、需求提取提示词、Claim/证据维护算法、Brief 内容策略或两段提交提示词。

- 保留先探索、handoff、收到 Brief 后编辑的主流程。
- 保留界面每任务 250 次主请求中间件尝试、200 次后的收尾提醒；按最新决定取消界面总时间限制。独立评测仍为 250 次 / 11,400 秒。原生重试重新进入中间件时也消耗一次额度；两个请求观察回调不会重复计数。SDK 内部 HTTP 重发不进入这个计数。
- 已尝试启动 Compass 或尝试修改的回合，保留有效性检查及至少剩余 15 次调用的门槛；首次交接及后续发生编辑/要求更新的回合各最多一次提交检查。没有启动／尝试交接、也没有尝试修改的回合正常返回，只记 `compass_skipped`，不做维护或提交检查。独立评测入口仍严格要求完整 Compass 流程。
- Compass 开启时仍读取 iCode 原生 `effective_max_transient_retries()`，遵守 `llm.retry.max_transient` / `CHRYS_MAX_TRANSIENT_RETRIES`。界面默认值、退避、取消、流式超时回退和已提交工作保护均由 iCode 管理；SDK HTTP 重试沿用所选模型 profile。Compass 不另建重试循环。
- 已启动任务跨用户轮次延续，保留原要求、证据、预算和 Brief 投递标记；追问原文追加到维护。新增/修改要求使用界面工具 `update_compass_requirements`，新任务使用 `/new`。界面没有 Compass 总时间上限，空闲不会让任务过期。完整边界见多轮审核文档。
- 原生审批、工具配置、工作区、用户停止与模型切换仍由 iCode 管理。界面版不把审批切换为评测入口的 BYPASS，也不会把工具集改成评测固定四类。

因此，界面版复用了原核心和任务控制策略，但不是历史 benchmark 的完全相同运行环境。

原审核指出的“200 步后的收尾提醒共用 Brief 投递标记”问题仍保留；这次不同时改算法。请求观察仍只证明本地请求内容包含 Brief，不证明上游供应商接收。

## 自动重试：最新修订

**可以继续使用原版自动重试，不必额外填写 Compass 重试参数。** 在 iCode 将重试次数设为 0 时仍按 0 执行，Compass 不强行开启它。

例子：读文件、handoff 后，第 3 次请求携带 Brief，但网络断开。iCode 重发同一份注入文本；Compass 保留未确认的 Brief，收到完整最终响应后才确认投递。此时预算计数为 4，而不是回到 3，也不会因为两个请求观察回调变成 8。随后的一次提交检查仍使用同一个剩余预算。

| 状态 | 最新行为 |
| --- | --- |
| Brief | 请求观察只记录“该次请求携带了文本”；完整响应返回才更新投递标记。失败与未完成流不会确认。自动重试复用 iCode 的原文重放，不另外排入一份。 |
| 后台响应轮询 | 非最终响应不确认 Brief；轮询延续原请求的观察。轮询失败是否重试仍服从原生保护，不绕过已执行工具的限制。 |
| 调用预算 | 每次进入主请求中间件先检查、递增并保存。失败尝试计入上限；历史回滚不返还额度，序列化/重建后也保留。该数不等于 SDK 内部 HTTP 请求数或账单调用数。 |
| 总时限 | 界面取消 Compass 总时间上限；原生单次请求超时和取消保留，主请求仍累计到同一任务的 250 次上限。 |
| 证据与维护 | 保留成功响应与已经执行的工具证据，按原身份去重；不把未完成流当成证据。原生整个运行的重试会阻止重放已提交工具工作/已展示回答，因此没有新增一套 Compass 历史回滚。 |
| 任务失败后 | 原生重试结束才清理未确认草稿；不删除用户注入，也不清空证据和预算。后续主动 Retry 可重新生成未确认 Brief。 |
| 退出 | 达到预算、流程有效性失败、用户取消不会由 Compass 再次启动；已有输出和工具工作的重试保护保留。 |

投递确认仅代表本地观察到请求文本并获得完整响应，不等于证明模型理解了 Brief，也不是供应商实际接收参数的证明。

本次相对界面初版仅调整 **1 个 iCode 原版文件**：`builder.py` 去掉强制 `0`，恢复原配置读取。没有修改原生 retry loop、history rollback、injection 或模型 SDK。其余逻辑都在 Compass 的 `interactive.py` 和 `middleware.py`：共享中间件提取请求观察、投递确认、计数方法；界面子类延后确认并提前保存预算。独立 CLI 仍使用默认中间件，保持请求观察时确认的原行为。核心、提示词与依赖未修改。

## 原版 iCode 本轮改了什么

| 已有文件 | 改动原因 |
| --- | --- |
| `src/chrys/app/tui/screens/agents/panels/basic.py` | 在 Basic 页增加 Compass 复选框，读取开关值；遵循只读与 ACP 隐藏状态。 |
| `src/chrys/app/tui/screens/agents/config.py` | 保存/关闭开关时更新该 Agent 的 metadata，保留其他 metadata。 |
| `src/chrys/orchestration/engine/build/builder.py` | 仅在开关开启时组装 Compass 工具和维护连接；传入已选模型，并登记原生资源清理。 |
| `src/chrys/orchestration/engine/run/bindings.py` | 增加可选 Compass 主任务入口；关闭时直接调用原 backend。多轮修订另加完成后校验失败的报告接口，交由原生收尾。 |
| `src/chrys/orchestration/engine/run/runner.py` | 一行改为调用该入口，原来的 steer、结果记录和收尾机制保留。 |
| `src/chrys/service/state/serializers.py` | 在会话序列化白名单中保留 Compass 状态，避免重建/恢复时丢失预算和证据。 |

没有修改原模型客户端、工具实现、依赖或 Agent YAML schema。开关使用原有 metadata 保存能力；没有进程全局 monkey patch。上一轮的配置目录支持、打包与文档改动仍在，详情见 `REVIEW.md`。

## 新代码审核顺序

1. `src/compass/adapters/icode/interactive.py`：主任务创建、状态恢复、预算与提交检查、清理。
2. `src/compass/adapters/icode/interactive_model.py`：沿用所选模型的维护连接与参数。
3. `src/compass/adapters/icode/middleware.py`：新增可选流式响应记录；独立入口默认仍拒绝流式。
4. `src/compass/runtime/prompts.py`：界面与 CLI 共用的流程与提交提示；原字符串未变。
5. `tests/integration/compass/test_interactive.py`：本轮新增的离线测试。

## 验证记录

### 界面初版（历史记录，本次未重跑）

仅运行本轮新增文件：

```bash
uv run --no-sync --python 3.14 pytest tests/integration/compass/test_interactive.py -n0 -q
```

**16 passed**，包括真实 Textual 配置页保存、原生主会话的流式/非流式探索到提交检查、关闭开关无额外调用、模型切换后重建与关闭、维护连接选择、序列化后恢复，以及无 handoff、失败、取消和预算边界。

模型请求使用 mock，调用真实 iCode 组装与会话路径；未发送付费请求。按用户要求没有重跑旧测试、Smart Test 或全量回归。改动文件的 Ruff 与 diff 格式检查另行完成。测试环境是 Python 3.14.0，仓库原 pin 3.14.10 没有修改；不宣称完成精确版本的全量验证。

### 自动重试修订（历史记录，本轮未重跑）

仅运行本次新增文件：

```bash
uv run --no-sync --python 3.14 pytest tests/integration/compass/test_interactive_retry.py -n0 -q
```

**23 passed**。覆盖真实主会话的流式/非流式断线重试、服务端历史模式下的整个运行回滚、同一 Brief 重放、已执行工具不重跑、重试次数耗尽、失败后序列化重建、250 次预算退出、退避占用总时限、流式停滞回退、已展示回答不重放、用户取消、流程无效不重试，以及后台响应的轮询边界。

全部使用离线 mock 模型，未发付费请求。没有运行旧的 16 项界面测试、其他旧测试或全量回归。修改文件的 Ruff 和 diff 格式检查通过。Python 为 3.14.0；未修改原 pin 3.14.10，未做在线供应商或精确版本的全量验证。
