# Compass 统一信息入口审核 · 2026-11-23

本轮把已有 context 和工具 hook 的信息接到同一个 adapter 入口，未增加全局 middleware，未改动 iCode 原版代码。Gate 的要求生成/更新判断、Brief 触发机制和业务算法都保持原样。

## 调用链

```text
iCode 每回合 context ── 用户原文、会话和工作区信息 ─┐
iCode after_tool hook ─ 工具参数、结果、失败、耗时 ─┼→ adapters/icode/information.py
既有 builder 接线 ──── 调用身份、所选模型信息 ────┘
                                                    ├→ adapter-input.jsonl：观察记录
                                                    └→ gate：用户输入 / 已适配的工具观察
                                                          ↓
                                             原有要求、证据、Claim、Brief 逻辑
                                                          ↓
                                               工具结果返回给 agent
```

`tool_session.py` 保留主会话查找、模型资源和生命周期管理；信息转换统一放到 `InformationInput`。`tools.py` 不再提前过滤工具类型和拒绝状态，由这个入口明确记录它们如何处理。

## 信息是否对齐

| 信息 | 实际来源 | 现在如何处理 |
| --- | --- | --- |
| 用户原始输入 | `TurnContext.prompt` | 保留原文，按原生 invocation ID 去重后送 gate；观察日志记录每次回调，所以重试可能有重复日志，但不会重复增加任务要求。 |
| 会话、Agent、工作区 | `ToolContext.session_id / agent_name / cwd / workspace_dirs` | 保存到观察记录；证据的工作目录使用当前调用 context。 |
| 本回合身份 | 已有 `InvocationOrigin` 接线 | 保存 kind、session_id、invocation_id、attempt 和 parent；证据补上 invocation_id，不再留空。 |
| 工具身份和输入 | `ToolCall.name / kind / call_id / arguments` | 保留字段和原始参数；已适配工具继续送 gate。 |
| 工具输出和状态 | `ToolResult.text / failed / rejected / duration_ms` | 全部记录，包括此前丢弃的耗时和拒绝状态。耗时用于审核，不进入维护判断。 |
| shell 初始工作目录 | `ToolCall.arguments["workdir"]`，未指定时用 context.cwd | 使用 iCode 自己的路径解析函数，与 shell 一致。修复之前错误读取 `working_dir` 的问题。命令内部再次 `cd` 不在这个字段的含义内。 |
| shell 退出码 | 结果文本尾部 `[exit_code: N]` | 保存数值及来源 `result_text_suffix`；不存在时保存 null / `not_provided`，不补 0。 |
| 当前模型身份 | 已有 builder 传入的 model profile | 只记录 profile_id、provider、model_id。不记录 key、请求头、endpoint 或完整配置；这也不是服务商实际请求参数回显。 |
| Brief 反馈 | Compass 工具返回值 | 仍通过 iCode 工具结果进入对话；after_tool 可记录返回文本，但它不作为新的仓库证据。 |

## 不同类型如何处理

| 类型 | 保存观察 | 送入现有证据算法 |
| --- | --- | --- |
| 已执行的 read / write / search / shell | 是 | 是，包括失败结果；失败不等于没有发生调用。 |
| MCP、其他 Extension、网页工具、未知或空 kind | 是，标记 `unmapped_tool_kind` | 暂不送入。当前没有这些类型的语义适配，不能拿任意文本当作读文件或测试成功。 |
| 被拒绝执行的工具 | 是，标记 `rejected_not_executed` | 不送入，避免把没执行的动作算成仓库变化。 |
| Compass 自己的查询/更新工具 | 是，标记 `compass_feedback_not_repository_evidence` | 不送入，避免 Brief 自己成为自己的新证据。 |
| 子代理、workflow node、side question | 本入口不收集 | 保持此前主会话隔离；它们的普通工具照常执行。 |

这里的“各种类型”指 iCode 确实送到当前主会话 after_tool 的调用。不是所有模型供应商内部动作，也不是一份完整执行轨迹。

## 三个容易理解的例子

1. **在子目录运行测试**：参数是 `{"command":"pytest","workdir":"backend"}`。之前 adapter 看错参数名，可能记成项目根目录；现在按宿主规则记成 `<workspace>/backend`，同时记录工具 ID、回合 ID、结果和耗时。
2. **MCP 返回一段资料**：现在不会直接丢掉，能在观察日志看到是哪次调用、输入是什么、返回了什么；但在没有 MCP 语义适配前，不会自动算成“测试通过”。
3. **写文件被用户拒绝**：日志保留 `rejected=true` 和原因文本；gate 不会收到“文件已修改”的证据。

## 保存位置和边界

会话自己的 Extension 目录中新增 `adapter-input.jsonl`，与 `session.json` 同级。复用 iCode 的 owner-only append 文件接口。日志里的 `session_data_dir` 用相对值 `.`，复制会话后仍指向本目录；其他 cwd 和工作区路径是当时观察到的历史值。

每行带 schema version、UTC 观察时间、来源、身份和对应字段。观察时间是 adapter 收到信息的时间，不是工具启动时间。日志只追加，不在启动时回放，因此不会重复增加证据；它跟随会话目录保存，本轮没有加入轮转或独立保留策略。日志损坏的尾行也不会被当成任务状态读取。

`evidence_eligible` 表示该工具类型/状态允许进入证据流程，不代表后续存档成功或测试通过。实际待处理数据仍以 `session.json` 为准。观察日志写入失败时仅记录错误类型警告，不替换宿主工具结果；任务账本自己的保存错误仍按原有路径报告。采集不等待维护模型锁，也不调用模型。

模型配置只取三个身份字段；工具参数和结果仍是宿主提供的文本，可能包含任务本身的敏感内容，观察日志与会话数据使用同样的私有保存边界，不会自动追加给模型。现有查询工具继续查询 Goal/R/K/C/E，不新增日志查询工具。

## 缺的接口单独列出

| 希望宿主提供 | 为什么现有公开 Extension 不够 | 本轮处理 |
| --- | --- | --- |
| `ToolContext / TurnContext` 中公开调用身份和稳定回合 ID | 只有 session_id / agent_name，无法稳妥区分主调用、子调用及重试归属 | 沿用既有内部 InvocationOrigin 接线，没有新增 host 修改。 |
| 当前有效模型的受管 side-call 接口 | 公开 context 没有界面当前选中的模型，也不管理模型客户端关闭和用量 | 沿用已有 builder 模型与生命周期接线；不自行猜模型或复制凭据。 |
| ToolResult 的结构化 metadata | 公开字段没有独立 exit_code、timeout、后台任务状态、附件信息 | 仅解析明确的退出码后缀；其他字段未知，不从 failed 推测具体原因。 |
| 可由 Extension 注册的模型请求/响应观察 hook | iCode 内部有 ChatMiddleware，但公开 Extension 没有注册入口 | 不自动收集 agent 的解释、假设、最终回答，不更改 middleware 链。 |
| 对 provider-hosted 工具、参数校验失败等统一事件 | 这些操作没有经过当前工具 hook | 不宣称完整捕获。 |

## 已有但本轮没有启用的接口

- `before_tool`：能看执行前意图，也能改参数/拒绝执行。本轮只需要执行结果，不加入执行前门禁。
- `ToolContext.data_dir`：用户级目录确实可用，但任务记录使用会话级 `session_data_dir`，不主动创建用户全局数据目录。
- 内部 ChatMiddleware / FunctionMiddleware：有能力观察各自调用，但不是公开 Extension 的通用注册入口，本轮不接入。
- 主模型 token、推理参数的实际转发情况：这些不是 context/tool hook 字段；模型身份不能代替请求日志或供应商回显。

## 修改文件和验证

- 新增 `src/compass/adapters/icode/information.py`：统一校验、字段转换、观察记录、证据路由。
- 修改 `tool_session.py`：原文和工具结果都交给统一入口。
- 修改 `tools.py`：after_tool 不再丢弃其他工具类型/拒绝结果；保留子调用隔离和异常时原结果。
- 新增 `tests/integration/compass/test_adapter_information.py`：24 个用例通过，涵盖原生回合/工具链、字段映射、不同 kind、拒绝状态、工作目录、未知退出码、身份隔离、重试、恢复、日志失败及模型凭据不进入记录。
- 一处旧测试的直接调用补上原生调用身份，只修改、未执行。
- 只跑上述新增文件，未运行旧测试或全量测试；无付费模型调用。Ruff 与修改的 adapter 类型检查通过。
- 本轮开始前后的 `src/chrys/` 文件哈希保持一致；Gate、业务模块和 provenance 未改。旧分支已经存在的 builder 接线保留，所以这仍不是“相对 upstream 零改动”。
- 使用现有 Python 3.14.0 环境，未更改上游 Python 3.14.10 固定要求；未提交或推送。
