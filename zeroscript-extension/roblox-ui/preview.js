// SPDX-License-Identifier: GPL-3.0-or-later
// Roblox UI Workspace - DOM preview renderer.
// Renders an approximate, editable DOM view of a ScreenGui tree. This is a
// design aid, NOT a Roblox renderer; Scale/Offset are approximated against a
// chosen viewport preset. Exposes globalThis.ZSRUI.preview.
(function (global) {
  "use strict";
  const model = (global.ZSRUI && global.ZSRUI.model) || (typeof require === "function" ? require("./model.js") : null);

  // Viewport presets: representative Roblox device sizes in CSS px.
  const PRESETS = {
    desktop: { w: 1280, h: 720, label: "Desktop 16:9" },
    laptop: { w: 1366, h: 768, label: "Laptop" },
    tablet: { w: 1024, h: 768, label: "Tablet" },
    phonePortrait: { w: 390, h: 844, label: "Phone Portrait" },
    phoneLandscape: { w: 844, h: 390, label: "Phone Landscape" },
  };

  const FONT_MAP = {
    SourceSans: "'Segoe UI', system-ui, sans-serif",
    SourceSansBold: "600 'Segoe UI', system-ui, sans-serif",
    Gotham: "'Montserrat', system-ui, sans-serif",
    GothamBold: "700 'Montserrat', system-ui, sans-serif",
    GothamBlack: "800 'Montserrat', system-ui, sans-serif",
    Code: "'Cascadia Code', Consolas, monospace",
  };

  function resolveDim(v, parentSize) {
    const scale = v.scale || 0;
    const offset = v.offset || 0;
    return scale * parentSize + offset;
  }

  function resolvePos(v, parentSize) {
    // Roblox position: scale*parentSize + offset. CSS uses top/left with
    // anchor adjustment handled at element level.
    return resolveDim(v, parentSize);
  }

  function anchorFractions(v) {
    return { ax: v && typeof v.x === "object" ? v.x.scale + (v.x.offset || 0) / 1e6 : 0, ay: 0 };
  }

  // Build a DOM subtree. viewport = {w,h}. Returns an element.
  // ctx = { selectedId, onSelect } for interaction.
  function renderNode(node, parentSize, ctx) {
    const schema = model.SCHEMA[node.class];
    const el = document.createElement("div");
    el.className = "zr-node zr-" + node.class.toLowerCase();
    el.dataset.nodeId = node.id;
    el.dataset.class = node.class;
    if (ctx.selectedId === node.id) el.classList.add("zr-selected");
    const p = node.props;

    if (schema.category === "modifier") {
      // Modifiers are not laid out as boxes; UIGradient handled on parent below.
      return null;
    }

    const parentW = parentSize.w;
    const parentH = parentSize.h;

    if (node.class === "ScreenGui") {
      el.className += " zr-screengui";
      el.style.position = "absolute";
      el.style.inset = "0";
      el.style.overflow = "hidden";
      el.style.background = ctx.tokens.palette.bg;
      el.style.width = parentW + "px";
      el.style.height = parentH + "px";
      if (!p.Enabled) el.style.opacity = "0.3";
      renderChildren(node, { w: parentW, h: parentH }, ctx, el);
      return el;
    }

    // Layout box for drawable classes.
    el.style.position = "absolute";
    let w = null, h = null;
    if (p.Size) { w = resolveDim(p.Size.x, parentW); h = resolveDim(p.Size.y, parentH); }
    if (w != null) el.style.width = w + "px";
    if (h != null) el.style.height = h + "px";

    if (p.Position && p.AnchorPoint) {
      const px = resolvePos(p.Position.x, parentW);
      const py = resolvePos(p.Position.y, parentH);
      const ax = p.AnchorPoint.x.scale * (w != null ? w : parentW) + p.AnchorPoint.x.offset;
      const ay = p.AnchorPoint.y.scale * (h != null ? h : parentH) + p.AnchorPoint.y.offset;
      el.style.left = (px - ax) + "px";
      el.style.top = (py - ay) + "px";
    }

    // Background
    if (p.BackgroundColor3) el.style.background = p.BackgroundColor3;
    if (p.BackgroundTransparency != null) {
      el.style.opacity = String(1 - p.BackgroundTransparency);
    }
    if (!p.Visible) el.style.display = "none";

    // Text
    if (node.class === "TextLabel" || node.class === "TextButton" || node.class === "TextBox") {
      el.textContent = node.class === "TextBox" ? (p.Text || p.PlaceholderText || "") : (p.Text || "");
      el.style.color = p.TextColor3;
      el.style.display = "flex";
      el.style.alignItems = p.TextYAlignment === "Top" ? "flex-start" : p.TextYAlignment === "Bottom" ? "flex-end" : "center";
      el.style.justifyContent = p.TextXAlignment === "Left" ? "flex-start" : p.TextXAlignment === "Right" ? "flex-end" : "center";
      el.style.fontFamily = FONT_MAP[p.Font] || FONT_MAP.SourceSans;
      el.style.overflow = "hidden";
      if (p.TextWrapped) el.style.whiteSpace = "normal";
      else el.style.whiteSpace = "nowrap";
      el.style.fontSize = (p.TextSize || 18) + "px";
      if (node.class === "TextBox") el.style.padding = "4px 8px";
    }

    // Image
    if (node.class === "ImageLabel" || node.class === "ImageButton") {
      if (p.Image) {
        const img = document.createElement("img");
        img.src = p.Image;
        img.style.width = "100%"; img.style.height = "100%";
        img.style.objectFit = p.ScaleType === "Fit" ? "contain" : p.ScaleType === "Crop" ? "cover" : "fill";
        img.style.opacity = String(1 - (p.ImageTransparency || 0));
        img.style.pointerEvents = "none";
        el.appendChild(img);
      } else {
        el.classList.add("zr-image-placeholder");
      }
    }

    // Modifiers affecting the box itself.
    const mods = (node.children || []).filter((c) => model.SCHEMA[c.class] && model.SCHEMA[c.class].category === "modifier");
    for (const mod of mods) {
      applyModifier(el, mod, { w, h });
    }

    if (p.ClipsDescendants) el.style.overflow = "hidden";

    // Child drawables.
    renderChildren(node, { w: w != null ? w : parentW, h: h != null ? h : parentH }, ctx, el);

    // Click to select.
    el.addEventListener("pointerdown", (ev) => {
      ev.stopPropagation();
      if (ctx.onSelect) ctx.onSelect(node.id);
    }, true);

    return el;
  }

  function renderChildren(node, size, ctx, host) {
    const drawable = (node.children || []).filter((c) => model.SCHEMA[c.class] && model.SCHEMA[c.class].category !== "modifier");
    // Honor UIListLayout / UIGridLayout for ordering/flowing.
    const layout = (node.children || []).find((c) => c.class === "UIListLayout" || c.class === "UIGridLayout");
    const sorted = sortChildren(drawable, node);
    if (layout) host.classList.add("zr-has-layout");
    for (const child of sorted) {
      const childEl = renderNode(child, size, ctx);
      if (childEl) host.appendChild(childEl);
    }
  }

  function sortChildren(children, node) {
    const layout = (node.children || []).find((c) => c.class === "UIListLayout");
    if (layout && layout.props.SortOrder === "Name") {
      return children.slice().sort((a, b) => a.name.localeCompare(b.name));
    }
    return children;
  }

  function applyModifier(el, mod, box) {
    const p = mod.props;
    if (mod.class === "UICorner") {
      const base = Math.min(box.w || 0, box.h || 0) || 20;
      const r = p.CornerRadius.scale * base + p.CornerRadius.offset;
      el.style.borderRadius = r + "px";
      el.style.overflow = "hidden";
    } else if (mod.class === "UIStroke") {
      el.style.boxShadow = "inset 0 0 0 " + p.Thickness + "px " + p.Color;
    } else if (mod.class === "UIGradient") {
      el.style.backgroundImage = "linear-gradient(" + (p.Rotation === 90 ? "180deg" : p.Rotation === 0 ? "90deg" : p.Rotation + "deg") + ", " + p.ColorA + ", " + p.ColorB + ")";
    }
  }

  // Render the whole selected screen into a host element at a given preset.
  function renderScreen(host, screen, tokens, presetName, selectedId, onSelect) {
    const preset = PRESETS[presetName] || PRESETS.desktop;
    host.innerHTML = "";
    host.style.position = "relative";
    host.style.width = preset.w + "px";
    host.style.height = preset.h + "px";
    const ctx = { tokens, selectedId, onSelect };
    const tree = renderNode(screen.root, { w: preset.w, h: preset.h }, ctx);
    if (tree) host.appendChild(tree);
    else {
      const empty = document.createElement("div");
      empty.className = "zr-empty";
      empty.textContent = "This screen has no renderable root.";
      host.appendChild(empty);
    }
    return preset;
  }

  const API = { PRESETS, renderScreen, renderNode, resolveDim, FONT_MAP };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  global.ZSRUI = global.ZSRUI || {};
  Object.assign(global.ZSRUI, { preview: API });
})(typeof globalThis !== "undefined" ? globalThis : this);
