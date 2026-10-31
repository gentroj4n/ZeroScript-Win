// SPDX-License-Identifier: GPL-3.0-or-later
// background.js - service worker.
// Owns ONE resilient WebSocket to the local bridge (ws://127.0.0.1:PORT).
// Keeping the socket here (not in the content script) avoids https→ws mixed
// content issues and centralises reconnect / timeout logic.
//
// Contract with content.js: every sendMessage ALWAYS gets a response object,
// even when the bridge is offline. The agentic loop must never hang waiting.

importScripts("core/autopilot-ledger.js");

const AUTOPILOT_RUNS_KEY = "zsAutopilotRuns";
let autopilotStorageQueue = Promise.resolve();
async function readAutopilotRuns() {
  const result = await chrome.storage.local.get({ [AUTOPILOT_RUNS_KEY]: {} });
  return result[AUTOPILOT_RUNS_KEY] && typeof result[AUTOPILOT_RUNS_KEY] === "object"
    ? result[AUTOPILOT_RUNS_KEY] : {};
}
function writeAutopilotRuns(runs) {
  autopilotStorageQueue = autopilotStorageQueue.catch(() => {}).then(() =>
    chrome.storage.local.set({ [AUTOPILOT_RUNS_KEY]: runs })
  );
  return autopilotStorageQueue;
}
async function mutateAutopilotRun(runId, mutate) {
  return autopilotStorageQueue = autopilotStorageQueue.catch(() => {}).then(async () => {
    const runs = await readAutopilotRuns();
    const run = runs[runId];
    if (!run) throw new Error("Unknown Autopilot run: " + runId);
    const value = await mutate(run);
    runs[runId] = run;
    await chrome.storage.local.set({ [AUTOPILOT_RUNS_KEY]: runs });
    return value === undefined ? run : value;
  });
}
async function recoverAutopilotRuns() {
  const runs = await readAutopilotRuns();
  let changed = false;
  for (const [id, run] of Object.entries(runs)) {
    try {
      const recovered = ZSAutopilotLedger.recoverExpiredLeases(run);
      if (recovered.length) changed = true;
      ZSAutopilotLedger.refreshReady(run);
      if (run.stopRequested) run.state = "cancelled";
      runs[id] = run;
    } catch (error) {
      console.warn("[ArcScript] Autopilot ledger recovery skipped", id, String(error));
    }
  }
  if (changed) await writeAutopilotRuns(runs);
  return runs;
}

const PORT = 17613;
const URL = `ws://127.0.0.1:${PORT}`;

// Chat sites where a ArcScript provider content script runs. Status pushes go
// to every tab matching these. Add the new provider's URL pattern here (and in
// manifest.json content_scripts + host_permissions) when integrating another AI.
const PROVIDER_URLS = ["https://chat.deepseek.com/*", "https://chatgpt.com/*", "https://chat.openai.com/*", "https://gemini.google.com/*", "https://www.kimi.ai/*", "https://kimi.ai/*", "https://chat.z.ai/*", "https://chat.qwen.ai/*", "https://arena.ai/*", "https://www.meta.ai/*", "https://meta.ai/*"];

const RECONNECT_MIN = 1000;
const RECONNECT_MAX = 5000;
const HEARTBEAT_MS = 10000;
// If no message (incl. pong) arrives within this window while we believe we're
// connected, the socket is half-open: force a reconnect instead of letting
// pending requests slowly time out.
const STALE_SOCKET_MS = 25000;
const REQUEST_TIMEOUT_DEFAULT = 130000; // a bit above the 120s tool timeout

// Workspace tools share one bridge-wide active root. Serialize root activation
// and the subsequent tool call in this worker so separate AI tabs cannot switch
// the bridge between those two operations.
let workspaceDispatchQueue = Promise.resolve();
function queueWorkspaceDispatch(operation) {
  const run = workspaceDispatchQueue.catch(() => {}).then(operation);
  workspaceDispatchQueue = run.catch(() => {});
  return run;
}
async function sendWorkspaceTool(name, args, timeout) {
  const duration = timeout || 120000;
  const response = await send({
    type: "call_tool", name, arguments: args || {}, timeout: duration
  }, duration + 10000);
  if (!response || !response.ok) {
    throw new Error(response && response.error || "Workspace operation failed: " + name);
  }
  return response;
}
async function parseWorkspaceResult(response) {
  let value = response && response.result !== undefined ? response.result : response;
  // The bridge may return {text, images}, MCP content blocks, a JSON string,
  // or nested {result/content} envelopes. Unwrap each form before inspecting it.
  for (let depth = 0; depth < 6; depth++) {
    if (Array.isArray(value)) {
      const part = value.find((item) => item && typeof item.text === "string");
      if (!part) return value;
      value = part.text;
      continue;
    }
    if (value && typeof value === "object") {
      if (typeof value.root === "string" || typeof value.recent_roots !== "undefined" ||
          typeof value.selected_path === "string" || typeof value.cancelled === "boolean") return value;
      if (typeof value.text === "string") { value = value.text; continue; }
      if (value.content !== undefined) { value = value.content; continue; }
      if (value.result !== undefined) { value = value.result; continue; }
      return value;
    }
    if (typeof value === "string") {
      try { value = JSON.parse(value); continue; } catch { return value; }
    }
    return value;
  }
  return value;
}
function normalizeWorkspacePath(value) {
  return String(value || "").trim().replace(/\//g, "\\").replace(/[\\]+$/, "").toLowerCase();
}
async function activateWorkspaceRoot(projectPath) {
  // Do not perform a recent-roots lookup for every operation: the selected path
  // is already explicitly assigned to this conversation. Extra bridge round trips
  // here were causing activation to stall on some response envelopes.
  const activate = await sendWorkspaceTool("workspace_set_root", { path: projectPath, persist: false }, 30000);
  const opened = await parseWorkspaceResult(activate);
  const actualRoot = opened && (opened.root || opened.workspace_root);
  if (!actualRoot || opened.configured === false ||
      normalizeWorkspacePath(actualRoot) !== normalizeWorkspacePath(projectPath)) {
    throw new Error("Could not activate project directory: " + projectPath +
      (opened && (opened.error || opened.message) ? " (" + String(opened.error || opened.message) + ")" : ""));
  }
  return actualRoot;
}
async function callToolInProject(projectPath, name, args, timeout) {
  // Pass the conversation root as request metadata. The bridge creates a scoped
  // manager view for this call, so parallel conversations never mutate or queue
  // behind the shared global workspace root.
  const duration = timeout || 120000;
  const response = await send({
    type: "call_tool", name, arguments: args || {}, timeout: duration,
    workspace_root: projectPath
  }, duration + 10000);
  if (!response || !response.ok) {
    throw new Error(response && response.error || "Workspace operation failed: " + name);
  }
  return response;
}

let ws = null;
let connected = false;
let reconnectDelay = RECONNECT_MIN;
let reconnectTimer = null;
let heartbeatTimer = null;
let lastMessageAt = 0; // timestamp of the last frame received from the bridge
let nextId = 1;
const pending = new Map(); // id -> {resolve, timer}
let toolsCache = [];
let mcpAlive = false;
let serversCache = [];
// true/false = a PLACE is loaded and usable in Roblox Studio; null = unknown.
// The MCP process stays alive when Studio is closed or its MCP option is off,
// so this is probed separately (bridge "studio_status").
let studioConnected = null;
// true/false = a Roblox Studio app is connected to the MCP server at all; null =
// unknown. studioApp=true with studioConnected=false means "Studio open but no
// place"; studioApp=false means "Studio closed OR its MCP option disabled".
let studioApp = null;
// true/false = a Roblox Studio WINDOW/PROCESS exists on this machine (checked
// bridge-side via tasklist); null = unknown/old bridge. Distinguishes the two
// studioApp=false sub-cases the UI must word differently: Studio genuinely not
// launched ("open Roblox Studio") vs Studio OPEN but its MCP plugin never
// registered with the bridge - the documented fix for the latter is opening
// Assistant Settings > MCP Servers inside Studio (validated live 3x), which
// "open Roblox Studio" wording completely fails to convey.
let studioProc = null;
let workspaceCache = null;
let agentControlCache = null;

function log(...a) {
  console.log("[zs-bg]", ...a);
}

// ── WebSocket lifecycle ─────────────────────────────────────────────────
function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  clearTimeout(reconnectTimer);
  try {
    ws = new WebSocket(URL);
  } catch (e) {
    log("WebSocket ctor failed", e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    connected = true;
    reconnectDelay = RECONNECT_MIN;
    lastMessageAt = Date.now();
    log("connected to bridge");
    try { ws.send(JSON.stringify({ type: "register_client", client: "browser" })); } catch {}
    startHeartbeat();
    broadcastStatus();
  };

  ws.onmessage = (ev) => {
    lastMessageAt = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleBridgeMessage(msg);
  };

  ws.onclose = () => {
    connected = false;
    mcpAlive = false;
    studioConnected = null;
    studioApp = null;
    studioProc = null;
    serversCache = [];
    stopHeartbeat();
    failAllPending("bridge connection closed");
    broadcastStatus();
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose will follow; nothing to do here but avoid an unhandled error.
    try { ws.close(); } catch {}
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.7, RECONNECT_MAX);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (connected) {
      // Half-open socket: the WS still reports OPEN but nothing comes through.
      // The pong (and every other frame) refreshes lastMessageAt; if it has
      // gone stale, drop the dead socket so onclose triggers a reconnect.
      if (lastMessageAt && Date.now() - lastMessageAt > STALE_SOCKET_MS) {
        log("socket stale, forcing reconnect");
        try { ws.close(); } catch {}
        return;
      }
      // Keeps the MV3 service worker alive AND detects a half-open socket.
      send({ type: "ping" }).catch(() => {});
      refreshStudioStatus();
    }
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

// Resolve once the socket is OPEN, or false after `timeout` ms.
function waitForConnection(timeout = 8000) {
  return new Promise((resolve) => {
    if (connected && ws && ws.readyState === WebSocket.OPEN) return resolve(true);
    connect(); // nudge a (re)connection - important after a worker wake-up
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (connected && ws && ws.readyState === WebSocket.OPEN) {
        clearInterval(iv);
        resolve(true);
      } else if (Date.now() - t0 > timeout) {
        clearInterval(iv);
        resolve(false);
      }
    }, 100);
  });
}

// ── request/response over the socket ────────────────────────────────────
async function send(obj, timeout = REQUEST_TIMEOUT_DEFAULT) {
  // The MV3 service worker can be suspended; the first message after a wake-up
  // arrives before the socket has re-opened. Wait for it instead of failing -
  // otherwise Kimi wrongly hears "bridge offline".
  if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
    await waitForConnection(8000);
  }
  return new Promise((resolve) => {
    if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, kind: "disconnected", error: "bridge not connected" });
      return;
    }
    const id = nextId++;
    const payload = { ...obj, id };
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        resolve({ ok: false, kind: "timeout", error: "bridge did not respond in time" });
      }
    }, timeout);
    pending.set(id, { resolve, timer });
    try {
      ws.send(JSON.stringify(payload));
    } catch (e) {
      clearTimeout(timer);
      pending.delete(id);
      resolve({ ok: false, kind: "disconnected", error: String(e) });
    }
  });
}

// Ask the bridge whether a Roblox Studio instance is actually connected to the
// MCP server. Broadcasts only on change so the UI updates promptly but quietly.
let studioProbing = false;
async function refreshStudioStatus() {
  if (studioProbing || !connected) return;
  studioProbing = true;
  try {
    const r = await send({ type: "studio_status" }, 12000);
    const v = r && r.ok && typeof r.studio === "boolean" ? r.studio : null;
    if (v !== studioConnected) {
      studioConnected = v;
      broadcastStatus();
    }
  } finally {
    studioProbing = false;
  }
}

function handleBridgeMessage(msg) {
  if (msg.agent_control && typeof msg.agent_control === "object") {
    agentControlCache = msg.agent_control;
  }
  if (msg.type === "agent_control") {
    agentControlCache = msg.control && typeof msg.control === "object" ? msg.control : {};
    broadcastAgentControl();
    broadcastStatus();
    return;
  }
  if (msg.workspace && typeof msg.workspace === "object") {
    workspaceCache = msg.workspace;
  }
  if ("studio" in msg && (typeof msg.studio === "boolean" || msg.studio === null)) {
    studioConnected = msg.studio;
  }
  if ("studio_app" in msg && (typeof msg.studio_app === "boolean" || msg.studio_app === null)) {
    studioApp = msg.studio_app;
  }
  if ("studio_proc" in msg && (typeof msg.studio_proc === "boolean" || msg.studio_proc === null)) {
    studioProc = msg.studio_proc;
  }
  if (msg.type === "studio_status") {
    resolvePending(msg.id, { ok: true, studio: studioConnected });
    broadcastStatus();
    return;
  }
  if (msg.type === "connected") {
    mcpAlive = !!msg.mcp_alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    broadcastStatus();
    return;
  }
  if (msg.type === "pong") {
    resolvePending(msg.id, { ok: true });
    return;
  }
  if (msg.type === "tools") {
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    mcpAlive = !!msg.mcp_alive;
    resolvePending(msg.id, { ok: true, tools: toolsCache });
    broadcastStatus();
    return;
  }
  if (msg.type === "tool_result") {
    resolvePending(msg.id, msg.ok
      ? { ok: true, text: msg.text, images: msg.images || [] }
      : { ok: false, kind: msg.kind, error: msg.error });
    return;
  }
  if (msg.type === "mcp_status") {
    mcpAlive = !!msg.alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    resolvePending(msg.id, { ok: !!msg.ok, alive: msg.alive, error: msg.error });
    broadcastStatus();
    return;
  }
  if (msg.type === "server_changed") {
    // The bridge acks, then restarts itself to reload config.json. The socket
    // will drop right after this - the content script shows a spinner until the
    // reconnect lands and a fresh status arrives.
    resolvePending(msg.id, { ok: !!msg.ok, error: msg.error, restarting: !!msg.restarting });
    return;
  }
  if (msg.type === "error") {
    resolvePending(msg.id, { ok: false, error: msg.error });
    return;
  }
}

function resolvePending(id, value) {
  const p = pending.get(id);
  if (!p) return;
  clearTimeout(p.timer);
  pending.delete(id);
  p.resolve(value);
}

function failAllPending(reason) {
  for (const [, p] of pending) {
    clearTimeout(p.timer);
    p.resolve({ ok: false, kind: "disconnected", error: reason });
  }
  pending.clear();
}

// ── status push to any open DeepSeek tab + popup ─────────────────────────
function statusObj() {
  return { type: "zs-status", connected, mcpAlive, studio: studioConnected, studioApp, studioProc, tools: toolsCache.length, servers: serversCache, workspace: workspaceCache, agentControl: agentControlCache };
}

function broadcastAgentControl() {
  const control = agentControlCache || {};
  const message = { type: "zs-agent-control", ...control };
  chrome.runtime.sendMessage(message).catch(() => {});
  chrome.tabs.query({ url: PROVIDER_URLS }, (tabs) => {
    for (const t of tabs) chrome.tabs.sendMessage(t.id, message).catch(() => {});
  });
}

function broadcastStatus() {
  chrome.runtime.sendMessage(statusObj()).catch(() => {});
  chrome.tabs.query({ url: PROVIDER_URLS }, (tabs) => {
    for (const t of tabs) chrome.tabs.sendMessage(t.id, statusObj()).catch(() => {});
  });
}

// ── messages from content.js / popup.js ─────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "autopilot_create_run": {
        const input = { ...(msg.input || {}) };
        if (!input.workspace && workspaceCache && workspaceCache.root) input.workspace = workspaceCache.root;
        const run = ZSAutopilotLedger.createRun(input);
        const runs = await readAutopilotRuns();
        runs[run.id] = run;
        await writeAutopilotRuns(runs);
        sendResponse({ ok: true, run });
        break;
      }
      case "autopilot_get_run": {
        const runs = await readAutopilotRuns();
        sendResponse({ ok: !!runs[msg.runId], run: runs[msg.runId] || null });
        break;
      }
      case "autopilot_list_runs": {
        const runs = await recoverAutopilotRuns();
        sendResponse({ ok: true, runs: Object.values(runs).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 50) });
        break;
      }
      case "autopilot_refresh_ready": {
        const run = await mutateAutopilotRun(msg.runId, (value) => ZSAutopilotLedger.refreshReady(value));
        sendResponse({ ok: true, run });
        break;
      }
      case "autopilot_dispatch_ready": {
        // Recovery must be committed, not performed on a detached read snapshot.
        // Otherwise an expired lease remains persisted and cannot be reclaimed
        // when no worker happens to be available during this dispatch pass.
        const run = await mutateAutopilotRun(msg.runId, (value) => {
          ZSAutopilotLedger.recoverExpiredLeases(value);
          ZSAutopilotLedger.refreshReady(value);
          return value;
        });
        if (run.stopRequested || ["completed", "blocked", "needs_approval", "failed", "cancelled"].includes(run.state)) {
          sendResponse({ ok: true, dispatched: [], runState: run.state });
          break;
        }
        const tabs = await new Promise((resolve) => chrome.tabs.query({ url: PROVIDER_URLS }, resolve));
        const dispatched = [];
        for (const tab of tabs) {
          if (!tab.id) continue;
          let worker = null;
          try {
            worker = await new Promise((resolve) => chrome.tabs.sendMessage(tab.id, { type: "zs-autopilot-worker-probe" }, (response) => {
              if (chrome.runtime.lastError) resolve(null); else resolve(response || null);
            }));
          } catch {}
          if (!worker || worker.available !== true) continue;
          const claimed = await mutateAutopilotRun(msg.runId, (value) =>
            ZSAutopilotLedger.claimNext(value, {
              id: (worker.workerId || worker.provider || "worker") + ":" + tab.id,
              provider: worker.provider, tabId: tab.id, roles: worker.roles,
            })
          );
          if (!claimed) continue;
          const task = claimed.task, lease = claimed.lease;
          const accepted = await new Promise((resolve) => chrome.tabs.sendMessage(tab.id, {
            type: "zs-autopilot-worker-dispatch", runId: msg.runId, taskId: task.id, task, lease,
          }, (response) => {
            if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
            else resolve(response || { ok: false, error: "Worker did not acknowledge dispatch." });
          }));
          if (accepted.ok) dispatched.push({ taskId: task.id, role: task.role, tabId: tab.id, provider: worker.provider });
          else {
            await mutateAutopilotRun(msg.runId, (value) => {
              const current = value.tasks.find((t) => t.id === task.id);
              if (current && current.status === "leased" && current.lease && current.lease.token === lease.token) {
                ZSAutopilotLedger.transition(value, task.id, "ready", { reason: accepted.error || "Worker rejected dispatch." });
              }
              return value;
            });
          }
        }
        const updated = await mutateAutopilotRun(msg.runId, (value) => {
          ZSAutopilotLedger.refreshReady(value);
          return { runState: value.state, ready: value.tasks.filter((t) => t.status === "ready").length, active: value.tasks.filter((t) => ["leased", "running"].includes(t.status)).length };
        });
        sendResponse({ ok: true, dispatched, ...updated });
        break;
      }
      case "autopilot_claim_task": {
        const result = await mutateAutopilotRun(msg.runId, (run) => {
          ZSAutopilotLedger.recoverExpiredLeases(run);
          const claim = ZSAutopilotLedger.claimNext(run, msg.worker || {});
          return claim ? { task: claim.task, lease: claim.lease, runState: run.state } : { task: null, lease: null, runState: run.state };
        });
        sendResponse({ ok: true, ...result });
        break;
      }
      case "autopilot_heartbeat_task": {
        const lease = await mutateAutopilotRun(msg.runId, (run) =>
          ZSAutopilotLedger.renewLease(run, msg.taskId, msg.leaseToken, msg.leaseMs)
        );
        sendResponse({ ok: true, lease });
        break;
      }
      case "autopilot_retry_task": {
        const run = await mutateAutopilotRun(msg.runId, (value) =>
          ZSAutopilotLedger.retryTask(value, msg.taskId, "Manual retry requested from the Autopilot run panel.")
        );
        sendResponse({ ok: true, run });
        break;
      }
      case "autopilot_transition_task": {
        const run = await mutateAutopilotRun(msg.runId, (value) => {
          ZSAutopilotLedger.transition(value, msg.taskId, msg.nextState, msg.payload || {});
          ZSAutopilotLedger.refreshReady(value);
          return value;
        });
        sendResponse({ ok: true, run });
        break;
      }
      case "autopilot_finalize_run": {
        const run = await mutateAutopilotRun(msg.runId, (value) => {
          ZSAutopilotLedger.refreshReady(value);
          ZSAutopilotLedger.finalize(value);
          return value;
        });
        sendResponse({ ok: true, run });
        break;
      }
      case "autopilot_cancel_run": {
        const run = await mutateAutopilotRun(msg.runId, (value) => ZSAutopilotLedger.cancelRun(value, msg.reason));
        const tabs = await new Promise((resolve) => chrome.tabs.query({ url: PROVIDER_URLS }, resolve));
        for (const tab of tabs) {
          if (!tab.id) continue;
          try {
            chrome.tabs.sendMessage(tab.id, { type: "zs-autopilot-worker-cancel", runId: msg.runId }).catch(() => {});
          } catch {}
        }
        sendResponse({ ok: true, run });
        break;
      }
      case "status":
        if (!connected) connect(); // self-heal after a worker wake-up
        sendResponse(statusObj());
        break;
      case "list_tools": {
        // Prefer a live refresh; fall back to cache so the loop never stalls.
        // 10s, not 25s: a catalogue request only blocks this long when one of the
        // MCP servers is dead (typically Roblox in a degraded, Blender-only
        // session), and in that exact case we already hold a perfectly good cached
        // catalogue. Waiting the full 25s just froze the boot for no new data.
        const r = await send({ type: "list_tools" }, 10000);
        if (r.ok) sendResponse({ ok: true, tools: r.tools });
        else sendResponse({ ok: toolsCache.length > 0, tools: toolsCache, error: r.error });
        break;
      }
      case "call_tool": {
        const timeout = (msg.timeout || 120000) + 10000;
        const isWorkspaceTool = String(msg.name || "").includes("workspace_");
        const r = isWorkspaceTool && msg.projectPath
          ? await callToolInProject(msg.projectPath, msg.name, msg.arguments, msg.timeout)
          : await send(
              { type: "call_tool", name: msg.name, arguments: msg.arguments, timeout: msg.timeout },
              timeout
            );
        sendResponse(r);
        break;
      }
      case "restart_mcp": {
        const r = await send({ type: "restart_mcp" }, 30000);
        sendResponse(r);
        break;
      }
      case "add_server": {
        const r = await send({
          type: "add_server", server_id: msg.server_id,
          command: msg.command, args: msg.args, env: msg.env,
        }, 15000);
        sendResponse(r);
        break;
      }
      case "remove_server": {
        const r = await send({ type: "remove_server", server_id: msg.server_id }, 15000);
        sendResponse(r);
        break;
      }
      case "reconnect":
        reconnectDelay = RECONNECT_MIN;
        connect();
        sendResponse({ ok: true });
        break;
      default:
        sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true; // async sendResponse
});

// Wake/keepalive hooks.
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

connect();
