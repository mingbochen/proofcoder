# ProofCoder 开发规范与验收标准 v2.1

> ProofCoder 是一个轻量编程智能体：模型负责根据环境反馈决定下一步行动，本地程序负责对话管理、工具执行、安全约束、错误恢复和任务终止。

本文档面向项目开发者、辅助开发工具和代码评审者，作为实现、测试和代码审查的统一依据。

---

## 1. 项目目标

ProofCoder 应能够接收自然语言编程任务，自主完成以下闭环：

1. 探索工作区和代码结构。
2. 搜索并读取相关文件。
3. 创建或精确修改文件。
4. 在本地运行测试、构建或静态检查。
5. 根据真实错误输出调整方案并继续执行。
6. 在满足终止条件后报告修改、验证证据和已知局限。

项目追求以下工程属性：

- **自主性**：具体步骤和工具选择由模型根据环境反馈动态决定，而非由固定业务流程写死。
- **真实性**：所有文件内容、命令结果和测试结论均来自本地执行，不能由模型臆测。
- **可验证性**：修改后的完成状态由测试或构建证据支持。
- **可追踪性**：模型回复、工具调用、执行结果、diff 和终止原因均可审计。
- **可解释性**：关键设计具有明确的备选方案、取舍和测试依据。
- **克制性**：优先保证闭环质量，不用功能数量制造不必要复杂度。

### 1.1 核心特色

项目重点实现三项能力：

1. **Evidence-Gated Completion**：文件修改后若没有后续成功验证，只能报告 `completed_unverified`，不能声明已经验证完成。
2. **Auditable Local Trajectory**：终端事件流、JSONL 运行轨迹、统一 diff 和最终统计共同记录完整执行过程。
3. **Defense-in-Depth Local Tools**：通过路径约束、敏感文件保护、命令无 shell 执行、环境过滤、超时和输出限制降低本地执行风险。

---

## 2. 项目红线

### 2.1 运行时独立性

ProofCoder 运行时必须保持独立，不得：

- 包装或调用 Claude Code、Codex、OpenCode、DeepSeek Harness 等现成 agent 产品。
- 使用 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI、DeepSeek Harness 等 agent 框架或 SDK。
- 使用其它代为实现 agent loop、工具调度、记忆、规划或执行器的组件。
- 使用 API 服务端托管的 Code Interpreter、Files API、File Search、Shell、Apply Patch、Computer Use 或类似文件/代码执行工具。

允许使用普通模型 API 客户端进行网络通信。本项目使用 `openai` Python 包调用 DeepSeek 的 OpenAI 兼容接口，但不使用 `openai-agents`。

本地工具读取必要代码片段并将文本作为普通模型输入，不属于托管文件工具；项目不得上传文件生成服务端 `file_id`，也不得让服务端工具代替本地程序操作文件或执行命令。

### 2.2 必须自行实现的核心逻辑

以下能力必须存在于项目源码中，并具有直接测试：

- 对话历史与上下文预算管理。
- 工具定义、注册、参数验证与分发。
- 文件工具和命令工具的本地执行。
- 模型文本与 tool call 的解析。
- Agent 主循环和状态维护。
- 完成、预算、中断与无进展终止。
- API、协议和工具错误的分类与恢复。

### 2.3 凭据与敏感信息

- API key 只从 `DEEPSEEK_API_KEY` 环境变量读取。
- 有效凭据不得进入源码、配置样例、Git 历史、日志或测试 fixture。
- `.env`、运行日志和本地状态目录必须被 Git 忽略。
- 配置对象、异常信息和日志输出必须脱敏。
- 本地命令默认不能继承 API key 等敏感环境变量。

### 2.4 依赖边界

建议依赖保持最小化：

- 运行依赖：`openai`、`rich`。
- 开发依赖：`pytest`、`pytest-cov`、`ruff`。
- 其余功能优先使用 Python 标准库实现。

新增运行依赖前必须说明用途、不可替代性和是否隐藏了项目核心逻辑。锁文件中的直接依赖与传递依赖均需检查。

---

## 3. 范围与非目标

### 3.1 核心范围

- DeepSeek API 客户端与原生 tool calling。
- 自行维护的消息历史和上下文压缩。
- 统一工具注册表与结果协议。
- 代码搜索、分段读取、创建、精确编辑和本地命令执行。
- 路径与命令安全策略。
- 结构化错误、自愈和多层终止。
- diff、运行轨迹与验证状态。
- 离线协议测试和真实模型端到端评测。

### 3.2 非目标

当前版本不实现：

- 多 agent、子 agent 或模型投票。
- 多模型路由和通用厂商插件系统。
- MCP 工具生态接入。
- 向量数据库或长期记忆。
- 图形界面和复杂 TUI。
- AST 级跨语言重构。
- 自动 Git 提交、推送或创建 PR。
- 完整操作系统沙箱。
- 任何服务端文件或代码执行能力。

非目标不是永久禁止，而是用于保持当前版本的实现可控、可测和可解释。

---

## 4. 技术方案

### 4.1 基础技术栈

- Python 3.11+。
- `src` 项目布局和完整类型标注。
- `dataclass` 表达内部协议与状态。
- `pyproject.toml` 管理项目，并保留经过验证的锁文件。
- 同步 AgentLoop 优先，不为异步和并发引入额外状态复杂度。

### 4.2 模型与接口

- 模型：`deepseek-v4-flash`。
- Base URL：`https://api.deepseek.com`。
- 协议：OpenAI 兼容 Chat Completions。
- 模型能力：原生 function tool calls。
- 默认 thinking mode，reasoning effort 为 `high`，允许通过环境变量调整。
- 默认使用非流式响应，避免流式工具参数拼接增加协议复杂度。

普通 `openai` 客户端仅负责请求序列化、HTTP 通信和响应对象构造。创建客户端时显式设置 `max_retries=0`，API 重试由项目代码统一处理。

### 4.3 选择 Chat Completions 的原因

Chat Completions 的消息结构与本项目需求直接对应：

- assistant 消息携带 `tool_calls`。
- 本地执行后使用 `role=tool` 和 `tool_call_id` 回填结果。
- 历史由客户端完整持有和重发，便于审计与验证上下文管理逻辑。
- 不需要引入 Responses API 的 item 协议或任何内置工具能力。

### 4.4 Tool schema 策略

DeepSeek strict tool calling 需要 Beta 接口。项目基线使用稳定地址，并在本地完成：

- JSON 参数解析。
- 工具名称检查。
- 必填字段、类型、范围和枚举验证。
- 未知字段拒绝。
- 结构化错误回填和有限纠正。

只有当 strict 模式通过全部协议与端到端测试时，才能作为可选配置；项目正确性不能依赖 Beta 功能。

### 4.5 Thinking 消息处理

DeepSeek thinking mode 的 assistant 消息可能同时包含：

- `content`
- `reasoning_content`
- `tool_calls`

仍保留在历史中的 assistant 消息必须完整保存这些字段。实现时应检查客户端对象序列化结果，并为 `reasoning_content` 编写协议回归测试。

终端和默认 JSONL 轨迹不显示或持久化完整 `reasoning_content`。面向用户呈现的是行动与执行证据，而不是隐藏推理过程。

### 4.6 配置与 CLI

核心环境变量：

- `DEEPSEEK_API_KEY`：必需。
- `DEEPSEEK_BASE_URL`：默认 `https://api.deepseek.com`。
- `DEEPSEEK_MODEL`：默认 `deepseek-v4-flash`。
- `DEEPSEEK_REASONING_EFFORT`：默认 `high`。

建议 CLI：

```text
proofcoder doctor
proofcoder run --workspace ./project "修复任务描述"
proofcoder trace <run-id>
proofcoder eval --repeat 3
```

- `doctor` 检查 Python、依赖、工作区权限、密钥是否设置和模型是否可访问，但不显示密钥。
- `run` 执行一次 agent 任务。
- `trace` 回放脱敏运行轨迹。
- `eval` 运行项目自带评测，不依赖第三方 agent harness。

---

## 5. 系统架构

```mermaid
flowchart TD
    U["用户任务"] --> A["AgentLoop"]
    A <--> M["DeepSeek API"]
    A --> T["Local Tool Registry"]
    T --> W["Workspace"]
    A --> C["Context and Trace"]
```

职责划分：

| 模块 | 职责 |
|---|---|
| CLI | 解析任务、工作区和运行配置，处理用户中断 |
| AgentLoop | 驱动模型—工具—观察循环，维护状态并判断终止 |
| DeepSeekClient | 构造 API 请求、规范化模型响应，不承担 agent 决策 |
| ContextManager | 管理消息预算、完整轮次裁剪和结构化状态摘要 |
| ToolRegistry | 提供 schema、验证参数、分发本地工具 |
| SafetyPolicy | 校验路径、敏感文件、命令和环境变量 |
| VerificationTracker | 跟踪修改和后续验证证据 |
| TraceRecorder | 写入脱敏事件、diff、统计和终止原因 |

### 5.1 建议目录结构

```text
proofcoder/
├── AGENTS.md
├── README.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── .gitignore
├── .env.example
├── src/proofcoder/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── agent.py
│   ├── protocol.py
│   ├── context.py
│   ├── errors.py
│   ├── retry.py
│   ├── prompt.py
│   ├── trace.py
│   ├── verification.py
│   ├── llm/
│   │   ├── base.py
│   │   ├── deepseek.py
│   │   └── scripted.py
│   ├── tools/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── files.py
│   │   ├── search.py
│   │   ├── command.py
│   │   └── finish.py
│   └── safety/
│       ├── paths.py
│       ├── secrets.py
│       └── commands.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── protocol_fixtures/
├── evals/
│   ├── fixtures/
│   ├── tasks.json
│   └── run_evals.py
├── docs/
│   ├── DEVELOPMENT_SPEC.md
│   ├── DESIGN.md
│   ├── COMPLIANCE.md
│   ├── THREAT_MODEL.md
│   └── EVAL_REPORT.md
└── scripts/
    ├── compliance_check.py
    └── secret_scan.py
```

目录按职责拆分，但不建设无实际需求的抽象层。除测试用 `ScriptedClient` 外，不实现通用多厂商客户端体系。

---

## 6. 核心协议

### 6.1 工具结果信封

所有工具返回统一、JSON 可序列化的结果：

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "meta": {
    "duration_ms": 12,
    "truncated": false
  }
}
```

失败示例：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "AMBIGUOUS_MATCH",
    "message": "old_text matched 3 locations; provide more context",
    "retryable": true
  },
  "meta": {}
}
```

错误码应稳定，错误消息应说明事实和可行的恢复方向，不得只返回堆栈或模糊的 `failed`。

### 6.2 工具定义

每个工具必须声明：

- 唯一名称。
- 面向模型的用途和边界说明。
- JSON Schema。
- 本地参数验证器。
- 本地执行函数。
- 是否修改工作区。
- 风险等级。
- 输出截断策略。

模型 schema 与本地验证规则必须由同一份定义生成或共享字段描述，避免两套协议漂移。不得使用 agent 工具框架代替注册与分发逻辑。

### 6.3 多 tool call 完整性

一轮 assistant 消息可能包含多个 `tool_calls`。处理流程必须满足：

1. 保存完整 assistant 消息和每个 `tool_call_id`。
2. 在产生副作用前验证整批工具名称与参数。
3. 任一调用格式非法时，默认不执行本批副作用，但仍为每个调用生成对应错误结果。
4. 全部合法时按模型给出的顺序执行，不并发执行文件修改或命令。
5. 每个调用后追加一条 ID 匹配的 `role=tool` 消息。
6. `finish_task` 必须是该轮唯一调用，否则返回协议错误。

该策略避免部分副作用，并保证后续请求的消息链完整。

---

## 7. 本地工具集

### 7.1 `list_files`

用途：了解工作区结构。

建议参数：`path`、`max_depth`、`pattern`、`include_hidden`。

要求：

- 默认忽略 `.git`、虚拟环境、`node_modules`、缓存和运行日志。
- 限制最大条目数并报告截断数量。
- 路径必须经过统一工作区校验。

### 7.2 `search_text`

用途：按文本或正则搜索代码，返回文件、行号和短上下文。

要求：

- 优先调用本地 `rg`，不可用时使用项目实现的 Python 回退。
- 支持 glob、大小写和最大结果数。
- 跳过二进制文件、大文件和默认忽略目录。
- 明确区分零匹配与搜索失败。

### 7.3 `read_file`

用途：按行读取文件，避免默认把整份大文件送入上下文。

建议参数：`path`、`start_line`、`end_line`。

要求：

- 返回带行号内容、总行数和实际读取范围。
- 限制单次字节数与行数。
- 拒绝二进制文件。
- 明确处理 UTF-8 BOM、换行符和解码错误。
- 默认拒绝 `.env`、私钥和常见凭据文件。

### 7.4 `create_file`

用途：创建新文件。

要求：

- 目标已存在时拒绝，不静默覆盖。
- 使用同目录临时文件和原子替换。
- 返回创建字节数和 diff。

### 7.5 `replace_in_file`

用途：精确修改已有文件。

建议参数：`path`、`old_text`、`new_text`、`expected_replacements`，默认期望一次。

要求：

- 零匹配返回 `MATCH_NOT_FOUND`。
- 匹配次数与期望不符返回 `AMBIGUOUS_MATCH`，不修改文件。
- 在内存完成全部校验后原子写入。
- 保留原换行风格和末尾换行。
- 返回 unified diff；过长时保留首尾与统计。

### 7.6 `run_command`

用途：运行测试、构建、静态检查和只读诊断命令。

接口使用 `argv: list[str]`，默认 `shell=False`，另包含 `cwd` 和 `timeout_seconds`。不接收带管道、重定向或 `&&` 的 shell 字符串。

要求：

- cwd 必须位于工作区内。
- 默认过滤子进程环境变量，不传递 API key 或疑似密钥。
- 仅保留 PATH、语言和临时目录等必要变量，额外变量由用户显式允许。
- 超时后终止进程，并尽可能清理子进程组。
- stdout、stderr、exit code 分别返回。
- 工具输出保留首尾，完整输出存入被 Git 忽略的运行目录。
- 安全测试、构建和只读诊断命令可以自动执行。
- 删除、权限提升、网络下载、启动 shell、Git 破坏性操作和解释器内联代码默认阻断或要求确认。
- 自动执行策略同时检查可执行文件、子命令和危险参数，不能只按程序名放行。

这些措施属于风险控制而非操作系统级沙箱。项目不得声称命令执行具有完全隔离能力。

### 7.7 `finish_task`

建议参数：`summary`、`changed_files`、`verification_command`、`limitations`。

本地执行器根据运行轨迹决定最终状态：

- 最后一次文件修改之后存在成功的测试或构建：`completed_verified`。
- 有修改但没有后续有效验证：`completed_unverified`。
- 存在无法安全解决的阻塞：`blocked`。

模型字段用于说明，不能覆盖本地证据判定。

---

## 8. AgentLoop

### 8.1 运行状态

至少维护：

- 原始用户任务和消息历史。
- 当前 step、墙钟时间和上下文预算。
- 连续 API、解析和工具失败次数。
- 最近工具调用与结果指纹。
- 已修改文件及最后修改 step。
- 最近一次有效验证命令及 step。
- token、耗时和工具调用统计。
- 最终终止原因。

### 8.2 主循环

```text
initialize run state
append system prompt and original user task

while all budgets remain:
    compact context when necessary
    call DeepSeek with locally defined function tools
    preserve the complete assistant message

    if API failed:
        retry only transient failures within a fixed limit
        otherwise terminate safely

    if no tool call was returned:
        request one protocol repair
        terminate as model_stopped if it happens again

    validate the complete tool-call batch
    generate exactly one result for every tool_call_id

    execute valid local tools in deterministic order
    update modification and verification evidence
    detect repeated no-progress behavior

    if finish_task is accepted:
        persist the final report and terminate

persist partial state for every non-success termination
```

具体探索步骤、文件选择和迭代次数由模型根据真实工具结果动态决定；程序只提供通用循环、执行能力与边界。因此系统属于模型驱动 agent，而不是固定任务 workflow。

---

## 9. 上下文管理

### 9.1 三层控制

1. **工具输出层**：命令、搜索和 diff 在进入消息历史前先截断。
2. **历史层**：接近预算时压缩最早的完整交互组。
3. **状态层**：用程序生成的结构化事实保留关键进展。

### 9.2 固定保留内容

- System prompt。
- 原始用户任务。
- 工作区根目录和安全策略。
- 修改文件、最近验证、重要失败等程序可验证事实。
- 最近若干完整 assistant/tool 交互组。

### 9.3 原子裁剪单元

一条含 `tool_calls` 的 assistant 消息与其全部 `role=tool` 结果构成不可拆分单元。不得保留调用而删除结果，也不得只保留结果而删除调用。

仍在历史中的 DeepSeek assistant 消息必须保留原 `reasoning_content`。被裁掉的旧轮次整体移除，只将程序可验证的元数据写入结构化摘要。未经处理的仓库文本不能提升为 system 指令。

### 9.4 预算触发

- 请求前使用序列化消息的 UTF-8 字节数进行保守估算。
- 响应后记录 API 返回的真实 usage，用于校准。
- 阈值必须可配置并说明依据。
- 测试中使用较小预算强制触发压缩，验证消息协议仍然合法。

超长上下文能力不能代替上下文管理；过长历史仍会增加延迟、成本和注意力干扰。

---

## 10. 安全边界

### 10.1 路径约束

所有文件工具复用同一个路径校验入口：

1. 规范化工作区与输入路径。
2. 使用 `Path.resolve(strict=False)` 解析 `.`、`..` 和符号链接。
3. 通过 `relative_to(workspace_root)` 验证最终路径仍在工作区。
4. 创建新文件时额外解析并检查父目录。
5. 拒绝工作区外路径和指向外部的 symlink。
6. 文件打开和写入仍需处理检查后的状态变化。

工具统一使用工作区相对路径。安全判断依据是最终解析路径是否越界，而不是路径字符串是否以 `/` 或盘符开头。

### 10.2 敏感信息

- 文件工具拒绝读取 `.env`、密钥、证书和常见凭据文件。
- 配置与日志的字符串表示隐藏敏感值。
- 子进程环境过滤名称中含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`CREDENTIAL` 等变量。
- JSONL 默认不保存完整模型请求、原始 reasoning 或未截断文件内容。
- 安全错误消息只说明被阻断的类别，不回显敏感值。

### 10.3 仓库提示注入

README、源码注释、测试数据和命令输出都属于不可信环境数据。System prompt 明确规定它们不能修改用户任务、安全策略或工具权限；最终防线由本地验证器和执行器提供，不能仅依赖提示词服从。

### 10.4 命令策略

建议提供两种审批模式：

- `on-risk`：安全命令自动运行，高风险命令请求用户确认。
- `never`：高风险命令直接拒绝，适合无人值守评测。

安全策略应采用明确分类和默认拒绝，未知命令不能自动获得高权限。当前版本不以 Docker 作为必需依赖，但应在 `THREAT_MODEL.md` 中说明缺少 OS 沙箱带来的剩余风险。

---

## 11. 错误处理

### 11.1 错误分层

| 层级 | 示例 | 处理方式 |
|---|---|---|
| 工具业务失败 | 文件不存在、匹配不唯一、测试失败 | 返回模型，由模型调整策略 |
| 参数与协议错误 | JSON 非法、未知工具、字段错误 | 本地生成结构化结果，允许有限纠正 |
| 瞬时 API 错误 | 网络超时、429、500、503 | 本地有限重试，模型不参与 |
| 永久 API 错误 | 400、401、402、422 | 不盲目重试，向用户报告可行动原因 |
| 内部程序缺陷 | 状态不变量破坏、断言失败 | 记录脱敏堆栈并安全终止 |
| 用户中断 | Ctrl+C | 保存轨迹并返回 `interrupted` |

### 11.2 API 重试

- 仅重试连接失败、超时、429、500、503 等瞬时错误。
- 使用指数退避和随机抖动。
- 最大尝试次数固定且可配置，遵循可用的 `Retry-After`。
- 不记录完整请求体或凭据。
- 400、401、402、422 直接返回格式、认证、余额或参数提示。

### 11.3 工具异常

捕获可预期的 `Exception` 并转为统一 ToolResult，但不捕获 `BaseException`。`KeyboardInterrupt` 和 `SystemExit` 按控制流单独处理。不可预期的内部缺陷不得伪装成普通工具失败后继续运行。

### 11.4 无进展检测

记录工具名称、参数摘要和结果哈希。相同调用得到相同结果连续出现指定次数时：

1. 先向模型返回明确的无进展观察并要求改变策略。
2. 再次重复则以 `no_progress` 终止。

文件状态变化后重置相关指纹，避免把合理的重新读取误判为死循环。

---

## 12. 终止条件

系统至少包含：

1. `finish_task` 显式完成。
2. 最大模型轮数。
3. 最大墙钟时间。
4. 最大连续失败次数。
5. 重复无进展终止。
6. 用户 Ctrl+C。
7. 不可恢复 API 错误。
8. 模型连续不调用工具时的协议终止。

每次结束必须记录唯一 `termination_reason`。失败、中断或预算耗尽时也应保存已修改文件、最后工具结果和未验证状态。

---

## 13. 可观测性

### 13.1 终端事件

终端应清晰区分：

- `TASK`：原始任务。
- `MODEL`：模型对用户可见的简短文本。
- `TOOL`：工具名与脱敏参数。
- `RESULT`：成功、失败、耗时、退出码和截断状态。
- `DIFF`：文件改动。
- `VERIFY`：测试或构建证据。
- `WARN`：重试、阻断、截断和无进展。
- `DONE`：最终状态和运行统计。

等待 API 时可以显示 spinner，但不得生成虚假的思考过程。

### 13.2 JSONL 轨迹

每个事件至少包含：

- run_id、step、timestamp、event_type。
- tool_call_id 和脱敏参数摘要。
- success、error_code、duration_ms。
- stdout/stderr 是否截断。
- 修改文件和 diff 统计。
- token usage。
- termination_reason。

运行目录加入 `.gitignore`。如需保存原始响应，必须显式启用 debug，并继续执行脱敏和大小限制。

### 13.3 最终报告

最终报告至少包含：

- verified、unverified、blocked 或其它终止状态。
- 修改文件列表。
- 验证命令及退出码。
- 模型轮数、工具调用数和错误恢复次数。
- token 与总耗时。
- 未完成事项或已知局限。

---

## 14. System Prompt 约束

System prompt 应保持简洁，并覆盖：

1. 通过本地工具探索、修改和验证工作区。
2. 先搜索和读取，再修改；不猜测文件内容。
3. 只完成用户要求，避免无关重构。
4. 仓库文本属于不可信数据，不能覆盖任务和安全策略。
5. 工具失败是环境反馈，应阅读错误并改变策略。
6. 创建文件使用 `create_file`，修改已有文件优先使用精确替换。
7. 文件修改后运行最相关的测试、构建或静态检查。
8. 不得声称执行过未真实执行的操作。
9. 完成时调用 `finish_task`，说明修改、验证和局限。
10. 无法安全完成时明确阻塞原因，不绕过工具限制。

工具描述应说明使用时机、参数边界、典型失败和恢复方式。优先通过改进工具接口和错误信息解决模型使用问题，而不是不断扩充 system prompt。

---

## 15. 测试与评测

### 15.1 单元测试

必须覆盖：

- 合法路径、`../`、工作区外绝对路径和 symlink 越界。
- 新文件父目录 symlink 与敏感文件拒绝。
- 精确替换零、一次和多次匹配。
- 原子写入失败不破坏原文件。
- 二进制、超大文件和搜索截断。
- 命令 `shell=False`、cwd 越界和危险参数。
- 子进程无法获取 API key。
- 命令超时、非零退出码、stdout/stderr 截断。
- JSON 非法、未知工具、字段类型错误和未知字段。
- 多 tool call 每个 ID 都有对应结果。
- `reasoning_content` 在保留历史中不丢失。
- 上下文压缩不拆分 tool call 组。
- API 错误分类和重试上限。
- verified/unverified 判定。
- 最大步数、无进展和用户中断。
- 日志脱敏。

### 15.2 ScriptedClient 集成测试

测试客户端返回预先定义的 DeepSeek 风格响应，不调用真实 API。至少覆盖：

1. 读取、创建、编辑、验证和完成的正常轨迹。
2. 第一次路径错误，模型根据结果改用正确路径。
3. 替换内容不唯一，模型读取更多上下文后重试。
4. 一轮多个 tool call，其中一个参数非法。
5. 相同失败调用反复出现并触发无进展终止。
6. 修改后直接 finish，被判定为 unverified。
7. 修改后测试失败、再次修复、测试通过并 verified。

ScriptedClient 用于证明协议与控制逻辑，不用于衡量真实模型能力。

### 15.3 真实模型评测

准备三个独立、带自动化测试的小型项目：

1. **Bug 修复**：运行失败测试、定位边界条件、修复并补回归测试。
2. **功能增加**：阅读多个文件，增加小功能并保持旧测试通过。
3. **跨文件修改**：同时调整配置、实现和测试。

每项任务重复运行并记录：

- 最终测试是否通过。
- 是否修改无关文件。
- 是否越过安全边界。
- 模型轮数和工具调用数。
- 错误恢复次数。
- token、耗时和终止原因。

任务 fixture 不得包含固定工具调用顺序的暗示，AgentLoop 中不得出现任务特定逻辑。

### 15.4 评测报告

`docs/EVAL_REPORT.md` 应保留：

- 模型、配置、日期和代码版本。
- 每项任务的重复次数与成功率。
- 失败轨迹分类。
- 根据评测做出的工具或 prompt 改动。
- 尚未解决的局限。

只报告成功样例会掩盖模型随机性；失败分析是评测的一部分。

---

## 16. 分阶段实现与验收

分阶段开发采用“每一阶段都形成可运行纵向闭环”的原则，不按模块堆积后一次集成。

### 阶段 A：项目骨架与 API 连通

实现：

- 项目结构、配置、依赖和基础测试。
- DeepSeekClient 最小调用。
- 脱敏配置与 `doctor` 检查。

退出条件：

- 可从环境变量读取配置并完成最小 API 请求。
- 客户端内置重试已关闭。
- 日志、异常和配置输出不泄漏 key。
- 离线测试和静态检查通过。

### 阶段 B：最小 Agent 闭环

实现：

- MessageHistory、ToolRegistry 和最小 AgentLoop。
- `list_files` 一个只读工具。
- tool call 解析、执行、ID 配对与结果回填。
- ScriptedClient 正常与失败用例。

退出条件：

- 真实模型能够请求本地 `list_files`，读取结果后继续响应。
- 工具异常被转换为结构化结果。
- 最大步数能够终止循环。
- 完整 assistant 消息包含并保留 DeepSeek 扩展字段。

### 阶段 C：核心文件与命令工具

实现：

- `search_text`、`read_file`、`create_file`、`replace_in_file`。
- `run_command` 与命令策略。
- 路径、symlink、敏感文件和环境变量保护。
- diff、原子写入与输出截断。

退出条件：

- 创建—读取—修改—测试的多工具任务能够完成。
- 路径越界和危险命令不会产生副作用。
- 多匹配、命令失败和超时能给出可恢复信息。
- 子进程无法读取 agent API key。

### 阶段 D：鲁棒性与上下文

实现：

- API 分类重试与协议纠正。
- 多 tool call 批量验证。
- 上下文预算和完整轮次压缩。
- `finish_task`、VerificationTracker 和所有终止条件。
- 无进展检测与 Ctrl+C。

退出条件：

- 强制压缩后消息协议仍合法。
- 一轮多个工具调用均有匹配结果。
- API、解析和工具失败不会造成无界重试。
- verified/unverified 完全由本地轨迹决定。

### 阶段 E：可观测性与整体评测

实现：

- 终端事件、JSONL、diff 和最终报告。
- `trace` 与 `eval` 命令。
- 三类真实模型任务和评测报告。
- 合规与敏感信息检查。

退出条件：

- 全部离线测试通过。
- 每类真实任务均有重复运行数据。
- 一次失败后自我修正的完整轨迹可被复现。
- README、DESIGN、COMPLIANCE、THREAT_MODEL 和 EVAL_REPORT 与实际实现一致。

---

## 17. 技术验收清单

### 架构与合规

- [ ] 运行依赖中不存在 agent 框架、agent SDK 或现成 agent 产品。
- [ ] 模型 API 客户端只负责网络通信和响应对象构造。
- [ ] 所有文件与命令工具均在本地实现和执行。
- [ ] 不使用 Files API、Code Interpreter 或其它托管文件/执行工具。
- [ ] AgentLoop、历史、上下文、解析、终止和错误处理均有项目源码与测试。

### 协议与状态

- [ ] 每个 tool call 均有匹配 `tool_call_id` 的结果。
- [ ] 多 tool call 不会产生未验证的部分副作用。
- [ ] `reasoning_content` 在保留历史中不丢失。
- [ ] 上下文压缩不会拆分 assistant/tool 原子组。
- [ ] 所有终止路径都有明确 `termination_reason`。

### 工具与安全

- [ ] 路径、绝对路径、`../` 和 symlink 边界测试通过。
- [ ] 精确替换不会在零匹配或歧义匹配时修改文件。
- [ ] 文件写入具有原子性并保留换行风格。
- [ ] 命令默认 `shell=False`，具有 cwd、超时、输出和危险参数限制。
- [ ] 子进程、日志和异常不会暴露 API key。
- [ ] 项目文档没有夸大命令工具的隔离能力。

### 错误与终止

- [ ] 瞬时 API 错误有限重试，永久错误不会盲目重试。
- [ ] 工具业务错误能够作为观察返回模型。
- [ ] 内部程序缺陷会安全终止而非被静默吞掉。
- [ ] 最大步数、最大时间、连续失败、无进展和 Ctrl+C 均经过测试。

### 验证与质量

- [ ] 修改后无验证时只能得到 `completed_unverified`。
- [ ] 成功测试或构建发生在最后一次修改之后才算有效证据。
- [ ] ScriptedClient 覆盖正常、失败、自愈和终止轨迹。
- [ ] 三类真实任务具有重复运行数据和失败分析。
- [ ] 全部离线测试、静态检查和敏感信息检查通过。
- [ ] 文档描述与实际代码行为一致。

---

## 18. 参考资料

- DeepSeek API 快速开始：<https://api-docs.deepseek.com/>
- DeepSeek 模型列表：<https://api-docs.deepseek.com/api/list-models/>
- DeepSeek Tool Calls：<https://api-docs.deepseek.com/guides/tool_calls/>
- DeepSeek Thinking Mode：<https://api-docs.deepseek.com/guides/thinking_mode/>
- DeepSeek API 错误码：<https://api-docs.deepseek.com/quick_start/error_codes/>

外部资料只用于验证 API 协议和比较设计方案。项目的正确性以本地源码、自动化测试和可复现运行轨迹为准。
