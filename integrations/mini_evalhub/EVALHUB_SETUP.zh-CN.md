# Compass v1.9 + mini-swe-agent 上传与配置

## 创建 Agent

上传本目录的 `r5_mini_evalhub_v1_9.py`，名称可填 `compass-v1.9-mini`。平台会加载其中的 `R5MiniAgent`。

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

v1.9 源码取自你提供的封存包，包 SHA256 为 `032bd720722e99ea3c783a8844aa15fac31e5c612203e18f1060c423e3923ac7`。Brief 默认预算为 15,000 字符，采用封存包的全文展示及预算回退逻辑。

## 实际执行流程

1. 下载 `spine-se-lab/iCode-brief` 的 `codex/compass-v1.9-mini-evalhub` 分支，核对交付参数里的 commit。
2. 安装 Python 3.14.0 和锁定的 mini-swe-agent 2.4.6 依赖，检查封存 v1.9 的 72 个文件哈希。
3. 读取平台下发的任务说明，让 mini 探索代码，再通过 `compass start` 建立要求和 Brief。
4. 按封存 v1.9 流程修改代码、维护 Claims、执行提交检查。
5. 保存完整补丁，交给平台的任务 verifier 打分。

主模型请求固定发送 `thinking={"type":"enabled"}`、`reasoning_effort="max"` 和 `max_tokens=64000`，最多 250 个主步骤。主模型 temperature、top_p、top_k 均不显式设置。维护模型沿用原版低开销配置，关闭 thinking，temperature 为 0。

`adapter-events.jsonl` 可以核对脚本实际发送的参数。提供方是否接受并执行 max，需要结合平台网关的转发记录确认。

## 沿用已验证的重试和预算处理

| 项目 | 当前设置 |
|---|---|
| 主模型重试 | mini 原生默认最多 10 次尝试，退避等待 4–60 秒，认证失败立即退出 |
| 单次主请求超时 | 900 秒 |
| R5 维护调用 | 沿用 SDK 默认重试，保留原异常类型和 HTTP 状态，R5 可识别 429/503 等临时错误 |
| 维护预算 | 每次最多 120 秒，合计最多 240 秒，最多 2 次结构化调用尝试 |
| 主步骤预算 | 250 步，同一步内的 HTTP 重试沿用 mini 原生计数方式 |
| 正常提交 | 生成退出码 0 |
| 步数耗尽 | 原生 LimitsExceeded、生成退出码 1，已有补丁仍交给 verifier |
| 连续格式错误退出 | 原生 RepeatedFormatError、生成退出码 1，已有补丁仍交给 verifier |
| 总生成超时 | 11,400 秒后发 SIGTERM，最多等待 30 秒后 SIGKILL，记录 124 与实际进程退出码，保留补丁交给 verifier |
| 工具执行 | bash -lc，每条最多 300 秒，保留超时前的输出，以错误观察返回给 agent |

总超时与步数耗尽分别记录。普通运行异常保留错误和补丁证据，并由平台记为异常。每题只启动一次 solver，轮询失败沿用有界查询恢复。

维护请求的 SDK 重试配置已恢复为原版的默认方式。当前依赖使用锁文件固定，历史四批的完整依赖闭包尚未逐包证明一致。平台网关、任务镜像、网络及 verifier 仍由平台控制。

## 网络与日志

下载依赖需要能访问 GitHub、Python 包源和模型网关。常用下载域名为 `github.com`、`release-assets.githubusercontent.com`、`pypi.org`、`files.pythonhosted.org`。已缓存的依赖可直接复用，内网镜像需包含锁文件对应的包。

排查时先取 `source-identity.json`、`agent/run-status.json`、`agent/adapter-events.jsonl`、`submission.json`。需要查看具体推理过程时再取 `agent/trajectory.json` 和 `generation.log`。

封存 v1.9 源码和 mini 版本已固定。本次通过 EvalHub 网关调用模型，在 OpenSandbox 中执行工具。本地使用的模型路由、任务环境及验收环境可能与平台有差异，通过率需以平台实际运行结果为准。
