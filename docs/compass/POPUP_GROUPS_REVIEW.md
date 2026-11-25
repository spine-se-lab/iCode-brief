# Compass 分组弹窗审核（2026-11-23）

## 展示依据

参考 CodeHelix-Plugin 的 `plugins/compass-brief/docs/compass-sidebar-zones-design.md` 当前交付、0.2.4 和 0.2.5。采用同一套分组和详情导航，将承载容器换成 `/compass status` 弹窗。撤下之前 Overview / Records / Current Brief 三页签。

- 八组：Goal、Requirements、Constraints、Claims、Tensions、Already read、Weak coverage、Evidence status。
- 默认仅展开 Goal；标题含计数，鼠标或 Tab / Enter 切换，最多展开一组，可以全部收起。空组显示解释，正文占剩余高度并滚动。
- 摘要最多两行原文，选择只强调标题。点击或 Enter 打开完整详情；支持组内 Previous / Next、关联跳转、Back 和 Esc。关闭后回到原分组和选中条目。
- R/K 展示已有覆盖级别和关联 Claim；Claim 展示 grounding、verdict、合并、冲突和证据引用，并能查看记录中的 Evidence 摘录。关联不等于需求已满足。
- 底部保留状态、Maintain、Preview Brief、Close。状态可点击/Enter 查看等待原因、游标和错误；Preview Brief 为独立只读弹窗。
- Maintain 复用现有 `/compass` 路径和忙碌检查；完成后关闭结果窗，回到刷新后的分组弹窗，保留有效分组与选中位置。

## 数据和边界

`commands.py` 的只读快照携带既有领域数据；`tui/presentation.py` 复用核心的覆盖、tension、已读和 evidence status 投影。`details.py`、`zones.py` 由参考实现适配而来；`status.py` 负责弹窗组装，`command.py` 接现有维护入口。所有改动在 Compass 和测试/文档内，没有改 iCode 宿主、runner 或核心算法。

状态窗口是明确标记 revision 的快照，不以定时器后台轮询；打开和从子弹窗返回时读取当前状态。详情停留在打开时的 revision。Maintain 显式触发模型工作；浏览和预览不处理排队观察、不写业务状态、不调用模型、不标记 Brief 已投递，也不重新执行测试。数据来源局限于已记录轨迹，不能据此宣称监控到外部文件改动。

## 验证

仅执行本轮新增 `tests/integration/compass/test_popup_groups.py`，覆盖纯投影、100×38 与 60×25 窗口的折叠/导航/返回/预览、空组/忙碌禁用/快照刷新、真实宿主测试夹具的 status→Maintain→刷新返回。全程离线模拟模型。

旧 `test_commands_status.py` 中针对三个页签的断言同步改为分组与预览断言，但未运行旧测试文件。使用现有 Python 3.14.0 环境、`uv run --no-sync`，未改变项目解释器或安装环境。新增测试结果：5 passed。Ruff、format 和本轮 adapter 文件 ty 类型检查全部通过。实际 Textual 渲染已导出并检查：[分组弹窗预览](POPUP_GROUPS_PREVIEW.svg)。

## 人工验收

重启 Downloads/iCode-brief 的 iCode，恢复已有 Compass 会话，输入 `/compass status`。依次打开 Requirements、Claims；选条目 Enter，查看关联，Back / Esc 返回。点击 Preview Brief 核对正文，点击 Maintain 运行既有维护，关闭结果后确认状态刷新。
