# iCode 接线缩减审核（2026-11-23，第二次缩减后更新）

builder 中的 Compass 接线移入 `src/compass/adapters/icode/host.py`，并删除不再需要的旧路径。三个工具、gate、维护算法、持久化格式、UI 和模型参数不变。独立评测入口和冻结复现代码未修改。

## 改动量

比较对象是迁移时的 main 基线 `90a3fb99c4d4ab476bdaf595bdace183e54dd7df`，包含当前未提交工作区。

| iCode 原版文件 | 第一次缩减后 | 现在 |
| --- | --- | --- |
| `src/chrys/orchestration/engine/build/builder.py` | +16 / −0 | **+7 / −0** |
| `src/chrys/app/tui/screens/main/commands.py` | +9 | +13（Compass 导入改为可选） |
| `src/chrys/app/tui/screens/main/screen.py` | +13 | +13（未改） |
| `src/chrys/foundation/platform/__init__.py` | +6 | +6（未改） |
| 原版源码合计 | +44 / −0 | **+39 / −0，4 个文件** |

统计不包含 Compass 自己的代码、安装注册、测试、翻译及文档。`runner.py`、`bindings.py`、`serializers.py` 与 main 基线一致。

## builder 现在只做一件事

```python
if "compass" in profile.extensions:
    from compass.adapters.icode.host import prepare as prepare_compass

    await prepare_compass(
        active_profile, extensions, prepared, on_side_call_usage, lambda: executor.inputs.origin
    )
```

- **只在 Agent 启用 Compass 时导入。** builder 顶部不再导入 Compass；TUI 命令列表的导入也改为可选，未安装 Compass 时 iCode 仍可启动，只是没有 `/compass` 命令。
- **只传公开接口拿不到的 5 项**：实际选中的模型、Extension 装配结果（用于替换 context 回调）、资源所有者 `PreparedAgent`、维护用量回调、延迟读取的回合身份。
- 工作区、会话 ID 和会话数据目录改从公开 `TurnContext` 读取，不再由 builder 传入。

## 删除了什么

| 删除项 | 原因 |
| --- | --- |
| `validate_profile(profile)` 旧开关检查 | 旧 `metadata.compass_enabled` 只出现在未发布的本地版本。现在该配置视为未启用 Compass，不报错；需要时仍可运行 `python -m compass.adapters.icode.migrate`。 |
| 构建完成后的 `compass.bind(lambda: executor.backend.history_state)` | 它只用于从 iCode history 导入旧 `compass_interactive` 状态。serializer 撤回后该字段在保存时已被丢弃，这条路径实际走不到。会话注册合并进 `prepare`。 |
| `adapters/icode/interactive.py`、`scope.py` 的旧作用域、`tools.py` 的旧界面分支 | 旧的界面严格流程（交接门禁、包装执行、提交检查）已不再由 builder 接入，只剩旧测试引用。 |

**影响**：只存在于旧 iCode history 中的 Compass 状态不再自动迁入。仍支持 Compass 自己的旧 `tool-state.json` 导入（见 [持久化审核](PERSISTENCE_REVIEW.md)）。

## 现在的顺序

```text
iCode 准备模型与工具
  → Compass prepare：创建 ToolCompass、登记 aclose、替换 context 回调、注册会话
  → iCode 创建 reminders、Agent、executor
```

context 回调必须在 iCode 创建 reminders 之前替换，所以 `prepare` 仍在原位置。会话不再需要等 executor 完成：后续构建步骤失败时，`PreparedAgent` 会调用 `aclose`，撤销注册并关闭维护模型，`test_small_host_seam.py` 覆盖了这个场景。

## 仍然依赖宿主的部分

这是缩小宿主 diff，不是零宿主接线：

- **所选模型**：公开 `ToolContext` 不提供，维护调用需要它（对应宿主诉求 H5）。
- **原文捕获**：仍通过 `prepare` 包装 Compass 自己的 context 回调来拿回合身份；公开 context 被调用时，iCode 还没有设置调用身份。
- **资源生命周期**：关闭由 `PreparedAgent` 负责（对应 H1）。
- `/compass` 命令需要 TUI 两个文件的注册代码（对应 H8）。

## 验证

- Compass 测试 133 项、iCode 架构检查、构建与执行、子代理、TUI 主界面和扩展相关测试，合计 2,951 项通过（4 并发）。
- Ruff 检查与格式检查通过；独立评测 `--prepare-only` 正常。
- 等待点清单用原生生成器刷新，只有 builder 中这一处调用的表达式文本变化，分类不变。
- 未运行 iCode 全量测试。使用现有 Python 3.14.0 环境；上游声明的 3.14.10 未修改。更新在 Downloads/iCode-brief，未提交、未推送。
