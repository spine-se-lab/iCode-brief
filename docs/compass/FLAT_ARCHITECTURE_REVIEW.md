# Compass 扁平分层审核 · 2026-11-22

这轮按 **adapter → gate → Compass 业务模块** 整理。业务模块直接并列在 `src/compass/` 下，不再套 `core/` 和 `runtime/`。目录迁移不改原来的维护算法。

## 每部分负责什么

| 位置 | 一句话职责 |
| --- | --- |
| `adapters/icode/tools.py` | 向 iCode 注册三个工具、使用说明和执行结果观察 hook。 |
| `adapters/icode/tool_session.py` | 识别当前主会话，捕获原始用户输入，把 iCode 工具结果转成普通观察数据，调用 gate，再把结果返回给 agent。 |
| `gate/controller.py` | 管理本次任务的要求和待处理证据，判断是否生成/更新要求、是否进入 Brief 维护。 |
| `gate/maintenance.py` | 原有触发机制：证据窗口、启动阶段、维护时机。没有改阈值。 |
| `task_contract/` | 从输入提取和保存 Goal、Requirement、Constraint。 |
| `evidence/` | 收集、整理、查询证据。 |
| `claims/` | 维护 Claim，处理合并、冲突和失效。 |
| `briefing/` | 选择内容并组织 Brief。 |
| `conversation/`、`model_calls/` | 原有对话记录和业务模型调用策略。 |
| `session.py`、`state.py` | 连接这些业务组件，保存/恢复它们的状态。不是另一层 harness。 |
| `persistence/` | Compass 自己的存档路径和 `session.json`。 |
| `ports.py` | 模型、事件、存储接口；业务不依赖 iCode 或 OpenAI SDK。 |

`evaluation/` 只存放原先独立评测用的预算、强制交接、提交检查。它是单独的入口，不在三个产品工具的调用链上。为了保留之前的评测能力，没有删除这部分代码。旧 `interactive.py` 包装器也不接入当前产品会话。

## 三个工具怎么走

### 1. 查询

`inspect_compass_brief(refs)` → adapter 确认会话 → gate 同步已收集的观察 → 业务模块按 ID 查询 → adapter 把记录返回给 agent。

同步观察只形成证据，不生成要求、不调用维护模型。传 `[]` 返回当前 Brief。

### 2. 生成或更新要求

`update_compass_requirements()` → adapter → gate 读取已捕获的原始用户文本和仓库探索信息。

- 没有完整 Goal / Requirements：调用原有提取逻辑生成。
- 有要求，但原文或仓库上下文变了：调用同一逻辑更新。
- 要求完整，原文和仓库上下文都没变：跳过提取，直接返回已有要求。

例如先说“修复空输入”，第一次生成要求；读完仓库后再调用，结合代码更新；不增加信息再调用一次，不重复调用模型。原业务 session 保留防御性的去重判断，供独立评测等其他入口使用。

### 3. 请求一次 Brief 流程

`update_compass_brief()` → adapter → gate：

1. 把排队观察记录成证据。
2. 检查 Goal 和 Requirements。缺任一项，先从原始用户输入生成；生成失败或仍缺项，则不进入维护。
3. 用原有 trigger 判断是否到维护时机。
4. 到时才调用原有 Claim 维护和 Brief 逻辑；没到时直接返回已有 Brief。
5. adapter 把 Brief、是否维护和判断原因作为工具结果反馈给 agent。

因此，“调用 Brief 工具”不是“强制更新 Brief”。正常的 iCode 执行、重试、写文件权限仍由 iCode 自己负责。

## 数据收集和存档

adapter 的观察 hook 只做转换和排队，不等维护模型完成。gate 在下次 Compass 工具调用时处理这些数据。用户原文按 iCode 回合 ID 去重；重试不会重复追加原文。

存档位置沿用 `<会话目录>/extensions/compass/session.json` 和 `ledger/`。以前文件里的 `core` 字段保留，以便旧存档继续恢复；它只是历史数据键，不代表目录层级。旧存档恢复不依赖宿主 serializer。最新存档也不会随宿主对话撤销自动回滚；同一存档目录仍按一个活跃写入者使用。

## 这次改动边界

- 本轮没有改动 `src/chrys/` 的任何文件；包括 `runner.py`。保留之前已经存在的模型和原文捕获接线。
- 36 个记录过来源的业务模块只迁移目录和导入路径，归一化 AST 校验与迁移前的记录一致；之前获准的 `session.py` 去重调整仍单独记录。
- 原维护时机、Claim 算法、评测预算和评测提示词均保留。
- gate 不导入宿主、适配器或评测代码；冷启动导入验证不加载 `chrys` 或 `compass.evaluation`。
- 已有产品测试的私有状态访问改到 `adapter.gate`；本轮只执行新增测试，没有跑旧测试。

## 验证

新增 `tests/integration/compass/test_flat_gate.py`，13 个用例通过：目录/算法校验、依赖边界、原生三工具调用、初次和探索后更新、缺要求时补生成、触发/不触发维护、原生重试和写文件、独立 gate、存档恢复、空输入拒绝、离线评测准备。

模型使用离线替身，没有产生付费调用。未运行全量测试或宣称性能提升。验证环境 Python 3.14.0；仓库要求的 3.14.10 固定值未改。代码在 Downloads 仓库中供审核，本轮未提交或推送。
