# ProofCoder 路线图

本文件只记录进度：做到哪里、下一步做什么。约束以[开发规范](DEVELOPMENT_SPEC.md)为准，约束为什么改变见[架构决策记录](adr/README.md)。每个合并到 `main` 的变更都要更新本文件。

## 当前位置

- 规范版本：v3.4
- 当前阶段：阶段 I（多轮会话）
- 下一步：小项 J.3，流式响应的拼装与校验，以及命令行与浏览器的增量渲染
- 最近完成：小项 J.2，`PROOFCODER_PROVIDER` 提供方选择与本地模型适配层（Ollama，标准库 HTTP）
- 阻塞项：阶段 I 的通用退出条件要求「新增能力有对应的真实模型评测 fixture 和重复运行数据」。fixture 已就绪且有离线测试，重复运行数据需要真实 key，见 [EVAL_REPORT §15](EVAL_REPORT.md)

状态取值：`已完成`、`进行中`、`未开始`、`阻塞`、`暂缓`。

## 阶段总览

| 阶段 | 目标 | 依赖 | 状态 | 版本 |
| --- | --- | --- | --- | --- |
| A–E | 骨架、Agent 闭环、核心工具、鲁棒性、可观测性与评测 | — | 已完成 | 0.1.0 |
| — | 本地浏览器界面（[ADR-0002](adr/0002-local-browser-interface.md)，PR #1） | A–E | 已完成 | 0.1.0 |
| 0 | 治理基础 | — | 已完成 | — |
| F | 检查点与回滚 | 0 | 已完成 | — |
| G | 文件工具扩展 | F | 已完成 | — |
| H | 命令策略配置与人工审批 | F | 已完成 | — |
| I | 多轮会话 | 0 | 进行中 | — |
| J | 可替换模型提供方与流式响应 | 0 | 进行中 | — |
| K | 上下文与仓库理解 | I | 未开始 | — |
| L | 操作系统级隔离 | G、H | 未开始 | — |
| M | 1.0 发布 | F–L | 未开始 | 1.0.0 |

各阶段的目标和退出条件见开发规范 §16，约束调整的依据见 [ADR-0003](adr/0003-v3-scope-revision.md)。

## 版本计划

- `0.1.0`：阶段 A–E 和浏览器界面。这是 `pyproject.toml` 中的当前版本，尚未打标签。
- 阶段 F–L 按完成顺序各升一个次版本（0.2.0、0.3.0……）。I、J 可以和 F–H 穿插进行，版本号按实际完成顺序分配。
- 阶段 M 完成后发布 1.0.0。

## 阶段 0：治理基础

| 小项 | 内容 | 状态 | PR |
| --- | --- | --- | --- |
| 0.1 | 开发规范 v3.0、本路线图、ADR-0001 至 0003、CHANGELOG、`AGENTS.md`、`CLAUDE.md`、PR 模板 | 已完成 | [#2](https://github.com/mingbochen/proofcoder/pull/2) |
| 0.2 | 选择许可证（Apache-2.0），添加 `LICENSE`，更新 README 与 `pyproject.toml` 的许可说明 | 已完成 | [#2](https://github.com/mingbochen/proofcoder/pull/2) |
| 0.3 | 在 `compliance.py` 中加入文档一致性检查（开发规范 §18.3），并修正规范 §5.1 目录树 | 已完成 | [#3](https://github.com/mingbochen/proofcoder/pull/3) |

退出条件：0.1–0.3 全部合并，CI 通过。

## 阶段 F：检查点与回滚

| 小项 | 内容 | 状态 | PR |
| --- | --- | --- | --- |
| F.1 | 阶段 ADR 与规范补全：检查点的存储方式、覆盖范围、敏感文件处理、保留与清理策略（[ADR-0004](adr/0004-workspace-checkpoints.md)、规范 §10.5 与 §13.4） | 已完成 | [#4](https://github.com/mingbochen/proofcoder/pull/4) |
| F.2 | 检查点创建与回滚核心，含轨迹事件和离线测试 | 已完成 | [#5](https://github.com/mingbochen/proofcoder/pull/5) |
| F.3 | 命令行回滚入口 `proofcoder rollback`（list/show/apply/delete），含确认清单与操作轨迹 | 已完成 | [#6](https://github.com/mingbochen/proofcoder/pull/6) |
| F.4 | 浏览器界面回滚入口：计划预览、与清单绑定的确认、检查点与回滚事件渲染 | 已完成 | [#7](https://github.com/mingbochen/proofcoder/pull/7) |
| F.5 | 评测 fixture 与文档同步：`rollback-word-wrap` fixture、评测流程的回滚验证、文档同步 | 已完成 | [#8](https://github.com/mingbochen/proofcoder/pull/8) |

## 阶段 G：文件工具扩展

| 小项 | 内容 | 状态 | PR |
| --- | --- | --- | --- |
| G.1 | 阶段 ADR 与规范补全：删除边界、覆盖语义、多处修改形状、无检查点时的行为（[ADR-0005](adr/0005-destructive-file-tools.md)、规范 §10.5.6） | 已完成 | [#9](https://github.com/mingbochen/proofcoder/pull/9) |
| G.2 | `delete_path`、`move_path`、`make_directory`、`patch_file`、`create_file` 的覆盖参数，以及无检查点时的拒绝，含离线测试 | 已完成 | [#10](https://github.com/mingbochen/proofcoder/pull/10) |
| G.3 | 删除与重命名的评测 fixture `cleanup-text-helpers`，以及文档同步 | 已完成 | [#11](https://github.com/mingbochen/proofcoder/pull/11) |

## 阶段 H：命令策略配置与人工审批

| 小项 | 内容 | 状态 | PR |
| --- | --- | --- | --- |
| H.1 | 阶段 ADR 与规范补全：策略来源与显式授权、三值判定、审批语义、与证据判定的关系（[ADR-0006](adr/0006-command-policy-and-approval.md)、规范 §10.4 与 §13.5） | 已完成 | [#13](https://github.com/mingbochen/proofcoder/pull/13) |
| H.2 | 三值判定、项目策略加载与冻结、同步主循环中的审批协议，含离线测试 | 已完成 | [#14](https://github.com/mingbochen/proofcoder/pull/14) |
| H.3 | 命令行与浏览器的审批入口，含摘要绑定 | 已完成 | [#15](https://github.com/mingbochen/proofcoder/pull/15) |
| H.4 | 非 Python 项目的评测 fixture `nodejs-word-count`，以及文档同步 | 已完成 | [#16](https://github.com/mingbochen/proofcoder/pull/16)、[#17](https://github.com/mingbochen/proofcoder/pull/17) |

## 阶段 I：多轮会话

| 小项 | 内容 | 状态 | PR |
| --- | --- | --- | --- |
| I.1 | 阶段 ADR 与规范补全：携带什么、放在哪里、证据不跨运行继承、会话数据的信任边界（[ADR-0007](adr/0007-cross-run-sessions.md)、规范 §9.5、§10.6 与 §13.6） | 已完成 | [#19](https://github.com/mingbochen/proofcoder/pull/19) |
| I.2 | 会话存储与校验、携带摘要的确定性组装与裁剪、证据不跨运行继承的保证，含离线测试 | 已完成 | [#20](https://github.com/mingbochen/proofcoder/pull/20) |
| I.3 | 命令行与浏览器的会话入口，以及浏览器在服务重启后的会话恢复 | 已完成 | [#21](https://github.com/mingbochen/proofcoder/pull/21) |
| I.4 | 声明任务序列的多轮会话评测 fixture `session-two-step-report`，以及文档同步 | 阻塞（等待真实模型重复运行数据） | [#22](https://github.com/mingbochen/proofcoder/pull/22) |

## 阶段 J：可替换模型提供方与流式响应

| 小项 | 内容 | 状态 | PR |
| --- | --- | --- | --- |
| J.1 | 阶段 ADR 与规范补全：提供方边界、流式给循环看什么、流式 tool call 的拼装、默认路径（[ADR-0008](adr/0008-provider-abstraction-and-streaming.md)、规范 §4.7 与 §11.5） | 已完成 | [#23](https://github.com/mingbochen/proofcoder/pull/23) |
| J.2 | 提供方选择与配置分组，以及本地模型适配层，含离线测试 | 已完成 | [#24](https://github.com/mingbochen/proofcoder/pull/24) |
| J.3 | 流式响应的拼装与校验，命令行与浏览器的增量渲染 | 未开始 | — |
| J.4 | 本地模型的评测记录与文档同步 | 未开始 | — |

## 阶段 K–M

这些阶段在开始时再拆分小项，统一按以下顺序：

1. 阶段 ADR 与规范补全（需要用户批准）。
2. 核心实现与离线测试。
3. 命令行和浏览器界面入口。
4. 评测 fixture 与文档同步。

## 已知问题与技术债

这些问题不属于某个阶段，但需要处理。开始处理时，把它移到对应阶段或新增一个小项。

| 问题 | 影响 | 建议处理时机 |
| --- | --- | --- |
| `serve` 的默认端口 8765 可能落入 Windows 的保留端口段（作者机器上为 TCP 8750–8849）。 | 首次启动失败，需要 `--port` | 任意小项 |
| 配置只从进程环境变量读取，不自动加载 `.env`；README 依赖 `uv run --env-file`。 | 没有 uv 时配置步骤繁琐 | 阶段 M 之前 |
| 在中文 Windows 上，若项目路径含非 ASCII 字符，Python 启动时以 GBK 读取 uv 写入的 UTF-8 `.pth` 会失败，报错发生在 ProofCoder 代码执行之前。需要 `PYTHONUTF8=1` 或纯 ASCII 路径。 | 首次运行即失败，且错误信息不指向原因 | 阶段 M 之前 |
| 浏览器界面：服务重启后丢失进行中的运行（已记录的会话可恢复）；模型文本不渲染 Markdown；切换语言不重绘进行中的运行。 | 使用体验 | 阶段 M |
| 阶段性命名，如 `STAGE_B_SYSTEM_PROMPT`。 | 可读性 | 阶段 M |

## 维护规则

- 每个 PR 合并后，更新“当前位置”、对应小项的状态和 PR 列。
- 增加、删除或调整阶段，必须先修改开发规范 §16 并新增 ADR。
- 已完成的小项保留在表中，不删除。
