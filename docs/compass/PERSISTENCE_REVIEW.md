# Compass 独立持久化审核（2026-11-20）

## 这次改变了什么

Compass 自己保存和恢复任务状态，不再把业务状态塞进 iCode 的对话 history、也不要求 iCode serializer 认识 Compass 字段。模型选择及输入捕获接线暂时不变，仍沿用界面选中的模型。

参考旧目录 `CodeHelix-Plugin/plugins/compass-brief/source/package/src/compass_brief` 中的 `chrys/storage.py`、`chrys/artifacts.py`、`persistence/locations.py`、Claims/Evidence 存储实现：保留数据与安装代码分离、按会话隔离、原子写入、所有者权限校验，以及原始证据和维护审计的分层。

没有照搬旧安装器的 deployment locator、PackageStore 和自定义数据根选择：当前是仓库内 Extension，直接使用它已有的 `session_data_dir`，无需另一套安装配置。旧 Feature 接口不等同于现在的 Extension，模型与生命周期接线没有假装已经迁移成功。

## 目录

```text
<iCode 会话目录>/extensions/compass/
├── session.json                  # Compass 自己的恢复入口
├── ledger/
│   ├── claims/
│   │   ├── state.json            # 核心 Claims 状态
│   │   ├── claims.jsonl          # 核心维护审计
│   │   ├── brief_metrics.jsonl   # 如有对应维护记录
│   │   └── briefs/               # 历次生成的 Brief
│   └── evidence/raw/             # 有界原始工具输出 E-xxx.txt
└── tool-state.json               # 如存在旧版本文件，只读取迁移，保留原件
```

`session.json` 包含用户原文、尚未整理的工具结果、Goal/Requirements/Constraints、Claims/Evidence、核心去重状态和维护游标。它是恢复入口，不是每个审计文件的副本索引扫描器；恢复不读取项目代码或隐藏验收数据。

文件内的账本位置使用相对值 `ledger`。会话目录复制到新位置后，恢复时解析到副本自己的账本，不继续依赖原会话的绝对目录。原始 Evidence 引用仍使用 core 的相对路径规则。

## 职责

- `runtime/persistence.py`：Compass 会话文件格式、读写与旧文件导入；只使用存储端口，不依赖 iCode。
- `adapters/icode/storage.py`：复用宿主现成的安全文件 API，实现存储端口，不修改宿主文件实现。
- `adapters/icode/tool_session.py`：持有 Compass 自己的内存状态，把收集的数据交给存储层；不再写宿主 history。
- `core/`：继续维护原来的 Claims、Evidence、Brief 产物；本轮未修改 core。

此前加入 `src/chrys/service/state/serializers.py` 的两个 Compass 字段已撤回，该文件与迁移基线 main 完全一致。`runner.py` 仍与同一基线一致。本轮没有增加其他 iCode 接线。

## 迁移和错误处理

1. 优先读取新的 `session.json`。
2. 文件不存在时，允许读取当前 Extension 目录中的旧 `tool-state.json`；首次保存写入新文件，旧文件保留。
3. ~~两个文件都不存在时，从 builder 提供的旧 history 导入 Compass 字段。~~ 2026-11-23 已删除：serializer 撤回后该字段在保存时已被丢弃，这条路径实际走不到；只存在于旧 iCode history 中的 Compass 状态不再自动迁入。见 [接线缩减审核](SMALL_HOST_SEAM_REVIEW.md)。
4. 格式损坏、权限错误、非预期账本路径明确报错，不当作“空会话”覆盖。
5. 保存走现有安全原子写接口；写入失败时保留最后成功的磁盘检查点。

## 保留边界

- 这次解决的是持久化独立性。builder 仍为 Compass 提供所选模型、资源关闭和主回合身份（旧状态导入入口已于 2026-11-23 删除）；不宣称全仓已实现零宿主改动。
- 普通重启恢复及复制目录后的证据读取已验证；没有运行真实 iCode UI 的完整 fork 流程。
- Compass 恢复最新独立检查点。iCode 对话回滚到旧消息时，不能据此声称 Compass 也自动回滚到同一时点；完整的原子回滚仍需要宿主公开对应生命周期能力。
- 同一目录只支持一个活动会话写入者；没有新增跨进程并发写入协议。
- 旧目录不批量移动、不删除；不触碰历史实验或冻结复现分支。

## 验证

只运行本轮新增 `tests/integration/compass/test_independent_persistence.py`：9 项通过。覆盖没有宿主 Compass 字段仍能恢复、原文和待处理证据恢复、复制目录并移走原路径后的证据读取、旧文件保留迁移、坏 JSON/错误格式拒绝、权限错误、路径逃逸拒绝及模拟写失败保留上次记录。

lint、格式与新持久化/接入文件的类型检查通过。没有运行旧测试或全量测试，没有付费模型请求，未提交、未推送。
