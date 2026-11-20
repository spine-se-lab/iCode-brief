# Compass 结构迁移审核

**本页为历史迁移记录。当前修复、重命名和新增测试见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。**

后续已增加界面开关；本文件保留结构迁移的历史验证。本轮界面改动与新增测试结果见 [TUI_REVIEW.md](TUI_REVIEW.md)。

目标仓库：`spine-se-lab/iCode-brief`。基于 main `9228ec4af94bd6723ef9fb53503c1ea655fb4016`；当前分支 `codex/compass-modular-latest-20261119`。没有提交、推送或调用付费模型。

这次将原先的 `integrations/r5/` 接入包整理为 `src/compass/` 能力层，统一使用 Compass 名称。核心策略、模型参数、预算、重试、提交检查和结束逻辑保持原样。

## 建议审核顺序

1. `src/compass/ports.py`：核心所需的模型、存储和事件接口。
2. `src/compass/core/session.py`：原有需求、证据、判断和 Brief 的协调逻辑。
3. `src/compass/runtime/workflow.py`：启动、完成检查、一次提交检查及收尾。
4. `src/compass/adapters/icode/headless.py`：挂接当前 iCode 实例的工具和中间件。
5. `src/compass/adapters/icode/middleware.py`：工具结果配对、证据去重、编辑前检查、Brief 注入和主调用计数。
6. `src/compass/cli.py`：组装依赖；使用 iCode 的内置 Code 配置资源创建 CompassCode。

## 如何保证没有顺便改算法

- 原始 72 文件的来源与哈希清单保留在 `src/compass/provenance.json`。
- 真正运行的 36 个核心/接口模块搬入 `core/` 和 `ports.py`。调整 Python 导入命名空间和格式；另有下面单独列出的 Git 标准输入隔离。测试只排除这些明确变化，其余语法树与原文件逐一比对。
- 为满足主仓库架构规范，`cli.py` 显式传入原默认值 `surface=None`；`core/evidence/observation.py` 的 `git status` 显式使用 `stdin=DEVNULL`，避免读取宿主输入。Git 查询命令、输出解析及证据策略不变。迁移测试严格检查该参数值，来源清单单独记录这一处 I/O 调整。
- 两段提交提示词从原始源文件提取，字符串哈希不变。
- 其余 36 个原文件属于旧宿主适配或旧打包配置，当前 iCode 接入不使用它们，因此不放进运行包。迁移前副本保留在本地案例目录。
- 原适配层重命名和重新分目录。存储实现移到 iCode adapter，通过已有端口注入运行会话。
- 工具名称、保存状态的 `compass_brief` 键、模型配置 ID 保持兼容。Agent 展示名改为 CompassCode，运行记录的来源字段改为 compass_archive。

## src/compass 之外的改动

| 文件 | 原因 |
| --- | --- |
| `src/chrys/foundation/platform/__init__.py` | 保留上一版的独立配置目录支持，5 行逻辑与 1 行说明；本轮未改其行为。 |
| `pyproject.toml` | wheel 加入 src/compass；Ruff 将 compass 识别为本项目包。不改依赖和版本。 |
| `scripts/chrys_test.py` | 将测试匹配规则改指向 src/compass 与 tests/integration/compass。 |
| `tests/integration/compass/` | 迁移已有测试，增加核心迁移一致性和宿主依赖边界检查。 |
| `tests/foundation/platform/test_config_dir_override.py` | 保留上一版配置目录测试。 |
| `docs/en/reference/settings.md`、`docs/zh-Hans/reference/settings.md` | 更新 Compass 名称和文档链接。 |
| `docs/compass/` | 本审核说明和使用说明。 |

## 已知问题不在本轮修复范围

上一轮审阅发现：超过 200 步的收尾提醒与真实 Brief 共用投递标记，晚到的 handoff 可能误满足有效性标记。本轮按“保持原来的逻辑不变”保留该行为，不能把结构迁移当作该问题已修复。请求观察回调也只是本地待发送消息证据，不是供应商回执。

结构迁移当时仅提供独立 headless 运行；后续界面支持见 TUI_REVIEW.md。仍不提供 Docker/EvalHub 隔离或 verifier。算法一致不代表升级后的 iCode 与历史实验逐字节一致，也不代表通过率提升。

## 验证

本轮在 macOS、Python 3.14.0 上完成以下验证（均无付费模型调用）：

| 检查 | 结果 |
| --- | --- |
| Compass 行为、配置目录、核心迁移与依赖边界专项 | 17 passed |
| 上述专项 + 完整 architecture 套件，最终修正后 | **1,357 passed** |
| Smart Test：因 pyproject.toml 改动自动选完整非联网回归 | **40,192 passed、3 failed、135 skipped** |
| Ruff check / format | 通过；54 个文件格式合规 |
| 原版 src/chrys 类型检查 | linux / darwin / win32 均通过 |
| wheel 构建、隔离导入、内置配置资源与 prepare-only | 通过，model_calls=0 |
| 官方 check_wheel 安装检查 | 元数据/文档检查通过；完整安装阶段因本机缺 Python 3.14.10 未通过 |

全量回归的三项失败均在**未修改的 main 源码**上单独复现。对照使用 `git archive HEAD src/chrys` 得到的源码，通过 PYTHONPATH 加载，并断言实际 `chrys.__file__` 指向该副本：

| 测试 | 本次工作树与 main 对照的相同现象 |
| --- | --- |
| `test_explicit_dotfile_glob_syntax[{x,{.envrc,y}}]` | 本机 rg 拒绝嵌套 alternate glob，返回解析错误。 |
| `test_background_commands_a_sub_agent_started_stop_when_it_finishes` | 本地解释器绝对路径较长，命令摘要被截断，测试正则期望的 `time.sleep(60)` 不完整。 |
| `test_worker_death_is_noticed_while_a_forked_child_holds_its_pipes` | 工作进程退出、子进程持有管道场景超过测试 20 秒等待期限。 |

因此没有把完整回归写成“全部通过”，也没有为消除上述失败修改原版 iCode。仓库 `.python-version` 仍固定 3.14.10；尚需在该精确版本环境完成安装检查。隔离 wheel 验证使用当前环境已有依赖，不等同于干净环境安装验证。

可复核的主要命令（本机 uv 需显式 `--python 3.14 --no-sync`；标准环境按仓库 pin 安装）：

```bash
uv run pytest tests/integration/compass tests/foundation/platform/test_config_dir_override.py -n0 -q
uv run pytest tests/architecture -n4 -q -m 'not integration and not gc_calibration'
uv run ruff check src/compass tests/integration/compass
uv run ruff format --check src/compass tests/integration/compass
uv build --wheel
uv run python -m compass --prepare-only --directory /absolute/path/new-preflight
```

本轮没有重新评测 benchmark，也不把离线测试通过解释成通过率不变或提升。Downloads 副本用于人工审核，尚未 commit 或 push。
