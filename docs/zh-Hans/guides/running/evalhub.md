# 在 EvalHub 运行 Compass v1.4.2 + iCode

上传仓库里的 `integrations/evalhub/r5_icode_evalhub.py`，Agent 类为 `R5ICodeAgent`。
这个文件包含启动和日志收集代码。任务启动后，脚本在任务沙箱中克隆本仓库的指定分支，安装锁定依赖，再启动 iCode。

## 平台填写

在任务模型设置里选择 DeepSeek Flash。平台提供的模型名称会原样用于 API 请求，支持平台自定义名称。
平台需向任务沙箱注入字符串环境变量 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，也支持 `OPENAI_API_BASE`。
提示词模板保持关闭，原始任务文本直接交给 Agent。

Agent 参数填写以下 JSON。

```json
{"repo_ref": "codex/compass-v1.4.2-evalhub"}
```

默认就是这个分支。每次新任务读取该分支当时的 HEAD，实际提交号保存在 `source-identity.json`。
如果需要检查分支仍指向某个提交，可额外填 `repo_commit`，值为完整的 40 位提交号。校验失败会在模型调用前结束。
`workdir` 默认使用任务沙箱当前工作目录，平台有明确代码目录时可以填写绝对路径。

## 运行过程

1. 在 `/logs/agent` 下创建本次任务独立目录，通过 `git clone --depth 1 --single-branch --branch ...` 获取源码。
2. 建立独立 Python 3.14.0 环境，执行 `uv sync --frozen --no-dev --no-default-groups`，使用仓库的 `uv.lock`。
3. 校验 Compass v1.4.2 的全部 72 个原始文件，开始 iCode 的源码探索、Compass 交接和 Brief 投递。
4. iCode 在任务工作目录修改代码并执行检查。结束时导出包含已提交修改和未提交修改的补丁，交给平台现有验收流程。

沙箱需要 Git 和 Python 3。uv 缺少时，脚本通过当前配置的 pip 软件源安装 uv。其后 Python 运行时和依赖下载遵循 uv 的下载及软件源配置，源码下载需要访问 GitHub。依赖装在本次任务目录内。
如果平台已准备好包含锁定 iCode 依赖的 Python 3.14 环境，可以用 `runtime_python` 指定其绝对路径。

## 版本和参数

| 项目 | 设置 |
|---|---|
| iCode 基线 | 0.34.0，`ffdc67ed74c1c2d78d16ab4b8544fed8d9ee3c69` |
| Compass | 用户提供的封存 v1.4.2，72 个原始文件逐字节保留 |
| 原始 Compass 包 SHA256 | `2866727c047a5bd4746f6d25b0ad64ebc717332b07f9842e3be607c98c95291e` |
| Brief 预算 | 6,000 字符，采用 v1.4.2 原始渲染逻辑 |
| 主模型输出上限 | 64,000 token |
| 主模型思考参数 | `reasoning_effort=max`，`thinking.type=enabled` |
| 主模型调用预算 | 250 次，生成时限 11,400 秒 |
| 维护模型 | 同一平台模型与网关，`temperature=0`，`thinking.type=disabled` |

主模型的 temperature、top_p、top_k 沿用提供方默认值。网关是否实际接受并执行思考参数，需要平台响应和服务配置共同确认。
本分支从上述评估基线创建。仓库 main 当前的 iCode 版本较新，独立升级 iCode 应另行验证适配。

## 查看结果与错误

- `source-identity.json` 记录实际分支和提交号。
- `runtime-setup.log` 记录源码克隆、依赖安装和源码校验。
- `generation.log` 记录 iCode 执行。
- `agent/adapter-events.jsonl` 记录主模型响应、维护和 Brief 投递。
- `submission.json` 和 `submission.patch` 保存最终提交校验信息和完整补丁。
- `outcome.json` 保存完成状态及固定错误类型。

安装失败时先看 `runtime-setup.log`。生成失败时看 `generation.log` 和 `agent/failure.json`。

## 本地验证与重新生成上传文件

```bash
uv sync --frozen --python 3.14.0
uv run --no-sync --python 3.14.0 python integrations/evalhub/build_agent.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/test_agent.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/test_submission.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/smoke_native.py
uv run --no-sync --python 3.14.0 python integrations/evalhub/smoke_sandbox.py
```

Smoke 使用本地合成任务和模拟模型响应，覆盖真实 iCode CLI、Brief、工具执行和补丁导出。内网 EvalHub 的端到端运行仍需要在平台执行验证。
