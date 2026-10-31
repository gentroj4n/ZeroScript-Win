// SPDX-License-Identifier: GPL-3.0-or-later
const SUPPORTED_HOSTS = [
  "chat.deepseek.com", "deepseek.com", "chatgpt.com", "chat.openai.com",
  "gemini.google.com", "www.kimi.ai", "kimi.ai",
  "chat.z.ai", "chat.qwen.ai", "arena.ai", "www.meta.ai", "meta.ai",
];
const DEFAULT_AI_URL = "https://chat.deepseek.com/";

document.getElementById("ver").textContent = `v${chrome.runtime.getManifest().version}`;

function initAutopilot() {
  const toggle = document.getElementById("autopilotToggle");
  const badge = document.getElementById("autopilotBadge");
  const broadcast = (enabled) => {
    chrome.tabs.query({}, (tabs) => {
      for (const tab of tabs) {
        if (!tab.id || !tab.url || !SUPPORTED_HOSTS.some((host) => tab.url.includes(host))) continue;
        chrome.tabs.sendMessage(tab.id, { type: "zs-autopilot-setting", enabled }, () => void chrome.runtime.lastError);
      }
    });
  };
  chrome.storage.local.get({ autopilotEnabled: false }, (value) => {
    toggle.checked = value.autopilotEnabled === true;
    badge.textContent = toggle.checked ? "On · 7 phases max" : "Off";
    badge.className = "badge" + (toggle.checked ? " ready" : "");
  });
  toggle.addEventListener("change", () => {
    const enabled = toggle.checked;
    chrome.storage.local.set({ autopilotEnabled: enabled }, () => {
      badge.textContent = enabled ? "On · 7 phases max" : "Off";
      badge.className = "badge" + (enabled ? " ready" : "");
      broadcast(enabled);
    });
  });
}

function initRunInBackground() {
  const toggle = document.getElementById("runInBackgroundToggle");
  const badge = document.getElementById("backgroundModeBadge");
  if (!toggle || !badge) return;
  const broadcast = (enabled) => {
    chrome.tabs.query({}, (tabs) => {
      for (const tab of tabs) {
        if (!tab.id || !tab.url || !SUPPORTED_HOSTS.some((host) => tab.url.includes(host))) continue;
        chrome.tabs.sendMessage(tab.id, { type: "zs-background-mode", enabled }, () => void chrome.runtime.lastError);
      }
    });
  };
  chrome.storage.local.get({ runInBackground: false }, (value) => {
    toggle.checked = value.runInBackground === true;
    badge.textContent = toggle.checked ? "On" : "Off";
    badge.className = "badge" + (toggle.checked ? " ready" : "");
  });
  toggle.addEventListener("change", () => {
    const enabled = toggle.checked;
    chrome.storage.local.set({ runInBackground: enabled }, () => {
      badge.textContent = enabled ? "On" : "Off";
      badge.className = "badge" + (enabled ? " ready" : "");
      broadcast(enabled);
    });
  });
}



function parseToolText(response) {
  if (!response || typeof response.text !== "string") return null;
  const raw = response.text.trim();
  try { return JSON.parse(raw); } catch {}

  // Bridge results may prefix JSON with "Output of 'tool_name':".
  // Extract the first complete JSON object or array, respecting quoted strings.
  const startObject = raw.indexOf("{");
  const startArray = raw.indexOf("[");
  const starts = [startObject, startArray].filter((index) => index >= 0);
  if (!starts.length) return null;
  const start = Math.min(...starts);
  const opening = raw[start];
  const closing = opening === "{" ? "}" : "]";
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < raw.length; i++) {
    const char = raw[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') { inString = true; continue; }
    if (char === opening) depth++;
    else if (char === closing) {
      depth--;
      if (depth === 0) {
        try { return JSON.parse(raw.slice(start, i + 1)); } catch { return null; }
      }
    }
  }
  return null;
}

function render(s) {
  const dot = document.getElementById("dot");
  const state = document.getElementById("state");
  const detail = document.getElementById("stateDetail");
  const list = Array.isArray(s.servers) ? s.servers : [];
  const up = list.filter((x) => x.alive).length;
  const ready = !!(s.connected && (s.mcpAlive || up > 0 || s.tools > 0));
  dot.className = "dot " + (s.connected ? (ready ? "on" : "warn") : "");
  state.textContent = !s.connected ? "Bridge offline" : ready ? "Local bridge connected" : "Bridge connected · waiting for tools";
  detail.textContent = s.connected ? `${s.tools || 0} tools loaded · ${up} MCP service(s) online` : "Start bridge.py or run start.bat";

  const workspace = s.workspace && typeof s.workspace === "object" ? s.workspace : null;
  if (workspace) {
    document.getElementById("workspaceRoot").textContent = workspace.root || "No valid folder is available.";
    const configured = workspace.configured === true;
    const badge = document.getElementById("workspaceBadge");
    badge.textContent = configured ? "Selected" : "Needs setup";
    badge.className = "badge" + (configured ? " ready" : "");
    document.getElementById("workspaceHint").textContent = configured
      ? "This folder is saved locally and will be reused on future starts. Change it at any time."
      : "No project folder has been confirmed. Pick a folder here or give the AI an absolute path when it asks.";
  } else if (!s.connected) {
    document.getElementById("workspaceRoot").textContent = "Bridge unavailable";
    document.getElementById("workspaceBadge").textContent = "Offline";
    document.getElementById("workspaceHint").textContent = "Connect the local bridge to read or change the selected folder.";
  }

  document.getElementById("toolCount").textContent = `${s.tools || 0}`;
  const roblox = list.find((x) => x.id === "roblox");
  const robloxStatus = document.getElementById("robloxStatus");
  const robloxBadge = document.getElementById("robloxBadge");
  if (!roblox) {
    robloxStatus.textContent = "Not configured · general coding tools work independently";
    robloxBadge.textContent = "Not configured";
    robloxBadge.className = "badge";
  } else if (!roblox.alive) {
    robloxStatus.textContent = "Configured, but its MCP process is offline";
    robloxBadge.textContent = "Offline";
    robloxBadge.className = "badge";
  } else if (s.studio === true) {
    robloxStatus.textContent = "Live Studio connection available";
    robloxBadge.textContent = "Connected";
    robloxBadge.className = "badge ready";
  } else if (s.studio === false) {
    robloxStatus.textContent = "MCP is running; connect Studio from Assistant Settings → MCP Servers";
    robloxBadge.textContent = "Waiting for Studio";
    robloxBadge.className = "badge";
  } else {
    robloxStatus.textContent = "MCP process is running; Studio state is unknown";
    robloxBadge.textContent = "Optional";
    robloxBadge.className = "badge";
  }

  const other = list.filter((x) => x.id !== "roblox");
  document.getElementById("servers").textContent = other.length
    ? `Other integrations · ${other.filter((x) => x.alive).length}/${other.length} online: ${other.map((x) => x.id).join(", ")}`
    : "Optional MCP integrations can be configured separately.";
}

function refresh() {
  chrome.runtime.sendMessage({ type: "status" }, (s) => {
    if (chrome.runtime.lastError) {
      document.getElementById("state").textContent = "Bridge status unavailable";
      return;
    }
    if (s) render(s);
  });
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" }, () => setTimeout(refresh, 600));
});
document.getElementById("restart").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Restarting…";
  chrome.runtime.sendMessage({ type: "restart_mcp" }, () => {
    button.disabled = false;
    button.textContent = original;
    setTimeout(refresh, 900);
  });
});
const openRobloxUiBtn = document.getElementById("openRobloxUi");
if (openRobloxUiBtn) {
  openRobloxUiBtn.addEventListener("click", () => {
    const url = chrome.runtime.getURL("workspace.html");
    chrome.tabs.query({}, (tabs) => {
      const existing = tabs.find((t) => t.url && t.url.startsWith(url));
      if (existing) {
        chrome.tabs.update(existing.id, { active: true });
        if (existing.windowId != null) chrome.windows.update(existing.windowId, { focused: true });
      } else {
        chrome.tabs.create({ url });
      }
    });
  });
}
document.getElementById("settings").addEventListener("click", () => {
  chrome.tabs.query({}, (tabs) => {
    const active = tabs.find((t) => t.active && t.url && SUPPORTED_HOSTS.some((h) => t.url.includes(h)));
    const anySupported = active || tabs.find((t) => t.url && SUPPORTED_HOSTS.some((h) => t.url.includes(h)));
    if (anySupported) {
      chrome.tabs.sendMessage(anySupported.id, { type: "zs-open-menu" });
      chrome.tabs.update(anySupported.id, { active: true });
    } else {
      chrome.tabs.create({ url: DEFAULT_AI_URL });
    }
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "zs-status") render(msg);
});
let activeAutopilotRunId = null;
let lastAutopilotDispatchAt = 0;

function runMessage(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(response || { ok: false, error: "No response from extension background." });
    });
  });
}


function setExplorerStatus(message, tone = "") {
  const node = document.getElementById("explorerStatus");
  if (!node) return;
  node.textContent = message;
  node.className = "hint explorer-status" + (tone ? " " + tone : "");
}

async function callWorkspaceTool(name, args = {}) {
  const response = await runMessage({ type: "call_tool", name, arguments: args, timeout: 30000 });
  if (!response || response.ok !== true) {
    throw new Error((response && (response.error || response.text)) || (name + " failed."));
  }
  const parsed = parseToolText(response);
  if (!parsed || typeof parsed !== "object") {
    throw new Error("The bridge returned an unreadable response for " + name + ".");
  }
  return parsed;
}

function applyWorkspaceDescription(info, fallbackPath = "") {
  const root = (info && (info.root || info.workspace_root)) || fallbackPath;
  if (root) document.getElementById("workspaceRoot").textContent = root;
  const configured = !!(info && info.configured) || !!root;
  const badge = document.getElementById("workspaceBadge");
  badge.textContent = configured ? "Selected" : "Needs setup";
  badge.className = "badge" + (configured ? " ready" : "");
  document.getElementById("workspaceHint").textContent = configured
    ? "Project-aware tools are scoped to this folder. Switch projects from Recent projects at any time."
    : "Choose a project folder to enable project-aware tools. Your selection is saved locally on this computer.";
}

function formatProjectSummary(title, info) {
  const lines = [title];
  if (info && typeof info === "object") {
    for (const [key, value] of Object.entries(info).slice(0, 12)) {
      if (value === null || value === undefined || key === "raw") continue;
      let display;
      if (Array.isArray(value)) display = value.slice(0, 6).map(String).join(", ") + (value.length > 6 ? ", …" : "");
      else if (typeof value === "object") display = Object.entries(value).slice(0, 4).map(([k, v]) => k + "=" + String(v)).join(", ");
      else display = String(value);
      if (display.length > 240) display = display.slice(0, 237) + "…";
      lines.push(key.replaceAll("_", " ") + ": " + display);
    }
  }
  return lines.join("\n");
}

async function loadRecentWorkspaces() {
  const list = document.getElementById("recentWorkspaceList");
  list.replaceChildren();
  try {
    const result = await callWorkspaceTool("workspace_recent_roots");
    const roots = Array.isArray(result.recent_roots) ? result.recent_roots.slice(0, 10) : [];
    if (!roots.length) {
      const empty = document.createElement("div");
      empty.className = "recent-empty";
      empty.textContent = "No recent projects yet. Choose a folder to add the first one.";
      list.appendChild(empty);
      return;
    }
    for (const entry of roots) {
      if (!entry || typeof entry.path !== "string") continue;
      const row = document.createElement("div");
      row.className = "recent-workspace";
      const copy = document.createElement("div");
      copy.className = "recent-workspace-copy";
      const name = document.createElement("div");
      name.className = "recent-workspace-name";
      name.textContent = entry.name || entry.path.split(/[\\/]/).filter(Boolean).pop() || entry.path;
      const path = document.createElement("div");
      path.className = "recent-workspace-path";
      path.textContent = entry.path;
      copy.append(name, path);
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = entry.current ? "Selected" : "Open";
      button.disabled = entry.current === true || entry.exists === false;
      button.title = entry.exists === false ? "This folder no longer exists" : (entry.current ? "This project is already selected" : "Switch the active project to this folder");
      button.addEventListener("click", async () => {
        button.disabled = true;
        button.textContent = "Opening…";
        try {
          const opened = await callWorkspaceTool("workspace_open_recent", { path: entry.path });
          applyWorkspaceDescription(opened, entry.path);
          setExplorerStatus("Opened project: " + entry.path, "success");
          document.getElementById("projectSummary").hidden = true;
          await refresh();
          await loadRecentWorkspaces();
        } catch (error) {
          setExplorerStatus("Could not open project: " + String(error.message || error), "error");
          button.disabled = false;
          button.textContent = "Open";
        }
      });
      row.append(copy, button);
      list.appendChild(row);
    }
  } catch (error) {
    const empty = document.createElement("div");
    empty.className = "recent-empty";
    empty.textContent = "Recent projects unavailable: " + String(error.message || error);
    list.appendChild(empty);
  }
}

document.getElementById("browseWorkspace").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Opening folder picker…";
  setExplorerStatus("Choose a folder in the native Windows picker.");
  try {
    const selection = await callWorkspaceTool("workspace_browse");
    if (selection.cancelled || !selection.chosen || !selection.selected_path) {
      setExplorerStatus("Selection cancelled. The current project was not changed.");
      return;
    }
    const active = await callWorkspaceTool("workspace_set_root", { path: selection.selected_path });
    applyWorkspaceDescription(active, selection.selected_path);
    setExplorerStatus("Project opened and saved: " + selection.selected_path, "success");
    document.getElementById("projectSummary").hidden = true;
    await refresh();
    await loadRecentWorkspaces();
  } catch (error) {
    setExplorerStatus("Could not select a project: " + String(error.message || error), "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});

document.getElementById("toggleRecentWorkspaces").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const panel = document.getElementById("recentWorkspacePanel");
  const opening = panel.hidden;
  panel.hidden = !opening;
  button.setAttribute("aria-expanded", String(opening));
  if (opening) {
    button.textContent = "Hide recent";
    await loadRecentWorkspaces();
  } else {
    button.textContent = "Recent projects";
  }
});

document.getElementById("inspectProject").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const summary = document.getElementById("projectSummary");
  button.disabled = true;
  button.textContent = "Analyzing…";
  try {
    const result = await callWorkspaceTool("workspace_project_info");
    summary.textContent = formatProjectSummary("PROJECT OVERVIEW", result);
    summary.hidden = false;
    setExplorerStatus("Project overview refreshed.", "success");
  } catch (error) {
    setExplorerStatus("Could not inspect project: " + String(error.message || error), "error");
  } finally {
    button.disabled = false;
    button.textContent = "Project overview";
  }
});

document.getElementById("showGitStatus").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const summary = document.getElementById("projectSummary");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const result = await callWorkspaceTool("workspace_git_status");
    summary.textContent = formatProjectSummary("GIT STATUS", result);
    summary.hidden = false;
    setExplorerStatus("Git status refreshed.", "success");
  } catch (error) {
    setExplorerStatus("Could not read Git status: " + String(error.message || error), "error");
  } finally {
    button.disabled = false;
    button.textContent = "Git status";
  }
});

function renderAutopilotRun(run) {
  const badge = document.getElementById("runStateBadge");
  const progress = document.getElementById("runProgress");
  const tasks = document.getElementById("runTasks");
  const stop = document.getElementById("stopAutopilotRun");
  if (!run) {
    badge.textContent = "No run";
    badge.className = "badge";
    progress.textContent = "No run selected.";
    tasks.replaceChildren();
    stop.disabled = true;
    return;
  }
  activeAutopilotRunId = run.id;
  badge.textContent = run.state.replaceAll("_", " ");
  badge.className = "badge" + (run.state === "completed" ? " ready" : "");
  const completed = run.tasks.filter((task) => task.status === "completed").length;
  const active = run.tasks.filter((task) => ["leased", "running"].includes(task.status)).length;
  const ready = run.tasks.filter((task) => task.status === "ready").length;
  progress.textContent = completed + "/" + run.tasks.length + " tasks completed · " + active + " active · " + ready + " ready. Workspace: " + (run.workspace || "(not recorded)");
  stop.disabled = ["completed", "blocked", "needs_approval", "failed", "cancelled"].includes(run.state);
  tasks.replaceChildren();
  for (const task of run.tasks) {
    const row = document.createElement("div");
    row.style.borderTop = "1px solid #263142";
    row.style.padding = "4px 0";
    const title = document.createElement("strong");
    title.textContent = task.role.replaceAll("_", " ") + ": ";
    const status = document.createElement("span");
    status.textContent = task.status;
    row.append(title, status);
    if (task.blockReason) {
      const reason = document.createElement("div");
      reason.style.color = "#e2b878";
      reason.textContent = task.blockReason;
      row.appendChild(reason);
    }
    const dependencies = (task.dependencies || []).map((id) => run.tasks.find((entry) => entry.id === id));
    const dependenciesComplete = dependencies.every((entry) => entry && entry.status === "completed");
    if (["blocked", "failed"].includes(task.status) && dependenciesComplete &&
        !["completed", "cancelled", "needs_approval"].includes(run.state)) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "Retry task";
      retry.className = "secondary";
      retry.style.marginTop = "5px";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        retry.textContent = "Retrying…";
        const result = await runMessage({ type: "autopilot_retry_task", runId: run.id, taskId: task.id });
        if (!result.ok) {
          document.getElementById("runProgress").textContent = "Retry failed: " + result.error;
          retry.disabled = false;
          retry.textContent = "Retry task";
          return;
        }
        renderAutopilotRun(result.run);
        await refreshAutopilotRun(true);
      });
      row.appendChild(retry);
    }
    const evidence = [];
    for (const item of (task.evidence || [])) {
      evidence.push((item.tool ? item.tool + ": " : "") + item.summary + (item.source ? " [" + item.source + "]" : ""));
    }
    for (const item of (task.criteriaEvidence || [])) {
      evidence.push("Criterion " + item.status + ": " + item.criterion + " | tool: " + item.tool + " | excerpt: " + item.excerpt);
    }
    for (const finding of (task.findings || [])) evidence.push("Finding: " + finding);
    if (evidence.length) {
      const details = document.createElement("details");
      details.style.marginTop = "5px";
      const summary = document.createElement("summary");
      summary.textContent = "Evidence & findings (" + evidence.length + ")";
      summary.style.cursor = "pointer";
      summary.style.color = "#9eb3d5";
      const body = document.createElement("pre");
      body.textContent = evidence.join("\n");
      body.style.whiteSpace = "pre-wrap";
      body.style.overflowWrap = "anywhere";
      body.style.font = "10px/1.5 Consolas, monospace";
      body.style.color = "#aab6c9";
      details.append(summary, body);
      row.appendChild(details);
    }
    tasks.appendChild(row);
  }
}

async function refreshAutopilotRun(dispatch = false) {
  if (!activeAutopilotRunId) return;
  const result = await runMessage({ type: "autopilot_get_run", runId: activeAutopilotRunId });
  if (!result.ok || !result.run) return;
  renderAutopilotRun(result.run);
  if (dispatch && Date.now() - lastAutopilotDispatchAt > 4500 &&
      !["completed", "blocked", "needs_approval", "failed", "cancelled"].includes(result.run.state)) {
    lastAutopilotDispatchAt = Date.now();
    const dispatched = await runMessage({ type: "autopilot_dispatch_ready", runId: activeAutopilotRunId });
    if (!dispatched.ok) {
      document.getElementById("runProgress").textContent = "Dispatch error: " + dispatched.error;
    }
  }
}

document.getElementById("startAutopilotRun").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const goal = document.getElementById("runGoal").value.trim();
  const acceptanceCriteria = document.getElementById("runCriteria").value.split("\n").map((x) => x.trim()).filter(Boolean);
  if (!goal || !acceptanceCriteria.length) {
    document.getElementById("runProgress").textContent = "Enter a goal and at least one acceptance criterion.";
    return;
  }
  button.disabled = true;
  button.textContent = "Creating…";
  try {
    const result = await runMessage({
      type: "autopilot_create_run",
      input: {
        goal,
        acceptanceCriteria,
        workspace: document.getElementById("workspaceBadge").textContent === "Selected"
          ? document.getElementById("workspaceRoot").textContent.trim()
          : "",
      },
    });
    if (!result.ok || !result.run) throw new Error(result.error || "Could not create run.");
    activeAutopilotRunId = result.run.id;
    chrome.storage.local.set({ zsAutopilotActiveRunId: activeAutopilotRunId });
    renderAutopilotRun(result.run);
    await refreshAutopilotRun(true);
  } catch (error) {
    document.getElementById("runProgress").textContent = "Could not start run: " + String(error.message || error);
  } finally {
    button.disabled = false;
    button.textContent = "Start run";
  }
});

document.getElementById("stopAutopilotRun").addEventListener("click", async () => {
  if (!activeAutopilotRunId) return;
  const result = await runMessage({ type: "autopilot_cancel_run", runId: activeAutopilotRunId, reason: "Stopped by user in the extension popup." });
  if (!result.ok) document.getElementById("runProgress").textContent = "Stop failed: " + result.error;
  else renderAutopilotRun(result.run);
});

chrome.storage.local.get({ zsAutopilotActiveRunId: null }, async (saved) => {
  activeAutopilotRunId = saved.zsAutopilotActiveRunId || null;
  await refreshAutopilotRun(true);
});

initAutopilot();
initRunInBackground();
refresh();
setInterval(() => { refresh(); refreshAutopilotRun(true); }, 2500);
