# EvalHub 上传与配置

## 创建 Agent

上传本目录的 `r5_mini_evalhub.py`，名称可填 `r5-mini-original`。平台会加载其中的 `R5MiniAgent`。

| 页面项目 | 填写方式 |
|---|---|
| Agent 参数 | 粘贴交付文件 `evalhub-arguments.json` 的完整内容 |
| 环境变量 | 使用平台已有的模型网关注入配置，自定义项填 `{}` |
| 提示词模板 | 关闭，或者只填 `{{instruction}}` |
| 模型 | 选择平台上的 DeepSeek Flash |
| 模型额外参数 | `{}`，脚本已发送本次固定实验参数 |
| 最大输出 token | 64,000 |
| 环境 | 使用当前 DeepSWE 任务的 OpenSandbox 环境 |
| Agent 运行超时 | 至少 12,000 秒，另给 setup 1,200 秒 |
| 合并总超时 | 如果只有一个同时覆盖安装和运行的字段，至少 13,200 秒 |
| 数据集与验收 | 使用任务包的代码、环境与 verifier 配置 |

平台需向任务沙箱注入 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`。沿用当前可以运行的 OpenSandbox 服务配置。脚本从任务沙箱的当前目录识别代码仓库。如果该目录不是代码仓库，在 Agent 参数里增加 `workdir` 并填写实际绝对路径。

## 实际执行流程

1. 下载 `spine-se-lab/iCode-brief` 的 `codex/r5-mini-original-evalhub` 分支，核对交付参数里的 commit。
2. 安装 Python 3.14.0 和锁定的 mini-swe-agent 2.4.6 依赖，检查原版 R5 的 72 个文件哈希。
3. 读取平台下发的任务说明，让 mini 探索代码，再通过 `compass start` 建立要求和 Brief。
4. 按原版 R5 流程修改代码、维护 Claims、执行提交检查。
5. 保存完整补丁，交给平台的任务 verifier 打分。

主模型请求固定发送 `thinking={"type":"enabled"}`、`reasoning_effort="max"` 和 `max_tokens=64000`，最多 250 个主步骤。主模型 temperature、top_p、top_k 均不显式设置。维护模型沿用原版低开销配置，关闭 thinking，temperature 为 0。

`adapter-events.jsonl` 可以核对脚本实际发送的参数。提供方是否接受并执行 max，需要结合平台网关的转发记录确认。

## 网络与日志

下载依赖需要能访问 GitHub、Python 包源和模型网关。常用下载域名为 `github.com`、`release-assets.githubusercontent.com`、`pypi.org`、`files.pythonhosted.org`。已缓存的依赖可直接复用，内网镜像需包含锁文件对应的包。

排查时先取 `source-identity.json`、`agent/run-status.json`、`agent/adapter-events.jsonl`、`submission.json`。需要查看具体推理过程时再取 `agent/trajectory.json` 和 `generation.log`。

原版 R5 核心和 mini 版本已固定。本次通过 EvalHub 网关调用模型，在 OpenSandbox 中执行工具。历史四批使用的模型路由、任务环境及验收环境可能与平台有差异，通过率需以平台实际运行结果为准。
