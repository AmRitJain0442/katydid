"use strict";

const $ = (selector) => document.querySelector(selector);
const ui = {
  clock: $("#clock"), createForm: $("#create-form"), repository: $("#repository"),
  createButton: $("#create-form button"), refresh: $("#refresh"), taskCount: $("#task-count"),
  taskList: $("#task-list"), detailEmpty: $("#detail-empty"), detailContent: $("#detail-content"),
  detailRepository: $("#detail-repository"), detailId: $("#detail-id"), detailState: $("#detail-state"),
  facts: $("#facts"), timeline: $("#timeline"), eventCount: $("#event-count"), record: $("#record"),
  steerForm: $("#steer-form"), instruction: $("#instruction"),
  pauseButton: $('[data-action="pause"]'), resumeButton: $('[data-action="resume"]'),
  cancelButton: $('[data-action="cancel"]'), steerButton: $("#steer-form button"), notice: $("#notice"),
};
let tasks = [];
let selectedId = null;
let selectedTask = null;
let selectedEvents = [];
let filter = "all";
let noticeTimer;
let renderedTasks = "";
let renderedEvents = "";
let renderedChecks = "";
let renderedRuntime = "";
let selectionVersion = 0;
let loadingTasks = false;
let controlPending = false;
let createPending = false;
const terminalStates = new Set(["completed", "failed", "cancelled", "unresolved"]);

function safeText(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
function knownState(value) {
  const state = safeText(value, "unknown").toLowerCase();
  if (["queued", "discovered"].includes(state)) return "queued";
  if (["completed", "passed", "paused", "failed", "error", "cancelled"].includes(state)) return state;
  if (state === "unresolved") return "failed";
  if (["running", "preparing", "planning", "testing", "diagnosing", "executing", "verifying", "repairing", "reviewing", "eligible", "merging", "publishing", "deploying", "releasing", "monitoring"].includes(state)) return "running";
  return "unknown";
}
function appendText(parent, tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = safeText(text);
  if (className) node.className = className;
  parent.append(node);
  return node;
}
function container(parent, tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  parent.append(node);
  return node;
}
function stateLabel(parent, value, className = "state-dot") {
  const node = appendText(parent, "span", value, className);
  node.dataset.state = knownState(value);
  return node;
}
function timestamp(value) {
  if (value === null || value === undefined || value === "") return NaN;
  return typeof value === "number" ? value * 1000 : Date.parse(String(value));
}
function formatTimestamp(value, timeOnly = false) {
  const milliseconds = timestamp(value);
  if (!Number.isFinite(milliseconds)) return safeText(value);
  const date = new Date(milliseconds);
  return timeOnly ? date.toLocaleTimeString([], { hour12: false }) : date.toLocaleString();
}
function age(value) {
  const ms = timestamp(value);
  if (!Number.isFinite(ms)) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - ms) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}
function humanize(value) {
  const text = safeText(value, "Unknown").replaceAll("_", " ").replaceAll("-", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
function showNotice(message, isError = false) {
  window.clearTimeout(noticeTimer);
  ui.notice.textContent = message;
  ui.notice.classList.toggle("error", isError);
  ui.notice.classList.add("visible");
  noticeTimer = window.setTimeout(() => ui.notice.classList.remove("visible"), 5000);
}
async function api(path, options = {}) {
  const request = { ...options, signal: AbortSignal.timeout(15000), headers: { ...(options.headers || {}) } };
  if (request.body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    request.headers["X-Katydid-Request"] = "dashboard";
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(path, request);
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
function runtimeLine(parent, label, value) {
  const line = container(parent, "div", "runtime-line");
  appendText(line, "span", label);
  appendText(line, "b", value);
}
function intervalLabel(seconds) {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}
async function loadRuntime() {
  const panel = $("#runtime-info");
  try {
    const { runtime } = await api("/api/runtime");
    const signature = JSON.stringify(runtime);
    if (signature === renderedRuntime) return;
    renderedRuntime = signature;
    panel.replaceChildren();
    $("#worker-dot").dataset.state = runtime.managed ? (runtime.worker_alive ? "completed" : "failed") : "unknown";
    $(".context-badge").textContent = "LIVE";
    if (!runtime.managed) {
      appendText(panel, "p", "Worker configuration is managed externally.");
      return;
    }
    const status = container(panel, "div", "runtime-status");
    appendText(status, "strong", runtime.worker_alive ? "Worker online" : "Worker stopped");
    const provider = container(panel, "div", "provider-card");
    appendText(provider, "strong", runtime.provider);
    appendText(provider, "span", runtime.model);
    runtimeLine(panel, "Repository watch", runtime.watch ? `Every ${intervalLabel(runtime.interval_seconds)}` : "Disabled");
    runtimeLine(panel, "Scheduled checks", runtime.schedule_seconds ? `Every ${intervalLabel(runtime.schedule_seconds)}` : "Disabled");
    for (const repository of runtime.repositories || []) {
      const section = container(panel, "section", "runtime-repo");
      const heading = appendText(section, "h4", repository.id);
      const checks = Object.entries(repository.required_checks || {});
      appendText(heading, "span", `${checks.length} required`);
      const list = container(section, "ul", "configured-checks");
      for (const [id, type] of checks) {
        const item = appendText(list, "li", humanize(id));
        appendText(item, "small", type);
      }
      for (const [stage, stageChecks] of Object.entries(repository.required_checks_by_stage || {})) {
        runtimeLine(section, `${humanize(stage)} checks`, Object.keys(stageChecks).join(", "));
      }
      runtimeLine(section, "Delivery", repository.delivery);
      runtimeLine(section, "Auto merge", repository.auto_merge ? "Enabled" : "Disabled");
      runtimeLine(section, "Auto deploy", repository.auto_deploy ? "Enabled" : "Disabled");
      runtimeLine(section, "Release hooks", repository.release_configured ? "Configured" : "Not configured");
      runtimeLine(section, "Required isolation", repository.isolation_required ? "Container" : "Not required");
    }
  } catch {
    renderedRuntime = "";
    panel.textContent = "Runtime status unavailable. Retrying automatically…";
    $("#worker-dot").dataset.state = "unknown";
    $(".context-badge").textContent = "OFFLINE";
  }
}
function renderTasks() {
  const search = $("#task-search").value.trim().toLowerCase();
  const rows = tasks.map(task => ({ task, age: age(task.created_at ?? task.created) }));
  const signature = JSON.stringify([selectedId, filter, search, rows.map(({ task, age }) => [task.id, task.repository, task.state, task.payload?.stage, age])]);
  if (signature === renderedTasks) return;
  renderedTasks = signature;
  const visible = rows.filter(({ task }) => {
    const state = knownState(task.state);
    const matchesFilter = filter === "all" || (filter === "active" && ["queued", "running", "paused"].includes(state)) || (filter === "attention" && ["failed", "error", "paused"].includes(state));
    return matchesFilter && [task.id, task.repository, task.state, task.payload?.stage].some(value => safeText(value, "").toLowerCase().includes(search));
  });
  const focusedId = document.activeElement?.closest(".task-card")?.dataset.id;
  const scroll = ui.taskList.scrollTop;
  ui.taskList.replaceChildren();
  ui.taskCount.textContent = `${visible.length} of ${tasks.length} investigation${tasks.length === 1 ? "" : "s"}`;
  if (!visible.length) appendText(ui.taskList, "div", tasks.length ? "No matching investigations." : "No investigations yet. Start your first run.", "empty");
  for (const { task, age: taskAge } of visible) {
    const id = safeText(task.id, "");
    const button = container(ui.taskList, "button", `task-card${id === selectedId ? " selected" : ""}`);
    button.type = "button";
    button.dataset.id = id;
    button.setAttribute("aria-pressed", String(id === selectedId));
    button.addEventListener("click", () => selectTask(id));
    const top = container(button, "span", "task-card-top");
    appendText(top, "strong", task.repository);
    appendText(top, "span", taskAge, "task-age");
    const meta = container(button, "span", "task-meta");
    const label = appendText(meta, "span", `${id.slice(0, 7)} · ${safeText(task.payload?.stage, "task")}`, "task-id");
    label.title = id;
    stateLabel(meta, task.state);
  }
  ui.taskList.scrollTop = scroll;
  if (focusedId) Array.from(ui.taskList.children).find(node => node.dataset.id === focusedId)?.focus({ preventScroll: true });
  const recent = $("#recent-tasks");
  const focusedRecent = document.activeElement?.closest(".recent-task")?.dataset.id;
  recent.replaceChildren();
  if (!tasks.length) appendText(recent, "p", "Your investigations will appear here.", "empty");
  for (const task of tasks.slice(0, 3)) {
    const button = container(recent, "button", "recent-task");
    button.type = "button";
    button.dataset.id = safeText(task.id, "");
    button.addEventListener("click", () => selectTask(button.dataset.id));
    appendText(button, "span", task.repository);
    appendText(button, "small", `${safeText(task.payload?.stage, "task")} / ${safeText(task.id).slice(0, 7)}`);
    stateLabel(button, task.state);
    appendText(button, "span", "↗").setAttribute("aria-hidden", "true");
  }
  if (focusedRecent) Array.from(recent.children).find(node => node.dataset.id === focusedRecent)?.focus({ preventScroll: true });
}
function renderControls(state) {
  const terminal = terminalStates.has(state);
  const unavailable = !selectedTask || controlPending;
  ui.pauseButton.disabled = unavailable || terminal || state === "paused";
  ui.resumeButton.disabled = unavailable || terminal || state !== "paused";
  ui.cancelButton.disabled = unavailable || terminal;
  ui.instruction.disabled = unavailable || terminal;
  ui.steerButton.disabled = unavailable || terminal;
  $("#control-hint").textContent = terminal ? "This investigation has ended. Start a new run to continue." : "Applied at the next control boundary";
}
function renderFacts(task) {
  ui.facts.replaceChildren();
  const result = task.result || {};
  const fields = [
    ["Stage", result.stage ?? task.payload?.stage], ["Mode", result.mode ?? task.payload?.mode],
    ["Commit", safeText(result.merged_sha || result.source_sha || task.payload?.base_sha).slice(0, 12)],
    ["AI calls", result.ai_calls ?? task.ai_calls], ["Epoch", task.epoch],
    ["Created", formatTimestamp(task.created_at ?? task.created)],
    ["Updated", formatTimestamp(task.updated_at ?? task.updated)],
  ];
  for (const [label, value] of fields) {
    const wrapper = container(ui.facts, "div", "fact");
    appendText(wrapper, "dt", label);
    appendText(wrapper, "dd", value);
  }
}
function resultGroups(task) {
  const result = task.result || {};
  return [["baseline", "Baseline checks"], ["verification", "Repair verification"], ["release_verification", "Release verification"], ["release_gate", "Release checks"], ["deploy", "Deployment"], ["health", "Health checks"], ["rollback", "Rollback"]]
    .filter(([key]) => Array.isArray(result[key]?.results))
    .map(([key, label]) => ({ key, label, ...result[key] }));
}
function renderOutcome(task) {
  const result = task.result || {};
  const titles = { healthy: "All required checks passed", repaired: "Repair verified and delivered", released: "Release deployed and verified" };
  const stateTitles = { queued: "Investigation queued", paused: "Investigation paused", completed: "Investigation complete", failed: "Investigation failed", unresolved: "Investigation needs attention", cancelled: "Investigation cancelled" };
  $("#outcome-title").textContent = titles[result.outcome] || stateTitles[task.state] || `${humanize(task.state)} repository`;
  const groups = resultGroups(task);
  const latest = groups.at(-1);
  const count = latest?.results.length;
  let description = result.review?.summary || result.diagnosis?.summary;
  if (!description) {
    if (result.outcome === "healthy") description = `${count ?? 0} checks recorded. The configured gate passed.${result.ai_calls === 0 ? " No AI repair was needed." : ""}`;
    else if (result.outcome === "released") description = "Release checks and deployment health passed. The exact commit is recorded in the evidence panel.";
    else if (terminalStates.has(task.state)) description = result.error || result.detail || "Review the recorded checks and activity for the full outcome.";
    else if (task.state === "paused") description = "The worker will pause at its next control boundary. Add a note or resume when ready.";
    else description = "The worker follows the repository’s configured checks and permissions. Activity appears below as the run progresses.";
  }
  $("#outcome-description").textContent = safeText(description);
  $("#outcome-symbol").textContent = task.state === "completed" ? "✓" : ["failed", "unresolved"].includes(task.state) ? "!" : "·";
  $("#outcome-symbol").dataset.state = knownState(task.state);
}
function renderChecks(task) {
  const groups = resultGroups(task);
  const signature = `${task.id}:${JSON.stringify(groups)}`;
  if (signature === renderedChecks) return;
  renderedChecks = signature;
  const target = $("#check-results");
  const expanded = new Set(Array.from(target.querySelectorAll("details[open]"), node => node.dataset.key));
  target.replaceChildren();
  $("#check-count").textContent = String(groups.reduce((sum, group) => sum + group.results.length, 0));
  if (!groups.length) appendText(target, "p", "No check results have been recorded yet. Results appear when the worker persists run evidence.", "empty");
  for (const group of groups) {
    const section = container(target, "section", "check-group");
    const head = container(section, "div", "check-group-head");
    appendText(head, "h3", group.label);
    const gateState = group.gate?.passed === true ? "passed" : group.gate?.passed === false ? "failed" : "unknown";
    stateLabel(head, gateState, "state-chip");
    if (group.gate?.reasons?.length) appendText(section, "p", group.gate.reasons.join(" · "), "event-caption");
    group.results.forEach((check, index) => {
      const row = container(section, "details", "check-row");
      row.dataset.key = `${task.id}:${group.key}:${index}:${check.id}`;
      row.open = expanded.has(row.dataset.key);
      const summary = container(row, "summary");
      appendText(summary, "span", humanize(check.id), "check-name");
      appendText(summary, "span", Number.isFinite(check.duration_seconds) ? `${check.duration_seconds.toFixed(2)}s` : "—", "check-duration");
      stateLabel(summary, check.status);
      appendText(row, "pre", JSON.stringify(check, null, 2));
      const prefix = `${String(index).padStart(3, "0")}-${check.id}/`;
      for (const [path, content] of Object.entries(group.log_tails || {})) {
        if (content && path.replaceAll("\\", "/").startsWith(prefix)) {
          appendText(row, "p", path, "event-caption");
          appendText(row, "pre", content);
        }
      }
    });
  }
}
const stateDescriptions = {
  preparing: "Preparing the repository and execution environment.", testing: "Running the configured checks.",
  diagnosing: "Analyzing the failure evidence.", repairing: "Applying a repair within the allowed paths.",
  verifying: "Retesting the candidate change.", reviewing: "Reviewing the repair against requirements.",
  publishing: "Publishing the candidate for delivery.", merging: "Waiting for delivery checks and merging.",
  releasing: "Running release checks and deployment hooks.", completed: "The worker recorded the final outcome.",
};
function renderEvents(events) {
  const showSystem = $("#show-system-events").checked;
  const key = `${selectedId}:${showSystem}:${JSON.stringify(events)}`;
  if (key === renderedEvents) return;
  renderedEvents = key;
  const expanded = new Set(Array.from(ui.timeline.querySelectorAll("details[open]"), node => node.dataset.event));
  ui.timeline.replaceChildren();
  const visible = events.filter(event => showSystem || !["lease_renewed", "heartbeat"].includes(event.kind || event.type));
  ui.eventCount.textContent = String(visible.length);
  ui.eventCount.title = `${events.length} total events; ${events.length - visible.length} system events hidden`;
  if (!visible.length) appendText(ui.timeline, "li", "No events recorded yet.", "empty");
  for (const event of visible) {
    const item = container(ui.timeline, "li", "event");
    item.dataset.state = knownState(event.state);
    const head = container(item, "div", "event-head");
    const kind = event.type || event.kind || event.action || "event";
    const title = kind === "transition" ? humanize(event.state) : humanize(kind);
    appendText(head, "strong", title);
    const rawTime = event.created_at ?? event.timestamp ?? event.time;
    const time = appendText(head, "time", formatTimestamp(rawTime, true));
    time.title = formatTimestamp(rawTime);
    if (Number.isFinite(timestamp(rawTime))) time.dateTime = new Date(timestamp(rawTime)).toISOString();
    const description = event.message || (kind === "transition" ? stateDescriptions[event.state] : null);
    if (description) appendText(item, "p", description, "event-caption");
    const details = container(item, "details");
    details.dataset.event = `${selectedId}:${event.id ?? JSON.stringify(event)}`;
    details.open = expanded.has(details.dataset.event);
    appendText(details, "summary", "Inspect event");
    appendText(details, "pre", JSON.stringify(event, null, 2));
  }
}
function switchTab(name, focus = false) {
  document.querySelectorAll("[data-tab]").forEach(button => {
    const active = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    $(`#${button.dataset.tab}-panel`).hidden = !active;
    if (active && focus) button.focus();
  });
}
function renderDetail(task, events) {
  selectedTask = task;
  selectedEvents = events;
  ui.detailEmpty.hidden = true;
  ui.detailContent.hidden = false;
  $("#task-context").hidden = false;
  ui.detailRepository.textContent = safeText(task.repository, "Unknown repository");
  ui.detailId.textContent = safeText(task.id, "Unknown task");
  $("#breadcrumb-current").textContent = safeText(task.repository);
  ui.detailState.textContent = safeText(task.state, "unknown");
  ui.detailState.dataset.state = knownState(task.state);
  renderControls(task.state);
  renderFacts(task);
  renderOutcome(task);
  renderChecks(task);
  renderEvents(events);
  const record = JSON.stringify(task, null, 2);
  if (ui.record.textContent !== record) ui.record.textContent = record;
}
function newInvestigation(focus = true) {
  selectionVersion++;
  selectedId = null;
  selectedTask = null;
  selectedEvents = [];
  ui.detailContent.hidden = true;
  ui.detailEmpty.hidden = false;
  $("#task-context").hidden = true;
  $("#breadcrumb-current").textContent = "New investigation";
  ui.instruction.value = "";
  renderTasks();
  if (focus) ui.repository.focus();
}
async function selectTask(id, quiet = false) {
  if (!id) return;
  const changed = selectedId !== id;
  const version = ++selectionVersion;
  selectedId = id;
  if (changed) {
    selectedTask = null;
    ui.instruction.value = "";
    ui.detailContent.hidden = true;
    $("#task-context").hidden = true;
    $("#breadcrumb-current").textContent = "Loading investigation…";
    switchTab("activity");
  }
  renderTasks();
  try {
    const [taskPayload, eventPayload] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(id)}`), api(`/api/tasks/${encodeURIComponent(id)}/events`),
    ]);
    if (selectedId === id && selectionVersion === version) {
      renderDetail(taskPayload.task, eventPayload.events || []);
      if (changed) {
        $("#task-detail").scrollTop = 0;
        $("#task-detail").focus({ preventScroll: true });
      }
    }
  } catch (error) {
    if (selectedId !== id || selectionVersion !== version) return;
    $("#breadcrumb-current").textContent = "Task unavailable · retrying";
    if (selectedTask) { selectedTask = null; renderControls("unknown"); }
    if (!quiet) showNotice(`Could not load task: ${error.message}`, true);
  }
}
async function loadTasks(quiet = false) {
  if (loadingTasks) return;
  loadingTasks = true;
  try {
    const payload = await api("/api/tasks");
    tasks = (Array.isArray(payload.tasks) ? payload.tasks : []).sort((a, b) => (timestamp(b.created_at ?? b.created) || 0) - (timestamp(a.created_at ?? a.created) || 0));
    $("#connection-status").textContent = "Connected · syncs every 4s";
    $("#connection-dot").dataset.state = "completed";
    renderTasks();
    if (selectedId && tasks.some(task => safeText(task.id, "") === selectedId)) await selectTask(selectedId, quiet);
    else if (selectedId) newInvestigation(false);
  } catch (error) {
    $("#connection-status").textContent = "Connection lost · retrying";
    $("#connection-dot").dataset.state = "failed";
    if (!quiet) showNotice(`Could not refresh tasks: ${error.message}`, true);
  } finally { loadingTasks = false; }
}
async function loadRepositories() {
  try {
    const payload = await api("/api/repositories");
    const selected = ui.repository.value;
    ui.repository.replaceChildren();
    for (const repository of payload.repositories || []) {
      const option = appendText(ui.repository, "option", repository);
      option.value = safeText(repository, "");
    }
    const ready = ui.repository.options.length > 0;
    if (Array.from(ui.repository.options).some(option => option.value === selected)) ui.repository.value = selected;
    if (!ready) appendText(ui.repository, "option", "No registered repositories");
    ui.repository.disabled = !ready;
    ui.createButton.disabled = !ready || createPending;
  } catch (error) {
    ui.repository.replaceChildren();
    appendText(ui.repository, "option", "Repositories unavailable");
    ui.repository.disabled = true;
    ui.createButton.disabled = true;
    showNotice(`Could not load repositories: ${error.message}`, true);
  }
}
async function control(action, instruction) {
  if (!selectedId || !selectedTask || controlPending) return false;
  const id = selectedId;
  const body = { action };
  if (instruction !== undefined) body.instruction = instruction;
  controlPending = true;
  renderControls(selectedTask.state);
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}/control`, { method: "POST", body });
    showNotice(`${humanize(action)} accepted.`);
    if (selectedId === id) await selectTask(id, true);
    await loadTasks(true);
    return true;
  } catch (error) {
    showNotice(`Control rejected: ${error.message}`, true);
    return false;
  } finally {
    controlPending = false;
    renderControls(selectedTask?.state);
  }
}
ui.createForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (createPending || ui.repository.disabled) return;
  createPending = true;
  ui.createButton.disabled = true;
  const version = selectionVersion;
  try {
    const payload = await api("/api/tasks", { method: "POST", body: { repository: ui.repository.value } });
    showNotice("Investigation dispatched.");
    if (version === selectionVersion) await selectTask(safeText(payload.task.id, ""));
    await loadTasks(true);
  } catch (error) { showNotice(`Could not dispatch task: ${error.message}`, true); }
  finally { createPending = false; ui.createButton.disabled = ui.repository.disabled; }
});
ui.steerForm.addEventListener("submit", async event => {
  event.preventDefault();
  const instruction = ui.instruction.value.trim();
  const id = selectedId;
  if (!instruction) return;
  const accepted = await control("steer", instruction);
  if (accepted && selectedId === id && ui.instruction.value.trim() === instruction) ui.instruction.value = "";
});
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => control(button.dataset.action)));
document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => {
  filter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  renderTasks();
}));
document.querySelectorAll("[data-tab]").forEach(button => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
  button.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    switchTab(event.key === "Home" ? "activity" : event.key === "End" ? "checks" : button.dataset.tab === "activity" ? "checks" : "activity", true);
  });
});
$("#task-search").addEventListener("input", renderTasks);
$("#new-investigation").addEventListener("click", () => newInvestigation());
$("#show-system-events").addEventListener("change", () => renderEvents(selectedEvents));
function setInspector(open) {
  $("#context-panel").hidden = !open;
  $(".app-shell").classList.toggle("inspector-closed", !open);
  $("#toggle-inspector").setAttribute("aria-expanded", String(open));
}
$("#toggle-inspector").addEventListener("click", () => setInspector($("#context-panel").hidden));
const compactViewport = window.matchMedia("(max-width:950px)");
setInspector(!compactViewport.matches);
compactViewport.addEventListener("change", event => setInspector(!event.matches));
$("#download-record").addEventListener("click", () => {
  if (!selectedTask) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify({ task: selectedTask, events: selectedEvents }, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `vultron-${safeText(selectedTask.id).replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
});
ui.refresh.addEventListener("click", () => { loadTasks(); loadRepositories(); loadRuntime(); });
window.setInterval(() => { loadTasks(true); loadRuntime(); }, 4000);
function updateClock() { ui.clock.textContent = new Date().toLocaleTimeString([], { hour12: false }); }
window.setInterval(updateClock, 1000);
updateClock();
loadRepositories();
loadTasks();
loadRuntime();
