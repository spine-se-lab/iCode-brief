# Compass 提交审核

2026-11-19。本次将已审核的 Compass 接入和修复整理为本地提交，不推送远端。

## 本次四项修复

1. 等待点清单：按仓库工具重新生成，共 2987 个等待点。33 个重新打开的记录逐一核对，均属于 builder 的构建路径、runner 的执行路径或新增的失败报告路径。旧节点原本均为 unresolved；保留原来的保守归类。新增 `own_or_release(compass.aclose)` 可能执行关闭，`compass.run` 包含模型、工具和维护等待，`report_policy_failure` 包含事件订阅者等待，均无覆盖所有分支的精确耗时区间证明，继续标为 unresolved，不将它们伪报为已精确计量。
2. 客户端生命周期：登记 `SelectedModel.complete` 的关闭责任。客户端赋给 `self.client` 后没有插入其他 await；`SelectedModel.aclose` 由 `InteractiveCompass.aclose` 调用，适配器在客户端创建前已注册到 `PreparedAgent`。保留连接复用，不改成每次维护新建连接。Smart Test 的所有者扫描规则也覆盖 `src/compass/**`。
3. 本地化：Compass 标签使用原生 `msg` 和 `render_str`，POT、简体中文 PO、消息 ID oracle 同步。品牌名中英文都保留 Compass。
4. UI 分组：使用独立 `bc-compass-section`，ACP 模式同时隐藏模型区和 Compass。原版“模型区 4 个控件”断言未改。Compass 测试增加独立分组、ACP 隐藏、切回显示检查。

核心 `src/compass/core/` 与 provenance 保持冻结。界面没有总时间上限、同一任务仍限 250 次主请求；独立评测仍为 250 次 / 11,400 秒。

## 真实无界面示例

实际运行 Downloads 中的代码，任务为修复标签清洗函数：去除首尾空白、小写化、排除空值、去重且保留顺序、不修改输入。使用真实 OpenRouter DeepSeek 响应，配置请求 max reasoning；本次未额外核验网关内部参数转发。

结果：退出码 0，完成状态 completed；10 次主请求/响应、6 次维护响应、1 次 Brief 投递、1 次提交检查。审计事件首尾约 72.25 秒。独立重跑生成的测试：8/8 通过。示例是功能冒烟验证，不代表基准成绩。

## 检查记录

本次精确测试结果以随后的验证记录为准。测试没有和源码编辑并发进行。

扩大范围的 Smart Test 实际结果为 38 failed、989 passed、1 skipped、1 error：其中有 worker 崩溃、本地服务器监听超时，打包子进程未找到仓库指定 Python 3.14.10。此记录保留，不标为通过，也没有证明每项失败都与分支无关。架构阶段因前一阶段失败未执行，故另行直接运行明确的架构检查。

额外全范围类型探查 `src/chrys src/compass` 报 20 条诊断，涉及 CLI 和冻结核心；没有为清空诊断改动 core。宿主原有范围和本次接入层的定向检查另记。用户提供的全量结果未被本次局部回归替代。

测试环境 Python 3.14.0，保留原 Python pin。未更改环境以规避检查；未运行整个 iCode 测试库。

## 最终定向验证

- 明确的等待清单、源码规范、本地化、客户端所有者检查，整个 ACP 配置文件、catalog oracle、全部 Compass：**297 passed，80.10 秒**。
- Ruff：src、tests、Smart Test 脚本检查通过；格式检查 2891 文件通过。
- 类型：`src/chrys`、Compass 接入层与 runtime 定向范围通过。
- i18n extract/update/check 与等待清单生成第二次运行无变化，git diff 空白检查通过。
- 离线打包另行尝试：沙箱内 uv 遇到 macOS 系统配置崩溃；沙箱外使用已有环境时缺少 hatchling，因此本次打包未完成，不将其记作通过。

本地提交仅包含代码、测试、文档、翻译和审核清单，不包含密钥、运行日志、模型产物或基准事后材料。冻结 core 哈希与本轮开始前相同。
