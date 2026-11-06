// SPDX-License-Identifier: GPL-3.0-or-later
// Protocol-2 tests for background.js (run: node background.test.cjs). Not shipped.
// background.js is a service worker, so it is evaluated in a vm context against
// a fake chrome.* / WebSocket - enough to drive the real onMessage listener and
// the real handleBridgeMessage, no browser needed.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function makeHarness(opts = {}) {
  const sockets = [];
  const store = Object.assign({}, opts.storage || {});
  const tabMessages = []; // {tabId, message}
  const runtimeMessages = [];
  const timers = [];
  let listener = null;
  class FakeWebSocket {
    constructor(url) {
      this.url = url; this.sent = []; this.readyState = 0;
      sockets.push(this);
    }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
    open() { this.readyState = 1; this.onopen(); }
    receive(obj) { this.onmessage({ data: JSON.stringify(obj) }); }
  }
  FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1; FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;
  const ledgerSrc = fs.readFileSync(path.join(__dirname, "core/autopilot-ledger.js"), "utf8");
  const chrome = {
    storage: { local: {
      get: async (defaults) => {
        const out = Object.assign({}, defaults);
        for (const k of Object.keys(defaults)) if (k in store) out[k] = store[k];
        return out;
      },
      set: async (values) => { Object.assign(store, values); },
    } },
    runtime: {
      onMessage: { addListener: (fn) => { listener = fn; } },
      onStartup: { addListener() {} }, onInstalled: { addListener() {} },
      sendMessage: (message) => { runtimeMessages.push(message); return Promise.resolve(); },
    },
    tabs: {
      query: (_q, cb) => cb(opts.tabs || []),
      sendMessage: (tabId, message) => { tabMessages.push({ tabId, message }); return Promise.resolve(); },
    },
  };
  const context = vm.createContext({
    chrome, WebSocket: FakeWebSocket, console: { log() {}, warn() {}, error() {} },
    crypto: { randomUUID: () => "11111111-2222-3333-4444-555555555555" },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    importScripts() { vm.runInContext(ledgerSrc, context); },
    Date, JSON, Promise, Map, Set, Math, Object, Array, String, Number, Error, RegExp, Boolean,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "background.js"), "utf8"), context, { filename: "background.js" });
  const tick = () => new Promise((r) => setImmediate(r));
  function call(msg, sender) {
    return new Promise((resolve) => listener(msg, sender || {}, resolve));
  }
  return { sockets, store, tabMessages, runtimeMessages, timers, tick, call, context };
}

async function openSocket(h) {
  await h.tick(); await h.tick();
  const ws = h.sockets[h.sockets.length - 1];
  assert.ok(ws, "background.js opened a WebSocket");
  ws.open();
  return ws;
}

(async () => {
  // ── register_client carries protocol 2 + a stable stored connection id ──
  {
    const h = makeHarness();
    const ws = await openSocket(h);
    const reg = ws.sent[0];
    assert.equal(reg.type, "register_client");
    assert.equal(reg.client, "browser");
    assert.equal(reg.protocol_version, 2);
    assert.equal(reg.session_id, "browser-11111111-2222-3333-4444-555555555555");
    assert.equal(h.store.zsConnectionId, reg.session_id, "id persisted to chrome.storage.local");
    console.log("PASS  register_client has protocol_version 2 and stored session id");

    const h2 = makeHarness({ storage: { zsConnectionId: "browser-abc-123" } });
    const ws2 = await openSocket(h2);
    assert.equal(ws2.sent[0].session_id, "browser-abc-123", "stored id is reused, not regenerated");
    console.log("PASS  existing connection id is reused");
  }

  // ── call_tool carries per-tab session_id from sender.tab.id ─────────────
  {
    const h = makeHarness();
    const ws = await openSocket(h);
    ws.receive({ type: "client_registered", ok: true, session_id: ws.sent[0].session_id, protocol_version: 2 });
    const answer = (n, kind) => {
      const frame = ws.sent[ws.sent.length - 1];
      ws.receive(kind
        ? { type: "tool_result", id: frame.id, ok: false, kind, error: kind + " msg" }
        : { type: "tool_result", id: frame.id, ok: true, text: "ok" + n });
    };
    // content script lies about its session: must be ignored
    let p = h.call({ type: "call_tool", name: "workspace_read_file", arguments: { path: "a" }, timeout: 1000,
      projectPath: "C:\\proj", session_id: "browser-tab-999" }, { tab: { id: 7 } });
    await h.tick(); await h.tick();
    let frame = ws.sent[ws.sent.length - 1];
    assert.equal(frame.type, "call_tool");
    assert.equal(frame.session_id, "browser-tab-7");
    assert.equal(frame.workspace_root, "C:\\proj");
    answer(1); await p;
    p = h.call({ type: "call_tool", name: "roblox_thing", arguments: {}, timeout: 1000 }, { tab: { id: 8 } });
    await h.tick(); await h.tick();
    frame = ws.sent[ws.sent.length - 1];
    assert.equal(frame.session_id, "browser-tab-8");
    answer(2); await p;
    // popup (no tab) falls back to the connection session
    p = h.call({ type: "call_tool", name: "workspace_info", arguments: {}, timeout: 1000 }, {});
    await h.tick(); await h.tick();
    assert.equal(ws.sent[ws.sent.length - 1].session_id, ws.sent[0].session_id);
    answer(3); await p;
    console.log("PASS  call_tool carries session_id browser-tab-<sender.tab.id> (+ workspace_root)");

    // file_locked / busy come back as ordinary failed results with kind + message,
    // also on the workspace_root path (which used to throw and drop the reply)
    for (const kind of ["file_locked", "busy"]) {
      p = h.call({ type: "call_tool", name: "workspace_write_file", arguments: {}, timeout: 1000, projectPath: "C:\\p" }, { tab: { id: 7 } });
      await h.tick(); await h.tick();
      answer(0, kind);
      const r = await p;
      assert.equal(r.ok, false); assert.equal(r.kind, kind); assert.equal(r.error, kind + " msg");
    }
    console.log("PASS  file_locked / busy surface as tool errors with kind and message");
  }

  // ── handoff target pinned in the popup is applied only when armed ───────
  {
    const h = makeHarness({ storage: { zsHandoffTarget: "cli-1" } });
    const ws = await openSocket(h);
    ws.receive({ type: "agent_control", control: { version: 2, runs: [], sessions: [
      { session_id: "cli-1", role: "cli", armed: true }, { session_id: "cli-2", role: "cli", armed: true } ] } });
    let p = h.call({ type: "call_tool", name: "zeroscript_handoff_to_cli", arguments: { goal: "g" }, timeout: 1000 }, { tab: { id: 1 } });
    await h.tick(); await h.tick(); await h.tick();
    assert.equal(ws.sent[ws.sent.length - 1].arguments.target_session_id, "cli-1");
    ws.receive({ type: "tool_result", id: ws.sent[ws.sent.length - 1].id, ok: true, text: "x" }); await p;
    p = h.call({ type: "call_tool", name: "zeroscript_handoff_to_cli", arguments: { target_session_id: "cli-2" }, timeout: 1000 }, { tab: { id: 1 } });
    await h.tick(); await h.tick(); await h.tick();
    assert.equal(ws.sent[ws.sent.length - 1].arguments.target_session_id, "cli-2", "explicit target wins");
    ws.receive({ type: "tool_result", id: ws.sent[ws.sent.length - 1].id, ok: true, text: "x" }); await p;
    ws.receive({ type: "agent_control", control: { version: 2, runs: [], sessions: [{ session_id: "cli-1", role: "cli", armed: false }] } });
    p = h.call({ type: "call_tool", name: "zeroscript_handoff_to_cli", arguments: {}, timeout: 1000 }, { tab: { id: 1 } });
    await h.tick(); await h.tick(); await h.tick();
    assert.equal(ws.sent[ws.sent.length - 1].arguments.target_session_id, undefined, "disarmed pin is not sent");
    ws.receive({ type: "tool_result", id: ws.sent[ws.sent.length - 1].id, ok: true, text: "x" }); await p;
    console.log("PASS  handoff target_session_id: pinned if armed, explicit wins, automatic otherwise");
  }

  // ── agent_control is filtered per tab; popup gets everything ────────────
  {
    const h = makeHarness({ tabs: [{ id: 1 }, { id: 2 }] });
    const ws = await openSocket(h);
    const control = { version: 2,
      runs: [
        { run_id: "r1", origin_session: "browser-tab-1", target_session: "cli-1", state: "queued" },
        { run_id: "r2", origin_session: "browser-tab-2", target_session: "cli-1", state: "active" },
      ],
      sessions: [{ session_id: "cli-1", role: "cli", armed: true }, { session_id: "browser-tab-1", role: "browser", armed: false }] };
    h.tabMessages.length = 0; h.runtimeMessages.length = 0;
    ws.receive({ type: "agent_control", control });
    const forTab = (id) => h.tabMessages.filter((m) => m.tabId === id && m.message.type === "zs-agent-control").pop().message;
    const m1 = forTab(1), m2 = forTab(2);
    assert.deepEqual(m1.control.runs.map((r) => r.run_id), ["r1"]);
    assert.deepEqual(m2.control.runs.map((r) => r.run_id), ["r2"]);
    assert.equal(m1.session_id, "browser-tab-1");
    assert.deepEqual(m1.control.sessions.map((s) => s.session_id), ["cli-1"], "tabs only see CLI sessions");
    const popup = h.runtimeMessages.filter((m) => m.type === "zs-agent-control").pop();
    assert.equal(popup.control.runs.length, 2, "popup receives the full run list");
    // zs-status pushed to tabs carries the same per-tab view; popup status the full one
    const st1 = h.tabMessages.filter((m) => m.tabId === 1 && m.message.type === "zs-status").pop().message;
    assert.deepEqual(st1.agentControl.runs.map((r) => r.run_id), ["r1"]);
    assert.equal(h.runtimeMessages.filter((m) => m.type === "zs-status").pop().agentControl.runs.length, 2);
    const statusReply = await h.call({ type: "status" }, { tab: { id: 2 } });
    assert.deepEqual(statusReply.agentControl.runs.map((r) => r.run_id), ["r2"]);
    assert.equal((await h.call({ type: "status" }, {})).agentControl.runs.length, 2);
    console.log("PASS  agent_control filtered per tab (origin_session), full list for popup");

    // Stop: only this tab's held run gets agent_stop
    ws.sent.length = 0;
    const stop = await h.call({ type: "agent_stop" }, { tab: { id: 1 } });
    assert.equal(stop.sent, 1);
    assert.deepEqual(ws.sent.map((f) => [f.type, f.run_id, f.session_id]), [["agent_stop", "r1", "browser-tab-1"]]);
    console.log("PASS  agent_stop sent only for the stopping tab's own run");
  }

  // ── protocol_mismatch: surface message, stop calls, back off ────────────
  {
    const h = makeHarness({ tabs: [{ id: 3 }] });
    const ws = await openSocket(h);
    h.tabMessages.length = 0;
    ws.receive({ ok: false, type: "client_registered", error: "protocol_mismatch", expected: 2, message: "bridge needs protocol 2" });
    ws.close(); // bridge closes after replying
    const st = h.tabMessages.filter((m) => m.message.type === "zs-status").pop().message;
    assert.equal(st.protocolError, "Update ZeroScript: bridge needs protocol 2");
    assert.equal(st.connected, false);
    const r = await h.call({ type: "call_tool", name: "x", arguments: {}, timeout: 1000 }, { tab: { id: 3 } });
    assert.equal(r.ok, false); assert.equal(r.kind, "protocol_mismatch");
    assert.match(r.error, /^Update ZeroScript/);
    const delays = h.timers.filter((t) => t.ms >= 10000).map((t) => t.ms);
    assert.ok(delays.length >= 1 && delays[delays.length - 1] >= 15000, "reconnect is backed off, not 1-5s: " + delays);
    const before = h.sockets.length;
    await h.call({ type: "status" }, {}); await h.tick();
    assert.equal(h.sockets.length, before, "status polling does not reconnect during backoff");
    console.log("PASS  protocol_mismatch surfaces 'Update ZeroScript', blocks calls, backs off");
  }
  console.log("background.test.cjs: all passed");
})().catch((e) => { console.error("FAIL", e); process.exit(1); });
