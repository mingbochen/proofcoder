# ProofCoder Stage E 真实模型评测报告

本报告记录 Stage E2a-4 的真实模型评测证据。结论基于仓库中已经保存的三个本地评测 artifact；本次报告编写没有重新运行 `proofcoder eval`，也没有调用模型 API。

正式评测 `b6f7ae7f5c0549ca96b7c66971c55d7b` 在干净 revision `ef1ded6293229c11b70076b3cb7107b470fb6d43` 上完成了 3 个 fixture、每项 3 次的评测。记录结果为 9/9 成功，成功率 100%。这个结果只描述本报告中的任务集、模型配置和运行环境，不代表 ProofCoder 对任意仓库都可靠。

---

## 1. 评测目的与成功判定

评测覆盖开发规范 15.3 定义的三类小型真实模型任务：bug 修复、功能增加和跨文件修改。目的不是检查模型是否输出了“完成”文本，而是检查一次 agent 轨迹是否产生了可由本地程序独立复核的正确修改。

一个 attempt 只有同时满足以下条件才计为成功：

1. AgentLoop 根据本地轨迹判定为 `completed_verified`，即最后一次文件修改之后存在成功的验证证据。
2. 评测器在 agent 结束后独立再次运行 fixture 的最终验证命令，且退出码为 0。
3. fixture 声明的每个 `required_modified_files` 都真实发生修改。
4. 修改集合不包含 `allowed_modified_files` 之外的文件；缺失必改文件或出现越界文件都会形成失败原因。
5. run ID、trace 路径和终止事件可解析，且 `trace_complete = true`。

因此，模型调用 `finish_task` 是结束请求，不是成功证明。完成状态、独立测试、文件范围和 trace 完整性共同构成成功判定。

---

## 2. 可复现配置

以下字段来自正式 artifact `.proofcoder/evals/b6f7ae7f5c0549ca96b7c66971c55d7b/metadata.json` 和 `summary.json`。

| 配置项 | 正式值 |
|---|---|
| UTC 开始时间 | `2026-08-29T07:01:15.109862Z` |
| UTC 完成时间 | `2026-08-29T07:02:51.459523Z` |
| 代码 revision | `ef1ded6293229c11b70076b3cb7107b470fb6d43` |
| `code.dirty` | `false` |
| 模型 | `deepseek-v4-flash` |
| API base URL | `https://api.deepseek.com` |
| reasoning effort | `high` |
| 每项重复次数 | 3 |
| 每个 attempt 最大模型步数 | 8 |
| 每个 attempt 最大运行时间 | 600 秒 |
| context budget | 262144 bytes |
| 最大连续失败批次 | 5 |
| 每个模型响应最大 API attempts | 3 |
| 独立验证超时 | 60 秒 |

正式运行命令如下；命令本身不包含凭据：

```powershell
.\.venv\Scripts\uv.exe run --locked --env-file .env proofcoder eval --repeat 3
```

`.env` 是本地、被 Git 忽略的环境文件；上述命令只包含文件名，不包含任何凭据值。如果已经安装 ProofCoder，并通过环境变量提供配置，可以使用等价命令 `proofcoder eval --repeat 3`。正式评测命令会进行真实模型调用，不属于离线验收命令。

---

## 3. 任务集

三个 fixture 都使用独立的小型 Python workspace。任务文本只描述目标，不指定固定工具调用顺序；通用 AgentLoop 中也没有按 fixture ID 或任务类别分支的任务特判。评测器会先确认 fixture 的初始失败证据，再运行 agent，最后执行同一条命令进行独立验证。

| Fixture | 类别 | 任务目标 | 必改文件 | 独立验证命令 |
|---|---|---|---|---|
| `bugfix-inclusive-total` | `bug_fix` | 修复 `inclusive_total` 的闭区间上界，并增加单值区间回归测试，同时保留已有行为 | `inclusive_total.py`；`tests/test_inclusive_total.py` | `python -m unittest discover -s tests -v` |
| `cross-file-message-format` | `cross_file_change` | 在设置中增加默认分隔符 `" | "`，让消息格式化读取该设置，并覆盖空消息 | `message.py`；`settings.py`；`tests/test_message.py` | `python -m unittest discover -s tests -v` |
| `feature-available-items` | `feature_addition` | 让 `available_names` 按原顺序只返回有库存的名称，增加空库存回归测试，并保持 `total_units` 不变 | `inventory.py`；`tests/test_inventory.py` | `python -m unittest discover -s tests -v` |

每个 fixture 的初始验证预期退出码为 1，成功验证预期退出码为 0。初始输出还必须包含 fixture 指定的失败或错误标识，避免把本来已经通过或以其它方式损坏的 workspace 当作有效起点。

---

## 4. 正式 3×3 结果

下表直接转录正式 `summary.json` 的逐 fixture 聚合值。由于 API retries 均为 0，API attempts 与 model calls 相同。

| Fixture | Attempts | Successes | Success rate | Model calls（API attempts） | Tool calls | API retries | Tool errors | Context compactions | Input tokens | Output tokens | Elapsed (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bugfix-inclusive-total` | 3 | 3 | 100% | 16（16） | 23 | 0 | 0 | 0 | 55992 | 3456 | 32.625 |
| `cross-file-message-format` | 3 | 3 | 100% | 19（19） | 31 | 0 | 0 | 0 | 75281 | 3692 | 33.328 |
| `feature-available-items` | 3 | 3 | 100% | 16（16） | 23 | 0 | 0 | 0 | 57187 | 3282 | 26.594 |
| **Overall** | **9** | **9** | **100%** | **51（51）** | **77** | **0** | **0** | **0** | **188460** | **10430** | **92.547** |

`elapsed_seconds` 是各 attempt 在运行结果中记录的 agent 耗时之和，不等同于 `started_at` 与 `completed_at` 之间包含评测编排和独立验证开销的总墙钟时间。

---

## 5. 正确性与安全证据

正式 `attempts.jsonl` 的 9 条记录提供了以下事实：

- 9/9 的 `completion_status` 均为 `completed_verified`。
- 9/9 的 `termination_reason` 均为唯一值 `finish_task`。
- 9/9 的独立最终验证均未超时、无错误码且退出码为 0。
- 每次 attempt 的 `changed` 恰好覆盖对应 fixture 的必改文件；`missing_required` 与 `unexpected` 均为空。
- 9/9 的 `trace_complete` 均为 `true`，run ID 和 trace 路径各自有 9 个唯一值，没有 attempt 共用同一条轨迹。
- 汇总中的 `failure_reason_counts` 为空，attempt 中的 `failure_reasons` 也均为空。
- 评测过程中产生的 trace、命令审计文件和 Python bytecode 被显式列入 `ignored_runtime`，没有混入被评分的源码修改集合。

对正式 artifact `.proofcoder/evals/b6f7ae7f5c0549ca96b7c66971c55d7b` 下全部 `.json`、`.jsonl` 和 `.txt` 文件进行了字面量扫描：`DEEPSEEK_API_KEY`、`Authorization` 和 `reasoning_content` 的匹配数均为 0。递归枚举还确认该 artifact 中不存在名为 `.env` 或 `.venv` 的文件或目录。该检查只报告这些明确标记和名称的扫描结果，不证明不存在任何可能形式的秘密；本次核验没有读取完整模型 reasoning 或输出其正文。

修复前 false-negative artifact 和修复后 smoke artifact 的报告取证仍只使用各自的 `metadata.json`、`summary.json` 和 `attempts.jsonl`，没有对它们声称相同的全目录扫描。

这些证据说明评测器的路径约束、命令策略、环境过滤和结构化评分在本任务集上没有记录到越界结果。它们不是操作系统级沙箱或完全隔离的证明；ProofCoder 的命令策略仍属于应用层风险控制。

---

## 6. 失败轨迹与修复

### 6.1 修复前的表面失败

旧评测 `.proofcoder/evals/5626e986bd884f6f998754381d93c315` 在 `2026-08-29T03:17:25.188317Z` 开始，记录的 revision 为 `fe1555762ebdab6d84a41bed33fa47332235d0ba`，且 `code.dirty = true`。其汇总表面结果为 0/9，成功率 0%。

但逐 attempt 证据显示：

- 9/9 的 agent 状态都是 `completed_verified`。
- 9/9 都以 `finish_task` 终止，trace 完整，独立最终验证退出码均为 0。
- 每条记录唯一的失败原因都是 `unexpected_files`。
- `unexpected` 只包含 `__pycache__/*.pyc` 或 `tests/__pycache__/*.pyc`；任务要求的源码和测试文件已经正确修改，`missing_required` 为空。

据此可以把 0/9 诊断为评测基础设施 false negative，而不是模型任务失败：`unittest` 在初始验证、agent 验证或最终验证过程中生成或更新 Python bytecode，旧快照评分器把这些 runtime artifact 当成了源码范围外修改。

### 6.2 评分器修复与复核

修复发生在评测器，而不是模型 prompt，因为错误属于 workspace 快照和评分逻辑。当前实现具有以下行为：

1. 先对 workspace entry 执行 symlink、解析后路径归属、文件类型和读取安全检查，再判断是否属于可忽略的 runtime artifact；“忽略”不会绕过安全检查。
2. runtime artifact 采用精确、区分大小写的路径分类，包括指定 cache 目录、`.pyc`/`.pyo`、`.proofcoder` 运行目录和覆盖率产物；近似名称不会被宽泛忽略。
3. 被忽略但发生变化的 runtime 文件通过 `files.ignored_runtime` 显式写入 attempt JSON，保留可审计性。
4. 汇总增加 `failure_reason_counts`，使失败分类可聚合。
5. 终端 `RESULT` 行显示 `reasons=...`，使单次失败原因无需读取完整轨迹即可识别。

修复后的单次 smoke `.proofcoder/evals/5c8dfea266f14cce9b60869dff962692` 在 `2026-08-29T06:51:02.488174Z` 开始，记录的 revision 仍为 `fe1555762ebdab6d84a41bed33fa47332235d0ba`，且 `code.dirty = true`。它对 `bugfix-inclusive-total` 运行 1 次并得到 1/1 成功；bytecode 和运行文件进入 `ignored_runtime`，`unexpected` 为空。

随后，干净提交上的正式评测 `b6f7ae7f5c0549ca96b7c66971c55d7b` 得到 9/9。修复前 revision 与正式 revision 之间的仓库差异不包含 `src/proofcoder/prompt.py`，因此没有为这个评分问题修改模型 prompt。

---

## 7. 视频候选

`cross-file-message-format` 连续 3 次成功，且每次都真实修改了配置 `settings.py`、实现 `message.py` 和测试 `tests/test_message.py`，独立最终验证均退出 0。它同时展示跨文件理解、配置传递、实现调整和回归测试，适合作为后续视频候选。

本报告只标记候选；没有证据表明视频已经录制。

---

## 8. 局限

- 任务集只有三个小型 Python fixture，不能代表真实大型仓库的复杂度和依赖结构。
- 每项只有三次重复，样本量不足以精确估计低概率失败。
- 只评测了 `deepseek-v4-flash` 和单一运行环境，没有模型间或跨平台比较。
- 本任务集上的 100% 成功率不代表对任意仓库、语言或任务都可靠。
- 命令策略是应用层允许、拒绝、超时、路径和环境过滤机制，不是 OS 级沙箱。
- 原始 artifact 默认保存在本地、被 Git 忽略的 `.proofcoder/evals/`；Git 中保留的是评测实现、fixture 和本汇总报告，不要求也不应提交 `.proofcoder` 运行产物。
- 尚未覆盖大型仓库、非 Python 工程、并发评测及长期稳定性。
- 本报告验证的是已保存结构化证据的一致性，没有重放真实 API 请求，也没有对完整模型文本进行内容评审。

---

## 9. 复现与查看证据

本次 Windows/PowerShell 正式评测在仓库根目录实际使用：

```powershell
.\.venv\Scripts\uv.exe run --locked --env-file .env proofcoder eval --repeat 3
```

`.env` 是本地、被 Git 忽略的环境文件，命令只引用文件名，不包含凭据值。如果已经安装 ProofCoder，并通过环境变量提供配置，可以使用等价命令：

```powershell
proofcoder eval --repeat 3
```

命令会为运行生成新的 eval ID；不要覆盖本文使用的 artifact。使用输出中的 `<eval-id>` 查看结构化结果：

```powershell
Get-Content .proofcoder/evals/<eval-id>/summary.json
Get-Content .proofcoder/evals/<eval-id>/attempts.jsonl
```

`attempts.jsonl` 的每条记录给出 `workspace`、`run_id` 和 `trace_path`。可在相应 attempt workspace 中通过安全 trace 回放命令查看轨迹，例如：

```powershell
proofcoder trace show --workspace .proofcoder/evals/<eval-id>/<sequence>/w <run-id>
```

`<sequence>` 使用 artifact 中的三位目录名，例如 `001`。查看证据不要求把 `.proofcoder/evals/` 加入 Git；这些本地运行产物应继续保持 ignored。

---

## 10. 证据索引

本报告使用以下 eval ID：

| 用途 | Eval ID | 结构化结果 |
|---|---|---|
| 修复前基础设施 false negative | `5626e986bd884f6f998754381d93c315` | 表面 0/9；9 次均已验证完成，统一被 `unexpected_files` 误判 |
| 修复后单次 smoke | `5c8dfea266f14cce9b60869dff962692` | 1/1 成功 |
| 干净提交正式评测 | `b6f7ae7f5c0549ca96b7c66971c55d7b` | 9/9 成功，成功率 100% |

每个 eval 的证据范围均限定为其 `metadata.json`、`summary.json` 和 `attempts.jsonl`。本报告中的事实值来自这些文件；关于旧 0/9 的“基础设施 false negative”属于根据 attempt 文件范围、验证退出码和当前评分实现作出的诊断。
