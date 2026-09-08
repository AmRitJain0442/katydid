"use strict";

const ui = {
  clock: document.querySelector("#clock"),
  createForm: document.querySelector("#create-form"),
  repository: document.querySelector("#repository"),
  createButton: document.querySelector("#create-form button"),
  refresh: document.querySelector("#refresh"),
  taskCount: document.querySelector("#task-count"),
  taskList: document.querySelector("#task-list"),
  detailEmpty: document.querySelector("#detail-empty"),
  detailContent: document.querySelector("#detail-content"),
  detailRepository: document.querySelector("#detail-repository"),
  detailId: document.querySelector("#detail-id"),
  detailState: document.querySelector("#detail-state"),
  facts: document.querySelector("#facts"),
  timeline: document.querySelector("#timeline"),
  eventCount: document.querySelector("#event-count"),
  record: document.querySelector("#record"),
  steerForm: document.querySelector("#steer-form"),
  instruction: document.querySelector("#instruction"),
  pauseButton: document.querySelector('[data-action="pause"]'),
  resumeButton: document.querySelector('[data-action="resume"]'),
  cancelButton: document.querySelector('[data-action="cancel"]'),
  steerButton: document.querySelector("#steer-form button"),
  notice: document.querySelector("#notice"),
};

let tasks = [];
let selectedId = null;
let noticeTimer = null;
let renderedEvents = "";
let renderedRuntime = "";

async function loadRuntime() {
  const panel = document.querySelector("#runtime-info");
  try {
    const { runtime } = await api("/api/runtime");
    const signature = JSON.stringify(runtime);
    if (signature === renderedRuntime) return;
    renderedRuntime = signature;
    panel.replaceChildren();
    if (!runtime.managed) {
      appendText(panel, "p", "Worker configuration is managed externally.");
      return;
    }
    appendText(panel, "strong", runtime.worker_alive ? "Worker online" : "Worker stopped");
    appendText(panel, "p", `${runtime.provider} · ${runtime.model}`);
    appendText(panel, "p", runtime.watch ? `Watching every ${runtime.interval_seconds}s` : "Manual and event dispatch");
    appendText(panel, "p", runtime.schedule_seconds ? `Scheduled checks every ${runtime.schedule_seconds}s` : "Scheduled checks disabled");
    for (const repository of runtime.repositories || []) {
      appendText(panel, "strong", repository.id);
      appendText(panel, "p", Object.keys(repository.required_checks || {}).join(" · "));
      appendText(panel, "p", `${repository.delivery} delivery · ${repository.auto_merge ? "automatic merge" : "merge disabled"}`);
      appendText(panel, "p", repository.auto_deploy ? "Automatic deployment enabled" : repository.release_configured ? "Release hooks configured" : "Deployment not configured");
    }
  } catch {
    renderedRuntime = "";
    panel.textContent = "Runtime status unavailable.";
  }
}

function safeText(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function knownState(value) {
  const state = safeText(value, "unknown").toLowerCase();
  if (["queued", "discovered"].includes(state)) return "queued";
  if (["completed", "passed", "paused", "failed", "error", "cancelled"].includes(state)) return state;
  if (state === "unresolved") return "failed";
  if (["running", "preparing", "planning", "testing", "diagnosing", "executing", "verifying", "repairing", "reviewing", "eligible", "merging", "publishing", "deploying", "releasing", "monitoring"].includes(state)) return "running";
  return "unknown";
}

function formatTimestamp(value) {
  if (value === null || value === undefined || value === "") return safeText(value);
  const milliseconds = typeof value === "number" ? value * 1000 : Date.parse(String(value));
  if (!Number.isFinite(milliseconds)) return safeText(value);
  return new Date(milliseconds).toLocaleString();
}

function renderControls(value) {
  const state = safeText(value, "unknown").toLowerCase();
  const terminal = ["completed", "failed", "cancelled", "unresolved"].includes(state);
  ui.pauseButton.disabled = terminal || state === "paused";
  ui.resumeButton.disabled = terminal || state !== "paused";
  ui.cancelButton.disabled = terminal;
  ui.instruction.disabled = terminal;
  ui.steerButton.disabled = terminal;
}

function showNotice(message, isError = false) {
  window.clearTimeout(noticeTimer);
  ui.notice.textContent = message;
  ui.notice.classList.toggle("error", isError);
  ui.notice.classList.add("visible");
  noticeTimer = window.setTimeout(() => ui.notice.classList.remove("visible"), 4200);
}

async function api(path, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
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

function appendText(parent, tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = safeText(text);
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function renderTasks() {
  ui.taskList.replaceChildren();
  ui.taskCount.textContent = `${tasks.length} task${tasks.length === 1 ? "" : "s"}`;
  if (!tasks.length) {
    appendText(ui.taskList, "div", "No investigations dispatched yet.", "empty");
    return;
  }
  for (const task of tasks) {
    const id = safeText(task.id, "");
    const button = document.createElement("button");
    button.type = "button";
    button.className = `task-card${id === selectedId ? " selected" : ""}`;
    button.setAttribute("aria-pressed", String(id === selectedId));
    button.addEventListener("click", () => selectTask(id));
    appendText(button, "strong", task.repository, null);
    const meta = document.createElement("span");
    meta.className = "task-meta";
    appendText(meta, "span", id, "task-id");
    const state = appendText(meta, "span", task.state, "state-dot");
    state.dataset.state = knownState(task.state);
    button.append(meta);
    ui.taskList.append(button);
  }
}

function renderFacts(task) {
  ui.facts.replaceChildren();
  const fields = [
    ["State", task.state],
    ["Epoch", task.epoch],
    ["Created", formatTimestamp(task.created_at ?? task.created)],
    ["Updated", formatTimestamp(task.updated_at ?? task.updated)],
  ];
  for (const [name, value] of fields) {
    const wrapper = document.createElement("div");
    wrapper.className = "fact";
    appendText(wrapper, "dt", name, null);
    appendText(wrapper, "dd", value, null);
    wrapper.title = safeText(value);
    ui.facts.append(wrapper);
  }
}

function eventTitle(event) {
  return event.type || event.kind || event.action || event.state || "Event";
}

function eventTime(event) {
  return event.created_at || event.timestamp || event.time || "";
}

function eventDetail(event) {
  return JSON.stringify(event, null, 2);
}

function renderEvents(events) {
  const eventKey = `${selectedId}:${JSON.stringify(events)}`;
  if (eventKey === renderedEvents) return;
  const expanded = new Set(Array.from(ui.timeline.querySelectorAll("details[open]"), (node) => node.dataset.event));
  const scrollTop = ui.timeline.scrollTop;
  renderedEvents = eventKey;
  ui.timeline.replaceChildren();
  ui.eventCount.textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  if (!events.length) {
    appendText(ui.timeline, "li", "No events recorded.", "empty");
    return;
  }
  for (const event of events) {
    const item = document.createElement("li");
    item.className = "event";
    const head = document.createElement("div");
    head.className = "event-head";
    appendText(head, "strong", eventTitle(event), null);
    const rawTime = eventTime(event);
    const time = appendText(head, "time", formatTimestamp(rawTime), null);
    time.title = safeText(rawTime);
    item.append(head);
    const details = document.createElement("details");
    details.dataset.event = `${selectedId}:${JSON.stringify(event)}`;
    details.open = expanded.has(details.dataset.event);
    appendText(details, "summary", "Inspect event", null);
    appendText(details, "p", eventDetail(event), null);
    item.append(details);
    ui.timeline.append(item);
  }
  ui.timeline.scrollTop = scrollTop;
}

function renderDetail(task, events) {
  ui.detailEmpty.hidden = true;
  ui.detailContent.hidden = false;
  ui.detailRepository.textContent = safeText(task.repository, "Unknown repository");
  ui.detailId.textContent = safeText(task.id, "Unknown task");
  ui.detailState.textContent = safeText(task.state, "unknown");
  ui.detailState.dataset.state = knownState(task.state);
  renderControls(task.state);
  renderFacts(task);
  renderEvents(events);
  ui.record.textContent = JSON.stringify(task, null, 2);
}

async function selectTask(id, quiet = false) {
  if (!id) return;
  selectedId = id;
  renderTasks();
  try {
    const [taskPayload, eventPayload] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(id)}`),
      api(`/api/tasks/${encodeURIComponent(id)}/events`),
    ]);
    if (selectedId === id) renderDetail(taskPayload.task, eventPayload.events || []);
  } catch (error) {
    if (!quiet) showNotice(`Could not load task: ${error.message}`, true);
  }
}

async function loadTasks(quiet = false) {
  try {
    const payload = await api("/api/tasks");
    tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    renderTasks();
    if (selectedId && tasks.some((task) => safeText(task.id, "") === selectedId)) {
      await selectTask(selectedId, quiet);
    } else if (selectedId) {
      selectedId = null;
      ui.detailContent.hidden = true;
      ui.detailEmpty.hidden = false;
    }
  } catch (error) {
    if (!quiet) showNotice(`Could not refresh tasks: ${error.message}`, true);
  }
}

async function loadRepositories() {
  try {
    const payload = await api("/api/repositories");
    ui.repository.replaceChildren();
    for (const repository of payload.repositories || []) {
      const option = document.createElement("option");
      option.value = safeText(repository, "");
      option.textContent = safeText(repository);
      ui.repository.append(option);
    }
    const ready = ui.repository.options.length > 0;
    ui.repository.disabled = !ready;
    ui.createButton.disabled = !ready;
  } catch (error) {
    ui.repository.replaceChildren();
    appendText(ui.repository, "option", "Repositories unavailable", null);
    showNotice(`Could not load repositories: ${error.message}`, true);
  }
}

async function control(action, instruction) {
  if (!selectedId) return;
  const body = { action };
  if (instruction !== undefined) body.instruction = instruction;
  try {
    const payload = await api(`/api/tasks/${encodeURIComponent(selectedId)}/control`, { method: "POST", body });
    showNotice(`${action[0].toUpperCase()}${action.slice(1)} accepted.`);
    await loadTasks(true);
    if (payload.task) renderDetail(payload.task, (await api(`/api/tasks/${encodeURIComponent(selectedId)}/events`)).events || []);
  } catch (error) {
    showNotice(`Control rejected: ${error.message}`, true);
  }
}

ui.createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  ui.createButton.disabled = true;
  try {
    const payload = await api("/api/tasks", { method: "POST", body: { repository: ui.repository.value } });
    selectedId = safeText(payload.task.id, "");
    showNotice("Investigation dispatched.");
    await loadTasks(true);
  } catch (error) {
    showNotice(`Could not dispatch task: ${error.message}`, true);
  } finally {
    ui.createButton.disabled = false;
  }
});

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", () => control(button.dataset.action));
});

ui.steerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const instruction = ui.instruction.value.trim();
  if (!instruction) return;
  await control("steer", instruction);
  ui.instruction.value = "";
});

ui.refresh.addEventListener("click", () => loadTasks());
window.setInterval(() => loadTasks(true), 4000);
window.setInterval(loadRuntime, 4000);
window.setInterval(() => { ui.clock.textContent = new Date().toLocaleTimeString([], { hour12: false }); }, 1000);
ui.clock.textContent = new Date().toLocaleTimeString([], { hour12: false });

loadRepositories();
loadTasks();
loadRuntime();
