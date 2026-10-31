// SPDX-License-Identifier: GPL-3.0-or-later
// Roblox UI Workspace - controller. Mounts the full editing surface into a
// container, wires the model, preview, Luau generator, persistence and Studio
// apply. Exposes globalThis.ZSRUI.mount(container, opts).
(function (global) {
  "use strict";
  const model = global.ZSRUI.model;
  const preview = global.ZSRUI.preview;
  const luau = global.ZSRUI.luau;

  const DOC_DIR = ".zeroscript/roblox-ui";
  const DEFAULT_FILE = DOC_DIR + "/current.json";

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v != null) node.setAttribute(k, v);
    }
    for (const child of (children || [])) if (child != null) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    return node;
  }

  function mount(container, opts) {
    const callTool = (opts && opts.callTool) || (global.ZSRUI && global.ZSRUI.callTool);
    if (!callTool) throw new Error("Roblox UI Workspace needs a callTool function to reach the bridge.");
    const onStatus = (opts && opts.onStatus) || (() => {});

    const state = {
      doc: model.createDocument("Untitled UI"),
      activeScreenId: null,
      selectedId: null,
      preset: "desktop",
      dirty: false,
      filePath: DEFAULT_FILE,
      studioConnected: false,
      pending: [],
    };
    state.activeScreenId = state.doc.screens[0].id;
    state.selectedId = state.doc.screens[0].root.id;

    // ---- Shell ---------------------------------------------------------------
    container.classList.add("zr-workspace");
    container.innerHTML = "";

    const statusBar = el("div", { class: "zr-statusbar" }, [
      el("span", { class: "zr-status-file", text: "No document" }),
      el("span", { class: "zr-status-spacer" }),
      el("span", { class: "zr-status-studio", text: "Studio: unknown" }),
    ]);

    const toolbar = el("div", { class: "zr-toolbar" });
    const screenSelect = el("select", { class: "zr-select", title: "Active screen" });
    screenSelect.addEventListener("change", () => { state.activeScreenId = screenSelect.value; state.selectedId = activeScreen().root.id; renderAll(); });
    const presetSelect = el("select", { class: "zr-select", title: "Preview viewport" });
    for (const [key, p] of Object.entries(preview.PRESETS)) presetSelect.appendChild(el("option", { value: key, text: p.label }));
    presetSelect.value = state.preset;
    presetSelect.addEventListener("change", () => { state.preset = presetSelect.value; renderPreview(); });

    const newBtn = el("button", { class: "zr-btn", text: "New", title: "New UI document" });
    const saveBtn = el("button", { class: "zr-btn", text: "Save", title: "Save document to workspace" });
    const loadBtn = el("button", { class: "zr-btn", text: "Load", title: "Load document from workspace" });
    const luauBtn = el("button", { class: "zr-btn", text: "View Luau", title: "Generate Roblox Luau" });
    const applyBtn = el("button", { class: "zr-btn zr-btn-accent", text: "Apply to Studio", title: "Apply screen to the connected Roblox Studio" });
    const addScreenBtn = el("button", { class: "zr-btn", text: "+ Screen", title: "Add a screen" });

    toolbar.append(screenSelect, addScreenBtn, el("span", { class: "zr-toolbar-sep" }), presetSelect, newBtn, saveBtn, loadBtn, luauBtn, applyBtn);

    // Main three-column layout: tree | preview | inspector+output.
    const treePane = el("div", { class: "zr-tree-pane" });
    const previewPane = el("div", { class: "zr-preview-pane" });
    const inspectPane = el("div", { class: "zr-inspect-pane" });
    const main = el("div", { class: "zr-main" }, [treePane, previewPane, inspectPane]);

    // Prompt bar / contextual assistant.
    const promptInput = el("input", { class: "zr-prompt-input", placeholder: "Describe a UI or a change (e.g. 'add a shop panel with a title and close button')" });
    const promptBtn = el("button", { class: "zr-btn zr-btn-accent", text: "Assist" });
    const promptNote = el("span", { class: "zr-prompt-note", text: "" });
    const promptBar = el("div", { class: "zr-prompt-bar" }, [promptInput, promptBtn, promptNote]);

    container.append(toolbar, main, promptBar, statusBar);

    // ---- Helpers -------------------------------------------------------------
    function activeScreen() {
      return state.doc.screens.find((s) => s.id === state.activeScreenId) || state.doc.screens[0];
    }
    function markDirty() { state.dirty = true; updateStatus(); }
    function updateStatus() {
      statusBar.querySelector(".zr-status-file").textContent = (state.dirty ? "*" : "") + state.doc.name + (state.dirty ? " (unsaved)" : "");
      statusBar.querySelector(".zr-status-studio").textContent = "Studio: " + (state.studioConnected ? "connected" : "not connected");
    }

    // ---- Tree ----------------------------------------------------------------
    function renderTree() {
      treePane.innerHTML = "";
      treePane.appendChild(el("div", { class: "zr-pane-title", text: "Components" }));
      const host = el("div", { class: "zr-tree" });
      const screen = activeScreen();
      function row(node, depth) {
        const isMod = model.SCHEMA[node.class] && model.SCHEMA[node.class].category === "modifier";
        const rowEl = el("div", {
          class: "zr-tree-row" + (state.selectedId === node.id ? " active" : "") + (isMod ? " modifier" : ""),
          style: "padding-left:" + (8 + depth * 14) + "px",
          title: node.class,
        }, [
          el("span", { class: "zr-tree-class", text: node.class }),
          el("span", { class: "zr-tree-name", text: node.name }),
        ]);
        rowEl.addEventListener("click", () => { state.selectedId = node.id; renderTree(); renderInspector(); renderPreview(); });
        host.appendChild(rowEl);
        for (const child of (node.children || [])) row(child, depth + 1);
      }
      row(screen.root, 0);
      treePane.appendChild(host);

      // Add-component control.
      const addWrap = el("div", { class: "zr-add-wrap" });
      const addSel = el("select", { class: "zr-select" });
      const addable = model.SCREEN_CHILD_CLASSES.concat(model.MODIFIER_CLASSES);
      for (const cls of addable) addSel.appendChild(el("option", { value: cls, text: cls }));
      const addBtn = el("button", { class: "zr-btn", text: "+ Add" });
      addBtn.addEventListener("click", () => {
        try {
          const parentId = state.selectedId;
          const node = model.addNode(activeScreen(), parentId, addSel.value);
          state.selectedId = node.id;
          markDirty(); renderAll();
        } catch (err) { flash(err.message); }
      });
      addWrap.append(addSel, addBtn);
      treePane.appendChild(addWrap);

      // Component actions.
      const actions = el("div", { class: "zr-tree-actions" });
      const acts = [
        ["Rename", () => {
          const node = model.findNode(activeScreen().root, state.selectedId);
          if (!node) return;
          const name = global.prompt ? global.prompt("Rename component", node.name) : null;
          if (!name) return;
          try { model.renameNode(activeScreen(), state.selectedId, name); markDirty(); renderAll(); } catch (err) { flash(err.message); }
        }],
        ["Up", () => { try { model.reorderNode(activeScreen(), state.selectedId, "up"); markDirty(); renderAll(); } catch (err) { flash(err.message); } }],
        ["Down", () => { try { model.reorderNode(activeScreen(), state.selectedId, "down"); markDirty(); renderAll(); } catch (err) { flash(err.message); } }],
        ["Duplicate", () => { try { const c = model.duplicateNode(activeScreen(), state.selectedId); state.selectedId = c.id; markDirty(); renderAll(); } catch (err) { flash(err.message); } }],
        ["Delete", () => { try { model.removeNode(activeScreen(), state.selectedId); state.selectedId = activeScreen().root.id; markDirty(); renderAll(); } catch (err) { flash(err.message); } }],
      ];
      for (const [label, fn] of acts) actions.appendChild(el("button", { class: "zr-btn zr-btn-small", text: label, onclick: fn }));
      treePane.appendChild(actions);
    }

    // ---- Inspector -----------------------------------------------------------
    function renderInspector() {
      inspectPane.innerHTML = "";
      inspectPane.appendChild(el("div", { class: "zr-pane-title", text: "Properties" }));
      const node = model.findNode(activeScreen().root, state.selectedId);
      if (!node) { inspectPane.appendChild(el("div", { class: "zr-empty", text: "Select a component." })); return; }
      const schema = model.SCHEMA[node.class];
      const header = el("div", { class: "zr-inspect-header" }, [
        el("strong", { text: node.class }),
        el("span", { class: "zr-muted", text: node.name }),
      ]);
      inspectPane.appendChild(header);
      const form = el("div", { class: "zr-props" });
      for (const [key, def] of Object.entries(schema.props)) {
        form.appendChild(propField(node, key, def));
      }
      inspectPane.appendChild(form);
    }

    function propField(node, key, def) {
      const value = node.props[key];
      const wrap = el("label", { class: "zr-prop" }, [el("span", { class: "zr-prop-name", text: key })]);
      let input;
      const commit = (v) => { try { model.setProp(activeScreen(), node.id, key, v); markDirty(); renderPreview(); } catch (err) { flash(err.message); } };
      if (def.type === "boolean") {
        input = el("input", { type: "checkbox" }); input.checked = !!value;
        input.addEventListener("change", () => commit(input.checked));
      } else if (def.type === "number") {
        input = el("input", { type: "number", step: def.step || 1, min: def.min, max: def.max }); input.value = value;
        input.addEventListener("change", () => commit(Number(input.value)));
      } else if (def.type === "enum") {
        input = el("select", { class: "zr-select" });
        for (const v of def.values) input.appendChild(el("option", { value: v, text: v }));
        input.value = value; input.addEventListener("change", () => commit(input.value));
      } else if (def.type === "color3") {
        input = el("input", { type: "color" }); input.value = value; input.addEventListener("input", () => commit(input.value));
      } else if (def.type === "string" || def.type === "assetid") {
        input = el("input", { type: "text" }); input.value = value; input.placeholder = def.type === "assetid" ? "rbxassetid://0" : "";
        input.addEventListener("change", () => commit(input.value));
      } else if (def.type === "udim") {
        input = udimInput(value, (v) => commit(v));
      } else if (def.type === "udim2") {
        input = udim2Input(value, (v) => commit(v));
      } else if (def.type === "vector2") {
        input = vec2Input(value, (v) => commit(v));
      } else {
        input = el("span", { class: "zr-muted", text: "(unsupported)" });
      }
      wrap.appendChild(input);
      return wrap;
    }

    function udimInput(value, onChange) {
      const wrap = el("span", { class: "zr-udim" });
      const s = el("input", { type: "number", step: "0.01", title: "scale" }); s.value = value.scale;
      const o = el("input", { type: "number", step: "1", title: "offset" }); o.value = value.offset;
      const update = () => onChange({ scale: Number(s.value), offset: Number(o.value) });
      s.addEventListener("change", update); o.addEventListener("change", update);
      wrap.append(s, o);
      return wrap;
    }
    function udim2Input(value, onChange) {
      const wrap = el("span", { class: "zr-udim2" });
      const parts = [];
      const make = (label, sub) => {
        const s = el("input", { type: "number", step: "0.01", title: label + " scale" }); s.value = sub.scale;
        const o = el("input", { type: "number", step: "1", title: label + " offset" }); o.value = sub.offset;
        return { s, o, label };
      };
      const x = make("x", value.x); const y = make("y", value.y);
      const update = () => onChange({ x: { scale: Number(x.s.value), offset: Number(x.o.value) }, y: { scale: Number(y.s.value), offset: Number(y.o.value) } });
      for (const p of [x, y]) { p.s.addEventListener("change", update); p.o.addEventListener("change", update); }
      wrap.append(el("span", { class: "zr-muted", text: "x" }), x.s, x.o, el("span", { class: "zr-muted", text: "y" }), y.s, y.o);
      return wrap;
    }
    function vec2Input(value, onChange) {
      const wrap = el("span", { class: "zr-udim" });
      const x = el("input", { type: "number" }); x.value = value.x;
      const y = el("input", { type: "number" }); y.value = value.y;
      const update = () => onChange({ x: Number(x.value), y: Number(y.value) });
      x.addEventListener("change", update); y.addEventListener("change", update);
      wrap.append(el("span", { class: "zr-muted", text: "x" }), x, el("span", { class: "zr-muted", text: "y" }), y);
      return wrap;
    }

    // ---- Preview -------------------------------------------------------------
    function renderPreview() {
      previewPane.innerHTML = "";
      previewPane.appendChild(el("div", { class: "zr-pane-title" }, [el("span", { text: "Preview" }), el("span", { class: "zr-muted", text: " (browser approximation, not Roblox rendering)" })]));
      const stage = el("div", { class: "zr-stage" });
      const canvas = el("div", { class: "zr-canvas" });
      stage.appendChild(canvas);
      previewPane.appendChild(stage);
      try {
        preview.renderScreen(canvas, activeScreen(), state.doc.tokens, state.preset, state.selectedId, (id) => { state.selectedId = id; renderTree(); renderInspector(); renderPreview(); });
      } catch (err) {
        canvas.appendChild(el("div", { class: "zr-error", text: "Preview error: " + err.message }));
      }
    }

    function renderAll() {
      // Keep screen selector in sync.
      screenSelect.innerHTML = "";
      for (const s of state.doc.screens) screenSelect.appendChild(el("option", { value: s.id, text: s.name }));
      screenSelect.value = state.activeScreenId;
      renderTree(); renderInspector(); renderPreview(); updateStatus();
    }

    let flashTimer = null;
    function flash(message) {
      promptNote.textContent = message;
      promptNote.classList.add("zr-flash-error");
      if (flashTimer) clearTimeout(flashTimer);
      flashTimer = setTimeout(() => { promptNote.textContent = ""; promptNote.classList.remove("zr-flash-error"); }, 4000);
    }

    // ---- Persistence ---------------------------------------------------------
    async function saveDoc() {
      const text = model.serializeDocument(state.doc);
      try {
        await callTool("workspace_write_file", { path: state.filePath, content: text });
        state.dirty = false; updateStatus();
        promptNote.textContent = "Saved to " + state.filePath;
      } catch (err) { flash("Save failed: " + err.message); }
    }
    async function loadDoc() {
      try {
        const res = await callTool("workspace_read_file", { path: state.filePath });
        state.doc = model.parseDocument(res.content);
        state.activeScreenId = state.doc.screens[0].id;
        state.selectedId = state.doc.screens[0].root.id;
        state.dirty = false; renderAll();
        promptNote.textContent = "Loaded " + state.filePath;
      } catch (err) { flash("Load failed: " + err.message); }
    }
    function newDoc() {
      if (state.dirty && global.confirm && !global.confirm("Discard unsaved changes?")) return;
      state.doc = model.createDocument("Untitled UI");
      state.activeScreenId = state.doc.screens[0].id;
      state.selectedId = state.doc.screens[0].root.id;
      state.dirty = false; renderAll();
    }

    // ---- Luau output ---------------------------------------------------------
    function showLuau() {
      const code = luau.generateScreen(activeScreen(), state.doc.tokens);
      inspectPane.innerHTML = "";
      inspectPane.appendChild(el("div", { class: "zr-pane-title", text: "Generated Luau" }));
      const actions = el("div", { class: "zr-tree-actions" }, [
        el("button", { class: "zr-btn zr-btn-small", text: "Back to Properties", onclick: renderInspector }),
        el("button", { class: "zr-btn zr-btn-small", text: "Copy", onclick: () => { if (global.navigator && navigator.clipboard) navigator.clipboard.writeText(code); } }),
      ]);
      const pre = el("pre", { class: "zr-luau" }, [el("code", { text: code })]);
      inspectPane.append(actions, pre);
    }

    // ---- Studio apply --------------------------------------------------------
    async function applyToStudio() {
      const screen = activeScreen();
      const screenName = screen.root.name;
      const flat = luau.flattenForStudio(screen);
      promptNote.textContent = "Applying " + flat.length + " instances…";
      try {
        const result = await callTool("roblox/execute_luau", {
          code: buildApplyLuau(screen),
          datamodel_type: "Edit",
        }, 60000);
        state.studioConnected = true;
        promptNote.textContent = "Applied " + screenName + ". Result: " + (typeof result === "string" ? result.slice(0, 120) : JSON.stringify(result).slice(0, 120));
        updateStatus();
      } catch (err) {
        state.studioConnected = false; updateStatus();
        flash("Studio apply failed (design preserved): " + err.message);
        // Record that this design has NOT been implemented.
        state.pending.push({ screen: screenName, error: String(err.message), at: Date.now() });
      }
    }

    // Build Luau that creates instances directly (no runtime styling loops, no
    // leftover scripts). The generated module is executed once through the
    // Studio MCP execute path; it does not leave a Script in the place.
    function buildApplyLuau(screen) {
      const moduleSrc = luau.generateScreen(screen, state.doc.tokens);
      // Wrap as a self-executing builder: replace the trailing `return build`
      // with a call, and parent under StarterGui.
      const body = moduleSrc
        .replace(/^-- Generated by[\s\S]*?local StarterGui = game:GetService\("StarterGui"\)\n/, "")
        .replace(/\nreturn build\s*$/, "\nlocal root = build(StarterGui)\nreturn root:GetFullName()\n");
      return body;
    }

    // ---- AI assistant (prompt bar) -------------------------------------------
    // Applies simple, explicit structural commands locally. For open-ended
    // natural-language design it delegates to the connected agent through the
    // skill; here we implement deterministic, verifiable operations.
    function assist() {
      const text = (promptInput.value || "").trim();
      if (!text) return;
      const lower = text.toLowerCase();
      try {
        if (/^add (a )?(frame|panel)/.test(lower)) {
          const node = model.addNode(activeScreen(), state.selectedId, "Frame", "Panel");
          state.selectedId = node.id; markDirty(); renderAll();
        } else if (/^add (a )?(button)/.test(lower)) {
          const node = model.addNode(activeScreen(), state.selectedId, "TextButton", "Button");
          state.selectedId = node.id; markDirty(); renderAll();
        } else if (/^add (a )?(label|text)/.test(lower)) {
          const node = model.addNode(activeScreen(), state.selectedId, "TextLabel", "Label");
          state.selectedId = node.id; markDirty(); renderAll();
        } else if (/corner/.test(lower)) {
          const node = model.addNode(activeScreen(), state.selectedId, "UICorner");
          markDirty(); renderAll();
        } else {
          promptNote.textContent = "I can add frames, buttons, labels, and corners. For full natural-language design, use the roblox-ui-expert skill with the agent.";
          return;
        }
        promptInput.value = "";
        promptNote.textContent = "Applied.";
      } catch (err) { flash(err.message); }
    }

    // ---- Wiring --------------------------------------------------------------
    newBtn.addEventListener("click", newDoc);
    saveBtn.addEventListener("click", saveDoc);
    loadBtn.addEventListener("click", loadDoc);
    luauBtn.addEventListener("click", showLuau);
    applyBtn.addEventListener("click", applyToStudio);
    addScreenBtn.addEventListener("click", () => {
      const screen = model.createScreen("Screen" + (state.doc.screens.length + 1));
      state.doc.screens.push(screen);
      state.activeScreenId = screen.id; state.selectedId = screen.root.id;
      markDirty(); renderAll();
    });
    promptBtn.addEventListener("click", assist);
    promptInput.addEventListener("keydown", (e) => { if (e.key === "Enter") assist(); });

    renderAll();

    // Expose for tests / debugging.
    return { state, renderAll, saveDoc, loadDoc, assist, buildApplyLuau, applyToStudio };
  }

  global.ZSRUI = global.ZSRUI || {};
  global.ZSRUI.mount = mount;
  if (typeof module !== "undefined" && module.exports) module.exports = { mount };
})(typeof globalThis !== "undefined" ? globalThis : this);
