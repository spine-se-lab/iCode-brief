# Compass Extension 接入审核（2026-11-20）

## 这次改了什么

把 Compass 的开关、工具装配、编辑前检查、稳定流程提示和会话制品目录接回 iCode 已有的 Extension 机制。用户在智能体的 **Tools → Extensions** 中勾选 `compass`；不再使用 Basic 页的专用复选框。

本次以已有 Compass 提交 `b8e6ad2eec171ac9748791560aba3b8384ac6d20` 为起点，合入已核实的 main `90a3fb99c4d4ab476bdaf595bdace183e54dd7df`。上游新增的界面过程折叠和实时摘要保留。Extension 接口在原基线就已存在，并非这五个新提交才引入。

这不是完全无侵入的独立插件：动态 Brief、主会话运行策略、维护模型和状态保存仍需要当前宿主的内部接口。没有新增通用插件框架，没有删除原来的交接校验或提交检查。

## 怎么启动和开启

在仓库目录执行 `uv sync --locked`，然后 `uv run icode`。更新后需要同步安装元数据，否则新的 Extension 入口不会被发现。

打开 `/agents tools`，选择当前主智能体，在 Extensions 中启用 `compass` 并保存。也可以在该 Agent 的 YAML 中写：

```yaml
extensions:
  - compass
```

这是每个 Agent 的设置，不是全局设置。沿用界面选定模型、审批及原生重试。同一任务跨轮累计最多 250 次主请求，没有 Compass 总时间上限；`/new` 开始新任务。独立 `python -m compass` 仍是 250 次 / 11,400 秒的专用评测入口，不受 UI 开关支配。

本轮没有改写用户实际配置文件。旧配置若有 `metadata.compass_enabled: true`，启动会明确提示迁移，避免后台仍开启而 UI 显示关闭。用真实 Agent 配置路径替换下面的占位路径：

```bash
# 只预览，不修改文件
uv run python -m compass.adapters.icode.migrate /absolute/path/agent.yaml
# 确认预览后，原子更新此文件
uv run python -m compass.adapters.icode.migrate /absolute/path/agent.yaml --apply
```

迁移保留其他配置键和已有扩展，删除旧开关键；只有原值为布尔 `true` 才加入 `compass`。重复执行不会重复加入。YAML 会重新排版，原注释不保留。外部 ACP 配置不能迁移成 Compass 主会话。

仅支持原生主会话。对子代理、workflow 或 side-question 不建立共享账本；它们的普通工具绕过 Compass 主会话交接检查，继续遵循宿主原生审批及其他扩展规则。误调用 Compass 工具仍明确拒绝，不会借用父任务状态。

## 从启动到结束

1. 安装元数据的 `chrys.extensions` 入口找到 `tools.py:extension`。它声明三个工具、一个 context 和一个 before_tool hook，不保存共享会话运行状态。
2. iCode 按 Agent 的 `extensions` 装配工具、context 和 hook。builder 确认装配成功，从已绑定的公开 context 取得 `session_data_dir`，建立这个主会话的运行实例，并交给宿主管理关闭。
3. 每次主调用进入 `InteractiveCompass.run()`，临时绑定当前 invocation。工具调用同时核对 session 和 invocation 身份。另一个会话、子调用和已退出调用的异步副本不能复用它。
4. 固定流程说明由 Extension context 提供，由原生 catalog 去重和重投递，不再手工追加 `compass-workflow`。证据捕获仍走原路径；模型单独调用 `start_compass_brief`，原 core 提取任务并维护 Brief。
5. 每次模型请求前按 revision 和冲突决定是否投递；界面模式仍在完整响应后确认。本轮没有把它降级成每个用户回合只注入一次。
6. 主会话编辑前走原生 `before_tool`；没有确认 Brief 的写入被拒。审批修改参数后也会重检查。被拒调用不会成为执行证据。原生重试、交接校验、提交检查、尾部维护和保存继续执行；退出后撤销该 invocation 的路由。

会话状态键仍是 `compass_interactive`。普通主会话的制品根目录改为公开 `ToolContext.session_data_dir`，当前宿主将它定位到 `<session>/extension-data/compass`；每个任务再按 task ID 的哈希分目录。

恢复旧 v1/v2 checkpoint 时，继续从宿主快照恢复预算、交接确认、revision、已见证据和用户要求。冻结 core 原有的恢复函数只复制被引用的、通过安全读取检查的 raw evidence；原 `<session>/compass` 目录及旧审计文件保持原样，不做全目录移动，也不把旧审计日志当新执行证据。目标证据不一致会拒绝覆盖。

**不把“迁目录”当成迁 checkpoint。** 保存/恢复仍与宿主会话状态桥接，不能仅凭新目录的最新 JSON 恢复一段旧对话。

**独立评测入口另列。** `python -m compass` 保留原来的 Guard middleware、稳定提示及用户指定制品目录，仍用共用的同一编辑策略函数。此次 public hook/context/目录迁移针对普通原生主会话，未改评测协议。

## 两个确定问题的修复

**收尾提醒不是 Brief。** 原来超过 200 次后的 closing 提醒可能走同一个确认函数，误推进 handoff 和 revision。现在投递信封明确区分 `brief` / `closing`；closing 只记录自己的投递。晚于第 200 次才交接时，先送真实 Brief，再送收尾提醒。关闭提醒不能单独让严格校验通过。

**审计追加使用平台存储接口。** `runtime` 不再直接依赖 `os.O_NOFOLLOW`。它调用存储适配器的 `append_text`，由 iCode 的安全文件函数处理文件所有权、权限、链接检查及 Windows 路径。冻结的 `ports.py` 未修改，追加能力放在运行层接口。这里是复用跨平台实现，不等于做过 Windows 实机验证。

## 还改着哪些原版源码

相对 main `90a3fb99`，`src/chrys` 内剩 5 个文件，共增加 57 行、删除 1 行。上游 main 自身更新不算 Compass 改动。

| 文件 | 保留原因 |
| --- | --- |
| `foundation/platform/__init__.py` | 独立评测可用 `CHRYS_CONFIG_DIR` 隔离配置；未设置时保持原行为。 |
| `orchestration/engine/build/builder.py` | 检查启用/迁移/装配结果，取得扩展制品目录、创建当前主会话实例，提供所选模型与生命周期。 |
| `orchestration/engine/run/bindings.py` | 持有可选运行策略并报告策略失败。 |
| `orchestration/engine/run/runner.py` | 保留主调用包装入口及原 executor 兼容路径。 |
| `service/state/serializers.py` | 保存/恢复已有 Compass 状态键。 |

`app/tui/screens/agents/panels/basic.py` 和 `app/tui/screens/agents/config.py` 已恢复到上述 main 内容。除此之外 `pyproject.toml` 注册 Extension 入口，wheel 仍一起包含 iCode 和 Compass；翻译清单移除了已删除的专用开关文字。

本轮 Compass 新增 `scope.py`（当前主调用路由）和 `migrate.py`（显式旧配置迁移），修改工具/context/hook 声明、交互接线、投递确认和审计适配。`check_edit` 是原 Guard 与新 hook 共用的判断；没有扩大原 shell 分类器的识别范围。没有修改 `core/`、`ports.py`、`provenance.json` 或冻结提交提示词。

## 本次继续迁移的验证

- **397 passed**：全部 Compass、Extension 服务、原生 Extension pipeline，以及测试规范、TUI 文案和等待点清单定向联合回归；其中含 **152 个 Compass 测试**（上一轮 145 + 本轮新增 7），数量不可相加。
- 新增案例覆盖真实宿主装配后的先拦截/后放行、审批修改命令后再拦截、拒绝结果不计证据、无主运行实例时拒绝、旧 v1/v2 checkpoint 迁目录恢复和证据冲突拒绝覆盖。
- `--prepare-only` 通过，36 个冻结文件及原提交提示校验通过；0 模型调用。
- iCode 全部 `src/chrys` 的 linux / darwin 类型检查通过；修改的接入模块与 builder 的 win32 目标类型检查通过。
- Smart Test 的第一次沙箱运行因 `sysctl` 权限及 worker 退出中断。允许本地进程查询后，结果为 **2,948 passed / 5 skipped / 1 failed**；唯一失败仍是已在干净 main 复现的长解释器路径截断断言。
- 对该单项使用**同一虚拟环境的临时短路径别名**复查，**1 passed**，没有改代码、依赖或测试断言。原 Smart Test 状态仍如实保留为 exit 1。
- Smart Test 因前述单项失败而未执行的 18 个架构目标已独立补跑：**402 passed**。与 397 项联合检查有重叠，不相加。
- Ruff、格式和 `git diff --check` 通过。
- wheel 构建及全新临时环境离线安装通过；实际发现三个工具、固定 context 与 before-tool hook，冻结源码和 CLI help 检查通过。仍使用 3.14.0，没有降低上游 3.14.10 版本要求。
- HTML 已离线检查目录锚点、UTF-8、表格和无外部资源。浏览器安全策略禁止 `file:` 预览，未声称视觉渲染验收通过。

## 上一轮迁移验证记录（历史，不代表重复执行）

验证环境为 macOS / Python 3.14.0；上游 `.python-version` 仍保留 3.14.10。本机审核可直接运行 `.venv/bin/icode`，避免 `uv run` 默认寻找尚未安装的解释器。没有修改日常 iCode 配置，没有发起付费模型请求或 benchmark/verifier。

| 检查 | 结果 |
| --- | --- |
| Compass 全部测试（原 124 项 + 本轮 21 项） | **145 passed** |
| Compass、Extension 装配、原生 UI/ACP 配置、测试规范、TUI 文案、等待点清单联合回归 | **426 passed**；在迁移命令补充测试前执行，包含当时的 144 项 Compass 测试，不与上一行相加。 |
| 源码冻结及独立入口 `--prepare-only` | 36 文件哈希通过；0 模型调用。 |
| Ruff、格式、i18n、diff 空白检查 | 通过。 |
| iCode 类型检查 | linux / darwin / win32 静态目标均通过。 |
| 本轮新增路由、迁移、存储、工具及运行接口的 Windows 目标类型检查 | 通过；不代表 Windows 实机验证或全部 core 类型检查通过。 |
| wheel 构建与归档检查 | 构建成功；元数据、77 个已跟踪文档及中文目录检查通过；新审核文档也确认入包。 |
| 全新临时环境安装 wheel | 使用锁定缓存依赖 / Python 3.14.0，工具发现、三个工具、冻结哈希、CLI help 通过。 |
| 仓库官方 wheel 安装检查 | 归档检查通过；安装阶段因本机没有 3.14.10 而未完成，没有改低项目版本要求。 |

Smart Test 因 `pyproject.toml` 改动自动升级为全仓库测试。在受限沙箱中出现本地端口绑定 / `sysctl` 权限错误及 35 次 worker 非正常退出后，主动中断。阶段统计为 **33,097 passed / 164 failed / 78 skipped / 1 error**，exit 2；Smart Test 的后续架构阶段未执行。不能称全仓库通过；上表架构回归来自单独执行的定向检查。

随后只对该轮失败项进行复查（允许本地回环端口与自有子进程查询，2 workers）：**162 passed / 2 failed / 1 skipped**。剩余两项在未修改 main `90a3fb99`、相同解释器/依赖下都复现：

- `tests/orchestration/sub_agents/test_integration.py::test_background_commands_a_sub_agent_started_stop_when_it_finishes`：本机解释器绝对路径很长，命令摘要被截断，测试预期的 `time.sleep(60)` 不再完整显示。
- `tests/service/tools/test_search_explicit_paths.py::test_explicit_dotfile_glob_syntax[{x,{.envrc,y}}]`：本机 `rg` 拒绝嵌套 alternate glob。

没有为这两个对照失败改动原生产品代码或放宽断言。也没有把中断运行中尚未执行的测试算作通过。

旧测试的必要更新只有：启用配置从旧 metadata 改为 extensions；原 Basic UI 测试改测正常 Tools/Extensions 开关；文件扩展和目录列表测试隔离测试开始前已安装的发行包，保留其真实测试安装与原有精确断言。另有专门测试验证实际安装的 Compass entry point，不通过伪造注册掩盖发现问题。

## 审核交付状态

main 合并记录单独保留；本轮 Compass 迁移按用户审核后的指令纳入本地提交。旧版本、core 和来源清单都保留，用户 Agent 配置没有自动迁移；远端推送是独立操作。


## 真实主会话验证（2026-11-20）

使用原生 `icode run` 和 `extensions: [compass]` 跑独立的端口范围解析修复任务，实际模型响应为 `deepseek/deepseek-v4.1-flash-1017`。进程正常退出，耗时 89.27 秒，15 次主请求均有响应。模型探索后调用一次 `start_compass_brief`，之后开始修改；审计记录两次 Brief 投递和一次提交检查，制品写入新的 `extension-data/compass` 目录。

独立重跑测试 **13/13 通过**：原有 8 项测试的 AST 保持不变，新增 5 项边界测试。此项是正常链路的真实冒烟验证，不是 benchmark 成绩，也不覆盖全部异常分支。模型配置请求 `reasoning.effort=max`；本次未做上游转发参数回显核验。示例代码与日志独立保存，不纳入产品源码或测试输入。


## 子调用工具误拦修复（2026-11-20）

此前 `before_tool` 对所有工具先调用 `current_runtime()`，子调用因身份不是主会话而被拒，连普通 `sleep` 都不能执行。新增回归通过 iCode 完整父代理→子代理→Extension hook→工具返回链路复现了问题（模型为离线模拟，不是一次付费真实模型运行）。

修复仅涉及 Compass 的 `scope.py` 和 `tools.py`：明确的非主调用跳过 Compass 编辑前检查；主会话或缺失身份仍沿用原严格检查。三个 Compass 工具保留原来的身份校验。明确识别子调用时不返回流程提示；由于当前宿主在准备子代理 context 时仍可能携带父调用身份，通用提示同时明确限定“只适用于主代理，子代理正常使用自己的工具，不调用 Compass”。这不是让子代理支持 Compass，也不宣称宿主已经在准备阶段提供正确的子调用身份。

新增 10 项回归覆盖三类非主调用的读、写、shell，以及完整子代理流水线。修复前 10 项失败；修复后全部 Compass 测试 **162 passed**（包括原有主调用拦截、身份缺失拒绝、交接及重试测试）。原 core 与 iCode 宿主源码未修改。

本次 Smart Test 最终为 **PASSED / exit 0**：Compass **162 passed**，架构检查 **72 passed**；不与先前单独跑的 162 项相加。Ruff、格式、Darwin 目标类型检查和 `git diff --check` 均通过。
