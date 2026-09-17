/* ProofCoder local interface.

   The page is a presentation layer: it starts one bounded run, long-polls the same
   sanitized events the JSONL trace receives, and renders them. It never interprets
   completion itself - the status shown is the status the local run reported. */

"use strict";

const TOKEN = document.querySelector('meta[name="proofcoder-token"]').content;
const STORE_KEY = "proofcoder.ui.v1";
const POLL_SECONDS = 20;

const TEXT = {
  zh: {
    newTask: "新任务",
    workspace: "工作区",
    history: "运行历史",
    session: "会话",
    sessionNew: "新建",
    sessionNone: "不使用会话",
    sessionOpen: "进行中",
    sessionEnded: "已结束",
    sessionRuns: "次运行",
    sessionNote: "会话把前几次运行的程序记录带进下一次运行。验证证据不会被继承：本次运行仍需自己取得验证。",
    sessionFailed: "会话操作失败",
    streaming: "生成中",
    stream: "流式输出",
    checking: "正在检查…",
    runDoctor: "自检",
    theme: "主题",
    openSidebar: "展开侧栏",
    closeSidebar: "收起侧栏",
    runSettings: "运行设置",
    startRun: "开始运行",
    stopRun: "停止运行",
    welcomeTitle: "今天想让 ProofCoder 做什么？",
    welcomeBody:
      "描述一个编程任务。ProofCoder 会在你选定的工作区里读取、修改文件并运行本地验证，完成状态只由真实执行结果决定。",
    composerPlaceholder: "描述一个任务，例如：为 utils.py 补充单元测试并运行",
    composerHint: "Enter 发送 · Shift + Enter 换行 · 运行前请选择一个可丢弃的工作区",
    chooseWorkspace: "选择工作区",
    workspacePlaceholder: "输入或粘贴目录路径",
    use: "使用",
    parent: "上级目录",
    recent: "最近使用",
    settingsNote: "这些上限与命令行参数一一对应，用于限制一次运行的成本和影响范围。",
    reset: "恢复默认",
    save: "保存",
    doctorTitle: "环境自检",
    offlineCheck: "仅本地检查",
    onlineCheck: "连接检查",
    noHistory: "该工作区还没有运行记录",
    noSubdirectories: "没有子目录",
    workspaceMissing: "请先选择一个工作区",
    workspaceNotFound: "目录不存在",
    workspaceNotDirectory: "该路径不是目录",
    workspaceReadonly: "目录不可写，运行可能失败",
    workspaceOk: "目录可用",
    apiKeyMissing: "未检测到 DEEPSEEK_API_KEY，运行会以配置错误结束",
    running: "正在运行",
    stopping: "正在停止…",
    thinking: "正在思考…",
    stepLabel: "第 {n} 步",
    task: "任务",
    model: "模型回复",
    toolCall: "调用工具",
    toolResult: "工具结果",
    diff: "文件改动",
    verification: "本地验证",
    warning: "警告",
    finished: "运行结束",
    accepted: "已接受",
    rejected: "未接受",
    statusVerified: "已验证完成",
    statusUnverified: "未验证完成",
    statusNoChanges: "未修改文件",
    statusBlocked: "已阻塞",
    statusNone: "无完成声明",
    changedFiles: "改动文件",
    noChangedFiles: "无",
    traceIncomplete: "轨迹不完整，不能作为完整证据",
    events: "事件",
    modelCalls: "模型调用",
    toolCalls: "工具调用",
    toolErrors: "工具错误",
    elapsed: "耗时",
    tokensIn: "输入 token",
    tokensOut: "输出 token",
    seconds: "秒",
    loadFailed: "请求失败，请确认本地服务仍在运行",
    startFailed: "无法开始运行",
    busy: "该工作区已有运行在进行中",
    cancelSent: "已请求停止，会在下一个检查点结束",
    replayTitle: "历史运行",
    liveTitle: "当前运行",
    checkpointTitle: "运行前检查点",
    checkpointEntries: "已记录文件",
    checkpointBytes: "已存字节",
    checkpointUncovered: "未覆盖",
    checkpointNone: "本次运行没有检查点，写入无法撤销",
    rollbackButton: "回滚这次运行",
    rollbackTitle: "回滚清单",
    rollbackLoading: "正在读取回滚清单…",
    rollbackEmpty: "工作区与运行前一致，没有需要撤销的改动",
    rollbackConfirm: "确认回滚",
    rollbackCancel: "取消",
    rollbackNotCovered: "不在覆盖范围内，将保持原样",
    rollbackByTool: "工具写入",
    rollbackChanged: "工作区在清单生成后发生了变化，请重新确认",
    rollbackDone: "回滚完成",
    rollbackPartial: "回滚未全部完成",
    rollbackFailedPaths: "未能恢复",
    rollbackBusy: "该工作区有运行在进行中，先停止它再回滚",
    rollbackMissing: "这次运行没有可用的检查点",
    approvalTitle: "需要你确认后才会执行",
    approvalApprove: "允许运行",
    approvalDeny: "拒绝",
    approvalWaiting: "等待你的决定…",
    approvalStale: "待确认的命令已经变了，请重新查看后再决定",
    approvalGone: "这条请求已经结束，无需再决定",
    approvalPolicy: "命令策略",
    approvalPolicyNone: "未加载项目策略",
    approvalOutcome: {
      approved: "已批准并执行",
      denied: "已拒绝，命令未执行",
      timed_out: "超时未决定，命令未执行",
      interrupted: "运行被中断，命令未执行",
    },
    actionRestore: "恢复",
    actionRecreate: "重建",
    actionDelete: "删除",
    actionCreateDirectory: "新建目录",
    actionRemoveDirectory: "删除目录",
    suggestions: [
      "在工作区里创建 hello.py，打印一行问候，并补一个单元测试后运行",
      "阅读工作区结构，找出没有测试覆盖的模块并补充测试",
      "运行现有测试，修复第一个失败用例",
    ],
  },
  en: {
    newTask: "New task",
    workspace: "Workspace",
    history: "Run history",
    session: "Session",
    sessionNew: "New",
    sessionNone: "No session",
    sessionOpen: "open",
    sessionEnded: "ended",
    sessionRuns: "runs",
    sessionNote: "A session carries the program's record of earlier runs into the next one. Verification is never inherited: this run must still earn its own.",
    sessionFailed: "Session request failed",
    streaming: "streaming",
    stream: "Stream output",
    checking: "Checking…",
    runDoctor: "Doctor",
    theme: "Theme",
    openSidebar: "Open sidebar",
    closeSidebar: "Close sidebar",
    runSettings: "Run settings",
    startRun: "Start run",
    stopRun: "Stop run",
    welcomeTitle: "What should ProofCoder work on?",
    welcomeBody:
      "Describe a coding task. ProofCoder reads and edits files in the workspace you choose and runs local verification; the completion status comes only from real execution.",
    composerPlaceholder: "Describe a task, for example: add unit tests for utils.py and run them",
    composerHint: "Enter to send · Shift + Enter for a new line · use a disposable workspace",
    chooseWorkspace: "Choose a workspace",
    workspacePlaceholder: "Type or paste a directory path",
    use: "Use",
    parent: "Parent",
    recent: "Recent",
    settingsNote:
      "These bounds mirror the command-line options and cap the cost and blast radius of one run.",
    reset: "Reset",
    save: "Save",
    doctorTitle: "Environment check",
    offlineCheck: "Local only",
    onlineCheck: "Check connection",
    noHistory: "No stored runs in this workspace yet",
    noSubdirectories: "No subdirectories",
    workspaceMissing: "Choose a workspace first",
    workspaceNotFound: "Directory does not exist",
    workspaceNotDirectory: "That path is not a directory",
    workspaceReadonly: "Directory is not writable; the run may fail",
    workspaceOk: "Directory is usable",
    apiKeyMissing: "DEEPSEEK_API_KEY is not set; runs end with a configuration error",
    running: "Running",
    stopping: "Stopping…",
    thinking: "Thinking…",
    stepLabel: "Step {n}",
    task: "Task",
    model: "Model",
    toolCall: "Tool call",
    toolResult: "Tool result",
    diff: "File change",
    verification: "Local verification",
    warning: "Warning",
    finished: "Run finished",
    accepted: "accepted",
    rejected: "not accepted",
    statusVerified: "Completed and verified",
    statusUnverified: "Completed, unverified",
    statusNoChanges: "Completed, no changes",
    statusBlocked: "Blocked",
    statusNone: "No completion claim",
    changedFiles: "Changed files",
    noChangedFiles: "none",
    traceIncomplete: "Trace is incomplete and is not complete evidence",
    events: "Events",
    modelCalls: "Model calls",
    toolCalls: "Tool calls",
    toolErrors: "Tool errors",
    elapsed: "Elapsed",
    tokensIn: "Input tokens",
    tokensOut: "Output tokens",
    seconds: "s",
    loadFailed: "Request failed; check that the local server is still running",
    startFailed: "Could not start the run",
    busy: "This workspace already has a run in progress",
    cancelSent: "Stop requested; the run ends at its next checkpoint",
    approvalTitle: "This command runs only if you allow it",
    approvalApprove: "Allow",
    approvalDeny: "Refuse",
    approvalWaiting: "Waiting for your decision…",
    approvalStale: "The pending command changed; review it again before deciding",
    approvalGone: "This request has already ended; no decision is needed",
    approvalPolicy: "Command policy",
    approvalPolicyNone: "no project policy loaded",
    approvalOutcome: {
      approved: "approved and executed",
      denied: "refused; the command did not run",
      timed_out: "no decision in time; the command did not run",
      interrupted: "the run was interrupted; the command did not run",
    },
    replayTitle: "Stored run",
    liveTitle: "Current run",
    checkpointTitle: "Pre-run checkpoint",
    checkpointEntries: "Files recorded",
    checkpointBytes: "Bytes stored",
    checkpointUncovered: "Not covered",
    checkpointNone: "This run has no checkpoint; its writes cannot be undone",
    rollbackButton: "Roll back this run",
    rollbackTitle: "Rollback plan",
    rollbackLoading: "Reading the rollback plan…",
    rollbackEmpty: "The workspace matches the pre-run state; there is nothing to undo",
    rollbackConfirm: "Apply rollback",
    rollbackCancel: "Cancel",
    rollbackNotCovered: "Not covered; left exactly as it is",
    rollbackByTool: "written by a tool",
    rollbackChanged: "The workspace changed after this plan was shown; review it again",
    rollbackDone: "Rollback finished",
    rollbackPartial: "Rollback did not finish completely",
    rollbackFailedPaths: "Not restored",
    rollbackBusy: "A run is using this workspace; stop it before rolling back",
    rollbackMissing: "This run has no stored checkpoint",
    actionRestore: "restore",
    actionRecreate: "recreate",
    actionDelete: "delete",
    actionCreateDirectory: "create directory",
    actionRemoveDirectory: "remove directory",
    suggestions: [
      "Create hello.py that prints a greeting, add a unit test, and run it",
      "Review the workspace layout and add tests for an uncovered module",
      "Run the existing tests and fix the first failing case",
    ],
  },
};

const LIMIT_FIELDS = [
  { key: "max_steps", zh: "最多模型回复数", en: "Maximum model responses" },
  { key: "max_seconds", zh: "最长运行秒数", en: "Maximum wall-clock seconds" },
  { key: "context_budget_bytes", zh: "上下文预算（字节）", en: "Context budget (bytes)" },
  { key: "max_consecutive_failures", zh: "连续失败批次上限", en: "Consecutive failed batches" },
  { key: "max_api_attempts", zh: "每次回复的 API 尝试数", en: "API attempts per response" },
];

const COMPLETION_TEXT = {
  completed_verified: ["statusVerified", "pill--ok"],
  completed_unverified: ["statusUnverified", "pill--warn"],
  completed_no_changes: ["statusNoChanges", "pill--info"],
  blocked: ["statusBlocked", "pill--bad"],
};

const state = {
  lang: "zh",
  theme: "system",
  workspace: "",
  recent: [],
  limits: {},
  limitBounds: {},
  status: null,
  activeRunId: null,
  viewedRunId: null,
  polling: false,
  renderedSteps: new Set(),
  approvalDigest: null,
  suggestionsShown: true,
  sessions: [],
  sessionId: "",
  stream: false,
};

const el = (id) => document.getElementById(id);
const dom = {};

/* ---------- storage ---------- */

function loadStore() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (error) {
    return {};
  }
}

function saveStore() {
  try {
    window.localStorage.setItem(
      STORE_KEY,
      JSON.stringify({
        lang: state.lang,
        theme: state.theme,
        workspace: state.workspace,
        recent: state.recent.slice(0, 8),
        limits: state.limits,
        stream: state.stream,
      })
    );
  } catch (error) {
    /* Private browsing or blocked storage: preferences simply do not persist. */
  }
}

/* ---------- helpers ---------- */

function t(key) {
  const table = TEXT[state.lang] || TEXT.en;
  return table[key] !== undefined ? table[key] : key;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function icon(path, size) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size || 14));
  svg.setAttribute("height", String(size || 14));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", path);
  svg.appendChild(shape);
  return svg;
}

const ICONS = {
  chevron: "m9 18 6-6-6-6",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  terminal: "m4 17 6-6-6-6M12 19h8",
  check: "M20 6 9 17l-5-5",
  alert: "M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  bot: "M12 8V4H8m-4 4h16v12H4zM9 13h.01M15 13h.01",
};

function toolIcon(name) {
  if (name === "run_command") return ICONS.terminal;
  if (name === "search_text") return ICONS.search;
  if (name === "finish_task") return ICONS.check;
  return ICONS.file;
}

function toast(message) {
  dom.toast.textContent = message;
  dom.toast.hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    dom.toast.hidden = true;
  }, 3200);
}

function banner(message, isError) {
  if (!message) {
    dom.banner.hidden = true;
    return;
  }
  dom.banner.textContent = message;
  dom.banner.classList.toggle("is-error", Boolean(isError));
  dom.banner.hidden = false;
}

function shortPath(path) {
  if (!path) return "—";
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

function formatTime(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString(state.lang === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/* ---------- transport ---------- */

async function api(path, options) {
  const settings = Object.assign({ method: "GET", headers: {} }, options || {});
  settings.headers = Object.assign({ "X-ProofCoder-Token": TOKEN }, settings.headers);
  if (settings.json !== undefined) {
    settings.headers["Content-Type"] = "application/json";
    settings.body = JSON.stringify(settings.json);
    delete settings.json;
  }
  const response = await fetch(path, settings);
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error((payload.error && payload.error.message) || t("loadFailed"));
    error.code = payload.error && payload.error.code;
    error.status = response.status;
    // Some errors carry usable data, such as the replacement plan behind PLAN_CHANGED.
    error.body = payload;
    throw error;
  }
  return payload;
}

/* ---------- localisation ---------- */

function applyLanguage() {
  document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((element) => {
    element.title = t(element.dataset.i18nTitle);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  dom.langButton.textContent = state.lang === "zh" ? "EN" : "中文";
  renderStatusLine();
  renderSuggestions();
  renderLimitFields();
  if (!state.viewedRunId && !state.activeRunId) {
    dom.threadTitle.textContent = t("newTask");
  }
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
}

/* ---------- status and workspace ---------- */

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    state.status = status;
    state.limitBounds = status.limits || {};
    LIMIT_FIELDS.forEach((field) => {
      if (state.limits[field.key] === undefined && state.limitBounds[field.key]) {
        state.limits[field.key] = state.limitBounds[field.key].default;
      }
    });
    renderStatusLine();
    const ok = status.api_key_configured && !status.configuration_error;
    if (!ok) banner(status.configuration_error || t("apiKeyMissing"), true);
    if (!state.workspace && status.default_workspace) {
      await setWorkspace(status.default_workspace, false);
    }
    renderLimitFields();
  } catch (error) {
    state.status = null;
    dom.statusDot.className = "status-dot is-bad";
    dom.statusText.textContent = t("loadFailed");
  }
}

function renderStatusLine() {
  const status = state.status;
  if (!status) return;
  const ok = status.api_key_configured && !status.configuration_error;
  dom.modelPill.textContent = status.model || "—";
  dom.statusDot.className = "status-dot " + (ok ? "is-ok" : "is-bad");
  dom.statusText.textContent = ok
    ? status.model
    : status.configuration_error || t("apiKeyMissing");
}

async function setWorkspace(path, announce) {
  if (!path) return;
  try {
    const info = await api("/api/workspace", { method: "POST", json: { path } });
    state.workspace = info.path;
    dom.workspaceName.textContent = info.name || shortPath(info.path);
    dom.workspacePath.textContent = info.path;
    dom.threadSubtitle.textContent = info.path;
    state.recent = [info.path].concat(state.recent.filter((item) => item !== info.path)).slice(0, 8);
    saveStore();
    if (!info.exists) {
      banner(t("workspaceNotFound"), true);
    } else if (!info.is_directory) {
      banner(t("workspaceNotDirectory"), true);
    } else if (!info.writable) {
      banner(t("workspaceReadonly"), true);
    } else if (state.status && state.status.api_key_configured) {
      banner("");
    }
    if (announce) toast(info.path);
    await refreshHistory();
    // Sessions live on disk beside the workspace, so this listing is also what
    // recovers them after the service restarts.
    await refreshSessions();
  } catch (error) {
    banner(error.message, true);
  }
}

async function refreshHistory() {
  if (!state.workspace) return;
  dom.historyList.replaceChildren();
  try {
    const data = await api(
      "/api/traces?workspace=" + encodeURIComponent(state.workspace) + "&limit=30"
    );
    if (!data.traces.length) {
      dom.historyList.appendChild(node("p", "history__empty", t("noHistory")));
      return;
    }
    data.traces.forEach((entry) => {
      const item = node("button", "history__item");
      item.type = "button";
      if (entry.run_id === state.viewedRunId || entry.run_id === state.activeRunId) {
        item.classList.add("is-active");
      }
      item.appendChild(node("span", "history__task", historyTitle(entry)));
      const meta = node("div", "history__meta");
      meta.appendChild(node("span", "", formatTime(entry.started_at)));
      const [label, tone] = COMPLETION_TEXT[entry.completion_status] || [null, null];
      if (label) {
        const pill = node("span", "pill " + tone, t(label));
        pill.style.fontSize = "10px";
        meta.appendChild(pill);
      }
      item.appendChild(meta);
      item.addEventListener("click", () => openStoredRun(entry.run_id));
      dom.historyList.appendChild(item);
    });
  } catch (error) {
    dom.historyList.appendChild(node("p", "history__empty", error.message));
  }
}

/* ---------- thread rendering ---------- */

function historyTitle(entry) {
  if (entry.termination_reason === "rollback") {
    const target = entry.target_run_id ? entry.target_run_id.slice(0, 12) : "";
    return target ? `${t("rollbackTitle")} · ${target}` : t("rollbackTitle");
  }
  return entry.task || entry.run_id.slice(0, 12);
}

function clearThread() {
  dom.messages.replaceChildren();
  state.renderedSteps = new Set();
  dom.welcome.hidden = !state.suggestionsShown;
}

function hideWelcome() {
  state.suggestionsShown = false;
  dom.welcome.hidden = true;
}

function agentBlock() {
  const wrapper = node("div", "agent");
  const avatar = node("div", "agent__avatar");
  avatar.appendChild(icon(ICONS.check, 16));
  const body = node("div", "agent__body");
  wrapper.appendChild(avatar);
  wrapper.appendChild(body);
  return { wrapper, body };
}

function currentBody() {
  const last = dom.messages.lastElementChild;
  if (last && last.classList.contains("agent")) {
    return last.querySelector(".agent__body");
  }
  const block = agentBlock();
  dom.messages.appendChild(block.wrapper);
  return block.body;
}

function appendUserTurn(task) {
  hideWelcome();
  const bubble = node("div", "bubble-user", task);
  dom.messages.appendChild(bubble);
  scrollToEnd();
}

function ensureStep(step) {
  if (!step || state.renderedSteps.has(step)) return;
  state.renderedSteps.add(step);
  const body = currentBody();
  body.appendChild(node("p", "step-divider", t("stepLabel").replace("{n}", String(step))));
}

function collapsibleCard(title, detail, iconPath) {
  const card = node("details", "card");
  const head = node("summary", "card__head");
  const mark = node("span", "card__icon");
  mark.appendChild(icon(iconPath, 13));
  head.appendChild(mark);
  head.appendChild(node("span", "card__title", title));
  if (detail) head.appendChild(node("span", "card__detail", detail));
  const chevron = icon(ICONS.chevron, 14);
  chevron.classList.add("chevron");
  head.appendChild(chevron);
  card.appendChild(head);
  const body = node("div", "card__body");
  card.appendChild(body);
  return { card, body };
}

function keyValues(pairs) {
  const list = node("dl", "kv");
  pairs.forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    list.appendChild(node("dt", "", key));
    list.appendChild(node("dd", "", typeof value === "string" ? value : JSON.stringify(value)));
  });
  return list;
}

function renderEvent(event) {
  const payload = event.payload || {};
  const type = event.event_type;
  if (type === "task") return;
  ensureStep(event.step);
  const body = currentBody();

  if (type === "model") {
    if (payload.text && String(payload.text).trim()) {
      body.appendChild(node("div", "say", String(payload.text).trim()));
    }
    return;
  }

  if (type === "tool_call") {
    const name = payload.tool_name || "tool";
    const detail = describeArguments(payload.arguments);
    const { card, body: cardBody } = collapsibleCard(name, detail, toolIcon(name));
    card.dataset.toolCallId = payload.tool_call_id || "";
    cardBody.appendChild(keyValues(Object.entries(payload.arguments || {})));
    body.appendChild(card);
    return;
  }

  if (type === "tool_result") {
    const target = findToolCard(payload.tool_call_id);
    const ok = payload.ok !== false;
    const pill = node("span", "pill " + (ok ? "pill--ok" : "pill--bad"), ok ? "ok" : "error");
    if (target) {
      const head = target.querySelector(".card__head");
      const chevron = head.querySelector(".chevron");
      head.insertBefore(pill, chevron);
      const summary = describeResult(payload);
      if (summary) {
        const detail = head.querySelector(".card__detail");
        if (detail) detail.textContent = summary;
      }
      target.querySelector(".card__body").appendChild(keyValues(resultPairs(payload)));
      if (!ok) target.open = true;
      return;
    }
    const { card, body: cardBody } = collapsibleCard(
      t("toolResult"),
      describeResult(payload),
      ICONS.file
    );
    cardBody.appendChild(keyValues(resultPairs(payload)));
    body.appendChild(card);
    return;
  }

  if (type === "diff") {
    const stats = payload.stats || {};
    const detail = `+${stats.added_lines || 0} −${stats.removed_lines || 0}`;
    const { card, body: cardBody } = collapsibleCard(payload.path || "diff", detail, ICONS.file);
    card.open = true;
    cardBody.style.padding = "0";
    cardBody.appendChild(renderDiff(String(payload.preview || "")));
    body.appendChild(card);
    return;
  }

  if (type === "verification") {
    const accepted = Boolean(payload.accepted);
    const argv = Array.isArray(payload.argv) ? payload.argv.join(" ") : "";
    const { card, body: cardBody } = collapsibleCard(t("verification"), argv, ICONS.terminal);
    const head = card.querySelector(".card__head");
    head.insertBefore(
      node(
        "span",
        "pill " + (accepted ? "pill--ok" : "pill--warn"),
        accepted ? t("accepted") : t("rejected")
      ),
      head.querySelector(".chevron")
    );
    cardBody.appendChild(
      keyValues([
        ["argv", argv],
        ["cwd", payload.cwd],
        ["exit_code", payload.exit_code],
        ["duration_ms", payload.duration_ms],
      ])
    );
    body.appendChild(card);
    return;
  }

  if (type === "warning") {
    const card = node("div", "card");
    const head = node("div", "card__head");
    const mark = node("span", "card__icon");
    mark.appendChild(icon(ICONS.alert, 13));
    head.appendChild(mark);
    head.appendChild(node("span", "card__title", String(payload.code || "WARNING")));
    if (payload.message) head.appendChild(node("span", "card__detail", String(payload.message)));
    card.appendChild(head);
    body.appendChild(card);
    return;
  }

  if (type === "approval") {
    renderApprovalEvent(payload, body);
    return;
  }

  if (type === "checkpoint") {
    body.appendChild(renderCheckpoint(payload));
    return;
  }

  if (type === "rollback") {
    body.appendChild(renderRollbackEvent(payload));
    return;
  }

  if (type === "termination") {
    dom.messages.appendChild(renderTermination(payload, event.run_id));
  }
}

function renderCheckpoint(payload) {
  const detail = payload.captured
    ? `${t("checkpointEntries")} ${payload.entry_count || 0} · ${t("checkpointBytes")} ${
        payload.captured_bytes || 0
      }`
    : t("checkpointNone");
  const { card, body } = collapsibleCard(t("checkpointTitle"), detail, ICONS.check);
  const uncovered = payload.uncovered;
  if (uncovered && typeof uncovered === "object") {
    body.appendChild(
      keyValues(
        Object.keys(uncovered)
          .sort()
          .map((key) => [`${t("checkpointUncovered")}: ${key}`, uncovered[key]])
      )
    );
  }
  return card;
}

function renderRollbackEvent(payload) {
  const detail = [
    `restored ${payload.restored_count || 0}`,
    `recreated ${payload.recreated_count || 0}`,
    `deleted ${payload.deleted_count || 0}`,
    `skipped ${payload.skipped_count || 0}`,
    `failed ${payload.failed_count || 0}`,
  ].join(" · ");
  const title = payload.complete ? t("rollbackDone") : t("rollbackPartial");
  const { card, body } = collapsibleCard(title, detail, ICONS.check);
  const failures = Array.isArray(payload.failures) ? payload.failures : [];
  if (failures.length) {
    body.appendChild(
      keyValues(failures.map((item) => [`${t("rollbackFailedPaths")}: ${item.path}`, item.code]))
    );
  }
  return card;
}

function findToolCard(callId) {
  if (!callId) return null;
  const cards = dom.messages.querySelectorAll("details.card");
  for (let index = cards.length - 1; index >= 0; index -= 1) {
    if (cards[index].dataset.toolCallId === callId) return cards[index];
  }
  return null;
}

function describeArguments(args) {
  if (!args || typeof args !== "object") return "";
  if (Array.isArray(args.argv)) return args.argv.join(" ");
  const keys = ["path", "query", "summary"];
  for (const key of keys) {
    if (typeof args[key] === "string" && args[key]) return args[key];
  }
  return "";
}

function describeResult(payload) {
  if (payload.ok === false) {
    return String(payload.error_code || "") + (payload.message ? ": " + payload.message : "");
  }
  const data = payload.data || {};
  if (typeof data.exit_code === "number") return "exit_code=" + data.exit_code;
  if (typeof data.match_count === "number") return "matches=" + data.match_count;
  if (typeof data.total_lines === "number") return "lines=" + data.total_lines;
  return "";
}

function resultPairs(payload) {
  const pairs = [["ok", payload.ok === false ? "false" : "true"]];
  if (payload.error_code) pairs.push(["error", String(payload.error_code)]);
  if (payload.message) pairs.push(["message", String(payload.message)]);
  const data = payload.data || {};
  Object.entries(data).forEach(([key, value]) => pairs.push([key, value]));
  if (payload.duration_ms !== undefined) pairs.push(["duration_ms", payload.duration_ms]);
  if (payload.truncated) pairs.push(["truncated", "true"]);
  return pairs;
}

function renderDiff(preview) {
  const block = node("pre", "diff");
  preview.split("\n").forEach((line) => {
    let className = "diff__line";
    if (line.startsWith("+") && !line.startsWith("+++")) className += " diff__line--add";
    else if (line.startsWith("-") && !line.startsWith("---")) className += " diff__line--del";
    else if (line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---")) {
      className += " diff__line--meta";
    }
    block.appendChild(node("code", className, line || " "));
  });
  return block;
}

function renderTermination(payload, runId) {
  const card = node("div", "final");
  const head = node("div", "final__head");
  head.appendChild(node("span", "final__title", t("finished")));
  const status = payload.completion_status;
  const [label, tone] = COMPLETION_TEXT[status] || ["statusNone", "pill--muted"];
  head.appendChild(node("span", "pill " + tone, t(label)));
  head.appendChild(
    node("span", "pill pill--muted", String(payload.termination_reason || "unknown"))
  );
  if (payload.trace_complete === false) {
    head.appendChild(node("span", "pill pill--warn", t("traceIncomplete")));
  }
  card.appendChild(head);

  const body = node("div", "final__body");
  const changed = Array.isArray(payload.changed_files) ? payload.changed_files : [];
  const files = node("div", "file-list");
  if (changed.length) {
    changed.forEach((file) => files.appendChild(node("span", "file-chip", file)));
  } else {
    files.appendChild(node("span", "file-chip", t("noChangedFiles")));
  }
  body.appendChild(node("p", "final__note", t("changedFiles")));
  body.appendChild(files);

  const verification = payload.verification;
  if (verification && Array.isArray(verification.argv)) {
    body.appendChild(
      keyValues([
        ["verification", verification.argv.join(" ")],
        ["cwd", verification.cwd],
        ["exit_code", verification.exit_code],
      ])
    );
  }

  const stats = node("div", "stats");
  [
    ["events", "events", payload.event_count],
    ["modelCalls", "model_calls", payload.model_calls],
    ["toolCalls", "tool_calls", payload.tool_calls],
    ["toolErrors", "tool_errors", payload.tool_errors],
    ["elapsed", "elapsed_seconds", formatSeconds(payload.elapsed_seconds)],
    ["tokensIn", "input_tokens", payload.input_tokens],
    ["tokensOut", "output_tokens", payload.output_tokens],
  ].forEach(([labelKey, _key, value]) => {
    if (value === undefined || value === null) return;
    const stat = node("div", "stat");
    stat.appendChild(node("span", "stat__label", t(labelKey)));
    stat.appendChild(node("span", "stat__value", String(value)));
    stats.appendChild(stat);
  });
  body.appendChild(stats);

  if (runId && state.workspace) {
    const actions = node("div", "final__actions");
    const button = node("button", "text-button rollback__open", t("rollbackButton"));
    button.type = "button";
    const panel = node("div", "rollback");
    panel.hidden = true;
    button.addEventListener("click", () => {
      button.disabled = true;
      openRollback(runId, panel, button);
    });
    actions.appendChild(button);
    body.appendChild(actions);
    body.appendChild(panel);
  }

  card.appendChild(body);
  return card;
}

/* ---------- rollback ---------- */

const ROLLBACK_ACTION_TEXT = {
  restore: "actionRestore",
  recreate: "actionRecreate",
  delete: "actionDelete",
  create_directory: "actionCreateDirectory",
  remove_directory: "actionRemoveDirectory",
};

async function openRollback(runId, panel, button) {
  panel.hidden = false;
  panel.replaceChildren(node("p", "rollback__note", t("rollbackLoading")));
  try {
    const plan = await api(
      `/api/checkpoints/${encodeURIComponent(runId)}/plan?workspace=` +
        encodeURIComponent(state.workspace)
    );
    renderRollbackPlan(plan, runId, panel, button);
  } catch (error) {
    panel.replaceChildren(
      node("p", "rollback__note", rollbackErrorText(error))
    );
    button.disabled = false;
  }
}

function renderRollbackPlan(plan, runId, panel, button, warning) {
  panel.replaceChildren();
  panel.appendChild(node("p", "rollback__title", t("rollbackTitle")));
  if (warning) panel.appendChild(node("p", "rollback__warn", warning));

  const items = Array.isArray(plan.items) ? plan.items : [];
  if (!items.length) {
    panel.appendChild(node("p", "rollback__note", t("rollbackEmpty")));
  } else {
    const list = node("ul", "rollback__list");
    items.forEach((item) => {
      const entry = node("li", "rollback__item");
      entry.appendChild(node("span", "pill pill--muted", t(ROLLBACK_ACTION_TEXT[item.action] || "actionRestore")));
      entry.appendChild(node("span", "rollback__path", item.path));
      if (item.by_tool) entry.appendChild(node("span", "rollback__source", t("rollbackByTool")));
      list.appendChild(entry);
    });
    panel.appendChild(list);
  }

  const skipped = Array.isArray(plan.skipped) ? plan.skipped : [];
  if (skipped.length) {
    panel.appendChild(node("p", "rollback__note", t("rollbackNotCovered")));
    const list = node("ul", "rollback__list");
    skipped.forEach((skip) => {
      const entry = node("li", "rollback__item rollback__item--skipped");
      entry.appendChild(node("span", "rollback__path", skip.path));
      entry.appendChild(node("span", "rollback__source", skip.reason));
      list.appendChild(entry);
    });
    panel.appendChild(list);
  }

  if (!items.length) {
    const actions = node("div", "rollback__actions");
    const close = node("button", "text-button", t("rollbackCancel"));
    close.type = "button";
    close.addEventListener("click", () => {
      panel.hidden = true;
      button.disabled = false;
    });
    actions.appendChild(close);
    panel.appendChild(actions);
    return;
  }

  const actions = node("div", "rollback__actions");
  const confirm = node("button", "primary-button", t("rollbackConfirm"));
  confirm.type = "button";
  const cancel = node("button", "text-button", t("rollbackCancel"));
  cancel.type = "button";
  cancel.addEventListener("click", () => {
    panel.hidden = true;
    button.disabled = false;
  });
  confirm.addEventListener("click", () => {
    confirm.disabled = true;
    cancel.disabled = true;
    applyRollback(runId, plan.plan_digest, panel, button);
  });
  actions.appendChild(confirm);
  actions.appendChild(cancel);
  panel.appendChild(actions);
}

async function applyRollback(runId, digest, panel, button) {
  try {
    const result = await api(`/api/checkpoints/${encodeURIComponent(runId)}/rollback`, {
      method: "POST",
      json: { workspace: state.workspace, plan_digest: digest },
    });
    renderRollbackResult(result, panel, button);
  } catch (error) {
    if (error.code === "PLAN_CHANGED" && error.body) {
      // The approval was for a plan that no longer applies, so ask again on the new one.
      renderRollbackPlan(error.body, runId, panel, button, t("rollbackChanged"));
      return;
    }
    panel.replaceChildren(node("p", "rollback__warn", rollbackErrorText(error)));
    button.disabled = false;
  }
}

function renderRollbackResult(result, panel, button) {
  panel.replaceChildren();
  const complete = result.complete !== false;
  panel.appendChild(
    node("p", complete ? "rollback__title" : "rollback__warn", complete ? t("rollbackDone") : t("rollbackPartial"))
  );
  const counts = [
    ["actionRestore", (result.restored || []).length],
    ["actionRecreate", (result.recreated || []).length],
    ["actionDelete", (result.deleted || []).length],
  ];
  panel.appendChild(keyValues(counts.map(([key, value]) => [t(key), value])));
  const failures = Array.isArray(result.failures) ? result.failures : [];
  if (failures.length) {
    panel.appendChild(
      keyValues(failures.map((item) => [`${t("rollbackFailedPaths")}: ${item.path}`, item.code]))
    );
  }
  button.disabled = false;
  refreshHistory();
}

function rollbackErrorText(error) {
  if (error.code === "WORKSPACE_BUSY") return t("rollbackBusy");
  if (error.code === "CHECKPOINT_NOT_FOUND") return t("rollbackMissing");
  return error.message || t("loadFailed");
}

function formatSeconds(value) {
  if (typeof value !== "number") return value;
  return value.toFixed(1) + t("seconds");
}

function setWorking(active) {
  const existing = el("working-indicator");
  if (existing) existing.remove();
  if (!active) return;
  const indicator = node("div", "working");
  indicator.id = "working-indicator";
  const dots = node("span", "dots");
  dots.appendChild(node("span"));
  dots.appendChild(node("span"));
  dots.appendChild(node("span"));
  indicator.appendChild(dots);
  indicator.appendChild(node("span", "", t("thinking")));
  dom.messages.appendChild(indicator);
  scrollToEnd();
}

function renderApprovalEvent(payload, body) {
  if (payload.phase === "policy") {
    const where = payload.policy_source || t("approvalPolicyNone");
    const detail = `${where} · ${payload.policy_entries || 0} · ${payload.approval_mode || ""}`;
    const { card, body: cardBody } = collapsibleCard(t("approvalPolicy"), detail, ICONS.alert);
    cardBody.appendChild(
      keyValues([
        ["policy_source", payload.policy_source],
        ["policy_digest", payload.policy_digest],
        ["policy_entries", payload.policy_entries],
        ["approval_mode", payload.approval_mode],
      ])
    );
    body.appendChild(card);
    return;
  }
  if (payload.phase === "request") {
    const argv = Array.isArray(payload.display_argv) ? payload.display_argv.join(" ") : "";
    const { card, body: cardBody } = collapsibleCard(t("approvalTitle"), argv, ICONS.terminal);
    cardBody.appendChild(
      keyValues([
        ["argv", argv],
        ["cwd", payload.cwd],
        ["command_kind", payload.command_kind],
        ["decision_source", payload.decision_source],
      ])
    );
    body.appendChild(card);
    return;
  }
  if (payload.phase === "decision") {
    const outcomes = t("approvalOutcome") || {};
    const label = outcomes[payload.outcome] || String(payload.outcome || "");
    const executed = payload.executed === true;
    const card = node("div", "card");
    const head = node("div", "card__head");
    const mark = node("span", "card__icon");
    mark.appendChild(icon(executed ? ICONS.terminal : ICONS.alert, 13));
    head.appendChild(mark);
    head.appendChild(node("span", "card__title", label));
    head.appendChild(
      node("span", "card__detail", `${payload.decided_by || ""} · ${payload.waited_seconds || 0}s`)
    );
    card.appendChild(head);
    body.appendChild(card);
  }
}

function syncApproval(run) {
  const pending = run && run.pending_approval ? run.pending_approval : null;
  if (!pending) {
    if (state.approvalDigest) {
      state.approvalDigest = null;
      if (dom.approvalPanel) dom.approvalPanel.hidden = true;
    }
    return;
  }
  if (state.approvalDigest === pending.digest) return;
  state.approvalDigest = pending.digest;
  renderApprovalPanel(pending);
}

function approvalPanel() {
  if (dom.approvalPanel) return dom.approvalPanel;
  const panel = node("section", "rollback");
  panel.id = "approval-panel";
  panel.hidden = true;
  dom.thread.appendChild(panel);
  dom.approvalPanel = panel;
  return panel;
}

function renderApprovalPanel(pending) {
  const panel = approvalPanel();
  panel.hidden = false;
  panel.replaceChildren();
  panel.appendChild(node("p", "rollback__title", t("approvalTitle")));
  const argv = Array.isArray(pending.display_argv) ? pending.display_argv.join(" ") : "";
  panel.appendChild(node("pre", "approval__argv", argv));
  panel.appendChild(
    keyValues([
      ["cwd", pending.cwd],
      ["timeout_seconds", pending.timeout_seconds],
      ["command_kind", pending.command_kind],
      ["decision_source", pending.decision_source],
    ])
  );
  const actions = node("div", "rollback__actions");
  const allow = node("button", "primary-button", t("approvalApprove"));
  allow.type = "button";
  const refuse = node("button", "text-button", t("approvalDeny"));
  refuse.type = "button";
  const answer = (decision) => {
    allow.disabled = true;
    refuse.disabled = true;
    sendApproval(pending.digest, decision, panel);
  };
  allow.addEventListener("click", () => answer("approve"));
  refuse.addEventListener("click", () => answer("deny"));
  actions.appendChild(allow);
  actions.appendChild(refuse);
  panel.appendChild(actions);
  scrollToEnd();
}

async function sendApproval(digest, decision, panel) {
  if (!state.activeRunId) return;
  try {
    await api(`/api/runs/${encodeURIComponent(state.activeRunId)}/approval`, {
      method: "POST",
      json: { request_digest: digest, decision },
    });
    panel.replaceChildren(node("p", "rollback__title", t("approvalWaiting")));
    panel.hidden = true;
    state.approvalDigest = null;
  } catch (error) {
    if (error.code === "APPROVAL_CHANGED" && error.body && error.body.run) {
      // The decision was for a command that is no longer the one waiting, so the
      // caller reviews the current one instead of answering blind.
      banner(t("approvalStale"), true);
      state.approvalDigest = null;
      syncApproval(error.body.run);
      return;
    }
    banner(error.code === "NO_PENDING_APPROVAL" ? t("approvalGone") : error.message, true);
    panel.hidden = true;
    state.approvalDigest = null;
  }
}

function scrollToEnd() {
  window.requestAnimationFrame(() => {
    dom.thread.scrollTop = dom.thread.scrollHeight;
  });
}

function setRunning(running) {
  dom.sendButton.hidden = running;
  dom.stopButton.hidden = !running;
  dom.taskInput.disabled = running;
  setWorking(running);
}

/* ---------- run lifecycle ---------- */

async function startRun() {
  const task = dom.taskInput.value.trim();
  if (!task) return;
  if (!state.workspace) {
    banner(t("workspaceMissing"), true);
    openModal("workspace-modal");
    return;
  }
  const request = Object.assign({ workspace: state.workspace, task }, state.limits);
  if (state.sessionId) {
    request.session_id = state.sessionId;
  }
  if (state.stream) {
    request.stream = true;
  }
  let payload;
  try {
    payload = await api("/api/runs", { method: "POST", json: request });
  } catch (error) {
    banner(error.code === "WORKSPACE_BUSY" ? t("busy") : error.message || t("startFailed"), true);
    return;
  }
  dom.taskInput.value = "";
  autoGrowSoon();
  if (state.viewedRunId) {
    clearThread();
  }
  hideWelcome();
  state.viewedRunId = null;
  state.activeRunId = payload.run.run_id;
  dom.threadTitle.textContent = task;
  appendUserTurn(task);
  setRunning(true);
  pollRun(payload.run.run_id, 0);
}

/* ---------- streamed text ---------- */

function syncPartialText(run) {
  const existing = el("partial-text");
  const text = run && typeof run.partial_text === "string" ? run.partial_text : "";
  if (!text) {
    // The committed event now carries this text, so the preview goes rather than
    // showing the same sentence twice.
    if (existing) existing.remove();
    return;
  }
  const node = existing || createPartialText();
  node.lastChild.textContent = text;
  scrollToEnd();
}

function createPartialText() {
  // Rendered in the same place and with the same shape a committed model message
  // takes, so the text does not jump when the event replaces the preview.
  const body = currentBody();
  const element = document.createElement("div");
  element.className = "say say--streaming";
  element.id = "partial-text";
  const label = document.createElement("span");
  label.className = "say__streaming-label";
  label.textContent = t("streaming");
  element.appendChild(label);
  element.appendChild(document.createTextNode(""));
  body.appendChild(element);
  return element;
}

/* ---------- sessions ---------- */

function renderSessions() {
  const select = dom.sessionSelect;
  if (!select) return;
  select.textContent = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = t("sessionNone");
  select.appendChild(none);
  for (const item of state.sessions) {
    const option = document.createElement("option");
    option.value = item.session_id;
    const state_label = item.ended_at ? t("sessionEnded") : t("sessionOpen");
    option.textContent = `${item.session_id.slice(0, 8)} · ${item.run_count} ${t(
      "sessionRuns",
    )} · ${state_label}`;
    option.disabled = Boolean(item.ended_at);
    select.appendChild(option);
  }
  if (!state.sessions.some((item) => item.session_id === state.sessionId)) {
    state.sessionId = "";
  }
  select.value = state.sessionId;
  dom.sessionNote.textContent = state.sessionId ? t("sessionNote") : "";
}

async function refreshSessions() {
  if (!state.workspace) {
    state.sessions = [];
    state.sessionId = "";
    renderSessions();
    return;
  }
  try {
    const data = await api(
      `/api/sessions?workspace=${encodeURIComponent(state.workspace)}`,
    );
    state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
  } catch (error) {
    state.sessions = [];
  }
  renderSessions();
}

async function createSession() {
  if (!state.workspace) {
    banner(t("workspaceMissing"), true);
    openModal("workspace-modal");
    return;
  }
  try {
    const data = await api("/api/sessions", {
      method: "POST",
      json: { workspace: state.workspace },
    });
    await refreshSessions();
    state.sessionId = data.session.session_id;
    renderSessions();
  } catch (error) {
    banner(error.message || t("sessionFailed"), true);
  }
}

async function pollRun(runId, cursor) {
  if (state.polling) return;
  state.polling = true;
  let nextCursor = cursor;
  try {
    while (state.activeRunId === runId) {
      const data = await api(
        `/api/runs/${runId}/events?cursor=${nextCursor}&wait=${POLL_SECONDS}`
      );
      nextCursor = data.cursor;
      syncApproval(data.run);
      syncPartialText(data.run);
      if (data.events.length) {
        setWorking(false);
        data.events.forEach(renderEvent);
        if (data.run.status === "running") setWorking(true);
        scrollToEnd();
      }
      if (data.done) {
        state.activeRunId = null;
        syncApproval(null);
        setRunning(false);
        await refreshHistory();
        // The finished run was just appended to its session, so the counts moved.
        await refreshSessions();
        break;
      }
    }
  } catch (error) {
    banner(error.message || t("loadFailed"), true);
    state.activeRunId = null;
    setRunning(false);
  } finally {
    state.polling = false;
  }
}

async function stopRun() {
  if (!state.activeRunId) return;
  try {
    await api(`/api/runs/${state.activeRunId}/cancel`, { method: "POST", json: {} });
    toast(t("cancelSent"));
    const indicator = el("working-indicator");
    if (indicator) indicator.lastChild.textContent = t("stopping");
  } catch (error) {
    banner(error.message, true);
  }
}

async function openStoredRun(runId) {
  if (state.activeRunId) return;
  try {
    const data = await api(
      `/api/traces/${runId}?workspace=${encodeURIComponent(state.workspace)}`
    );
    state.viewedRunId = runId;
    clearThread();
    hideWelcome();
    dom.threadTitle.textContent = data.task || t("replayTitle");
    appendUserTurn(data.task || runId);
    data.events.forEach(renderEvent);
    setRunning(false);
    await refreshHistory();
    dom.thread.scrollTop = 0;
  } catch (error) {
    banner(error.message, true);
  }
}

function newTask() {
  if (state.activeRunId) return;
  state.viewedRunId = null;
  state.suggestionsShown = true;
  clearThread();
  dom.threadTitle.textContent = t("newTask");
  dom.taskInput.focus();
  refreshHistory();
}

/* ---------- modals ---------- */

function openModal(id) {
  el(id).hidden = false;
}

function closeModal(id) {
  el(id).hidden = true;
}

async function browseTo(path) {
  try {
    const data = await api("/api/workspace/browse?path=" + encodeURIComponent(path || ""));
    dom.browserHere.textContent = data.path;
    dom.workspaceInput.value = data.path;
    dom.browserUp.disabled = !data.parent;
    dom.browserUp.dataset.path = data.parent || "";
    dom.browserRoots.replaceChildren();
    data.roots.forEach((root) => {
      const button = node("button", "text-button", shortPath(root) || root);
      button.type = "button";
      button.title = root;
      button.addEventListener("click", () => browseTo(root));
      dom.browserRoots.appendChild(button);
    });
    dom.browserList.replaceChildren();
    if (!data.directories.length) {
      dom.browserList.appendChild(node("p", "browser__empty", t("noSubdirectories")));
    }
    data.directories.forEach((entry) => {
      const button = node("button", "browser__entry");
      button.type = "button";
      button.appendChild(icon(ICONS.chevron, 13));
      button.appendChild(node("span", "", entry.name));
      button.addEventListener("click", () => browseTo(entry.path));
      dom.browserList.appendChild(button);
    });
  } catch (error) {
    dom.browserList.replaceChildren(node("p", "browser__empty", error.message));
  }
}

function renderRecent() {
  dom.recentWrapper.hidden = state.recent.length < 2;
  dom.recentList.replaceChildren();
  state.recent.forEach((path) => {
    const button = node("button", "recent__item", path);
    button.type = "button";
    button.title = path;
    button.addEventListener("click", async () => {
      await setWorkspace(path, true);
      closeModal("workspace-modal");
    });
    dom.recentList.appendChild(button);
  });
}

function renderSuggestions() {
  dom.suggestions.replaceChildren();
  (TEXT[state.lang].suggestions || []).forEach((text) => {
    const chip = node("button", "suggestion", text);
    chip.type = "button";
    chip.addEventListener("click", () => {
      dom.taskInput.value = text;
      autoGrow();
      dom.taskInput.focus();
    });
    dom.suggestions.appendChild(chip);
  });
}

function renderLimitFields() {
  dom.limitFields.replaceChildren();
  LIMIT_FIELDS.forEach((field) => {
    const bounds = state.limitBounds[field.key];
    if (!bounds) return;
    const wrapper = node("div", "field");
    const label = node("label", "", field[state.lang] || field.en);
    label.htmlFor = "limit-" + field.key;
    const input = document.createElement("input");
    input.id = "limit-" + field.key;
    input.type = "number";
    input.min = String(bounds.minimum);
    input.max = String(bounds.maximum);
    input.value = String(
      state.limits[field.key] !== undefined ? state.limits[field.key] : bounds.default
    );
    wrapper.appendChild(label);
    wrapper.appendChild(input);
    wrapper.appendChild(node("small", "", `${bounds.minimum} – ${bounds.maximum}`));
    dom.limitFields.appendChild(wrapper);
  });
}

function saveLimits() {
  LIMIT_FIELDS.forEach((field) => {
    const input = el("limit-" + field.key);
    if (!input) return;
    const value = Number(input.value);
    if (Number.isFinite(value)) state.limits[field.key] = value;
  });
  saveStore();
  closeModal("settings-modal");
  toast(t("save"));
}

function resetLimits() {
  state.limits = {};
  LIMIT_FIELDS.forEach((field) => {
    const bounds = state.limitBounds[field.key];
    if (bounds) state.limits[field.key] = bounds.default;
  });
  saveStore();
  renderLimitFields();
}

async function runDoctor(offline) {
  dom.doctorResults.replaceChildren(node("p", "modal__note", t("checking")));
  try {
    const data = await api("/api/doctor", { method: "POST", json: { offline } });
    dom.doctorResults.replaceChildren();
    data.checks.forEach((check) => {
      const row = node("div", "check");
      row.appendChild(
        node("span", "pill " + (check.ok ? "pill--ok" : "pill--bad"), check.ok ? "PASS" : "FAIL")
      );
      const text = node("div", "");
      text.appendChild(node("span", "check__name", check.name));
      text.appendChild(node("div", "check__detail", check.detail));
      row.appendChild(text);
      dom.doctorResults.appendChild(row);
    });
  } catch (error) {
    dom.doctorResults.replaceChildren(node("p", "modal__note is-error", error.message));
  }
}

/* ---------- composer ---------- */

function autoGrow() {
  // Reset before measuring: scrollHeight of a textarea never shrinks below its own
  // current height, so growing once would otherwise pin the composer open.
  dom.taskInput.style.height = "auto";
  const next = Math.min(Math.max(dom.taskInput.scrollHeight, 34), 200);
  dom.taskInput.style.height = next + "px";
}

function autoGrowSoon() {
  // Measuring before the first layout pass reports the flex container's height
  // rather than the text's, so defer the initial and post-send measurement.
  window.requestAnimationFrame(autoGrow);
}

/* ---------- bootstrap ---------- */

function cacheDom() {
  [
    ["thread", "thread"],
    ["messages", "messages"],
    ["welcome", "welcome"],
    ["suggestions", "suggestions"],
    ["taskInput", "task-input"],
    ["sendButton", "send-button"],
    ["stopButton", "stop-button"],
    ["banner", "composer-banner"],
    ["threadTitle", "thread-title"],
    ["threadSubtitle", "thread-subtitle"],
    ["modelPill", "model-pill"],
    ["statusDot", "status-dot"],
    ["statusText", "status-text"],
    ["historyList", "history-list"],
    ["sessionSelect", "session-select"],
    ["sessionNew", "session-new"],
    ["sessionNote", "session-note"],
    ["streamToggle", "stream-toggle"],
    ["workspaceName", "workspace-name"],
    ["workspacePath", "workspace-path"],
    ["workspaceInput", "workspace-input"],
    ["workspaceFeedback", "workspace-feedback"],
    ["browserHere", "browser-here"],
    ["browserRoots", "browser-roots"],
    ["browserList", "browser-list"],
    ["browserUp", "browser-up"],
    ["recentList", "recent-list"],
    ["recentWrapper", "recent-wrapper"],
    ["limitFields", "limit-fields"],
    ["doctorResults", "doctor-results"],
    ["langButton", "lang-button"],
    ["toast", "toast"],
    ["app", "app"],
  ].forEach(([key, id]) => {
    dom[key] = el(id);
  });
}

function bindEvents() {
  dom.sessionSelect.addEventListener("change", () => {
    state.sessionId = dom.sessionSelect.value;
    dom.sessionNote.textContent = state.sessionId ? t("sessionNote") : "";
  });
  dom.sessionNew.addEventListener("click", createSession);
  dom.streamToggle.addEventListener("change", () => {
    state.stream = dom.streamToggle.checked;
    saveStore();
  });
  dom.sendButton.addEventListener("click", startRun);
  dom.stopButton.addEventListener("click", stopRun);
  dom.taskInput.addEventListener("input", autoGrow);
  dom.taskInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      startRun();
    }
  });

  el("new-run").addEventListener("click", newTask);
  el("workspace-button").addEventListener("click", () => {
    openModal("workspace-modal");
    renderRecent();
    browseTo(state.workspace || "");
  });
  el("workspace-apply").addEventListener("click", async () => {
    await setWorkspace(dom.workspaceInput.value.trim(), true);
    closeModal("workspace-modal");
  });
  dom.workspaceInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") el("workspace-apply").click();
  });
  dom.browserUp.addEventListener("click", () => browseTo(dom.browserUp.dataset.path || ""));

  el("settings-button").addEventListener("click", () => {
    renderLimitFields();
    openModal("settings-modal");
  });
  el("settings-save").addEventListener("click", saveLimits);
  el("settings-reset").addEventListener("click", resetLimits);

  el("doctor-button").addEventListener("click", () => {
    openModal("doctor-modal");
    runDoctor(true);
  });
  el("doctor-offline").addEventListener("click", () => runDoctor(true));
  el("doctor-online").addEventListener("click", () => runDoctor(false));

  document.querySelectorAll("[data-close]").forEach((element) => {
    element.addEventListener("click", () => closeModal(element.dataset.close));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".modal").forEach((modal) => {
        modal.hidden = true;
      });
      dom.app.classList.remove("is-sidebar-open");
    }
  });

  el("lang-button").addEventListener("click", () => {
    state.lang = state.lang === "zh" ? "en" : "zh";
    saveStore();
    applyLanguage();
    // A stored run can be re-rendered in the new language; a live run keeps the
    // labels it already drew rather than replaying events the server has released.
    if (state.viewedRunId && !state.activeRunId) {
      openStoredRun(state.viewedRunId);
    } else {
      refreshHistory();
    }
  });
  el("theme-button").addEventListener("click", () => {
    const order = ["system", "light", "dark"];
    state.theme = order[(order.indexOf(state.theme) + 1) % order.length];
    saveStore();
    applyTheme();
    toast(state.theme);
  });

  el("sidebar-open").addEventListener("click", () => dom.app.classList.add("is-sidebar-open"));
  el("sidebar-close").addEventListener("click", () => dom.app.classList.remove("is-sidebar-open"));
  el("sidebar-scrim").addEventListener("click", () =>
    dom.app.classList.remove("is-sidebar-open")
  );
  window.addEventListener("resize", autoGrowSoon);
}

async function main() {
  cacheDom();
  const stored = loadStore();
  const browserLanguage = String(navigator.language || "en").toLowerCase();
  state.lang = stored.lang || (browserLanguage.startsWith("zh") ? "zh" : "en");
  state.theme = stored.theme || "system";
  state.recent = Array.isArray(stored.recent) ? stored.recent : [];
  state.limits = stored.limits && typeof stored.limits === "object" ? stored.limits : {};
  state.stream = stored.stream === true;
  applyTheme();
  applyLanguage();
  bindEvents();
  dom.streamToggle.checked = state.stream;
  autoGrowSoon();
  if (stored.workspace) {
    await setWorkspace(stored.workspace, false);
  }
  await refreshStatus();
  dom.taskInput.focus();
}

main();
