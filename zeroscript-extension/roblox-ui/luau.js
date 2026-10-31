// SPDX-License-Identifier: GPL-3.0-or-later
// Roblox UI Workspace - generates Roblox-compatible Luau from a UI document.
// Exposes globalThis.ZSRUI.luau. Pure functions: no DOM, no side effects.
(function (global) {
  "use strict";
  const model = (global.ZSRUI && global.ZSRUI.model) || (typeof require === "function" ? require("./model.js") : null);

  // Luau numeric literal formatting: trim floating point noise.
  function num(n) {
    if (typeof n !== "number" || !isFinite(n)) return "0";
    const rounded = Math.round(n * 10000) / 10000;
    return String(rounded);
  }

  function color3(hex) {
    const m = /^#([0-9a-fA-F]{6})$/.exec(hex || "");
    if (!m) return "Color3.fromRGB(0, 0, 0)";
    const v = parseInt(m[1], 16);
    return "Color3.fromRGB(" + ((v >> 16) & 255) + ", " + ((v >> 8) & 255) + ", " + (v & 255) + ")";
  }

  function udim(u) { return "UDim.new(" + num(u.scale) + ", " + num(u.offset) + ")"; }
  function udim2(v) { return "UDim2.new(" + num(v.x.scale) + ", " + num(v.x.offset) + ", " + num(v.y.scale) + ", " + num(v.y.offset) + ")"; }
  function vector2(v) { return "Vector2.new(" + num(v.x) + ", " + num(v.y) + ")"; }

  function literal(type, value, key) {
    switch (type) {
      case "boolean": return value ? "true" : "false";
      case "number": return num(value);
      case "string": return JSON.stringify(String(value));
      case "assetid": return JSON.stringify(String(value || ""));
      case "color3": return color3(value);
      case "udim": return udim(value);
      case "udim2": return udim2(value);
      case "vector2": return vector2(value);
      case "enum":
        // Enum prefix depends on the property family; handled per-property below.
        return JSON.stringify(value);
      default: return "nil";
    }
  }

  // Enums need the correct Enum.<Type>.<Value>. Map property -> enum family.
  const ENUM_FAMILY = {
    ZIndexBehavior: "ZIndexBehavior",
    AutomaticCanvasSize: "AutomaticSize",
    ScrollingDirection: "ScrollingDirection",
    TextXAlignment: "TextXAlignment",
    TextYAlignment: "TextYAlignment",
    ApplyStrokeMode: "ApplyStrokeMode",
    FillDirection: "FillDirection",
    HorizontalAlignment: "HorizontalAlignment",
    VerticalAlignment: "VerticalAlignment",
    SortOrder: "SortOrder",
    DominantAxis: "DominantAxis",
    AspectType: "AspectRatioConstraintType",
    ScaleType: "ScaleType",
  };

  // UIStroke/Font handling that is not a plain Enum assignment.
  function enumLiteral(key, value) {
    if (key === "Font") return "Enum.Font." + value;
    const family = ENUM_FAMILY[key] || key;
    return "Enum." + family + "." + value;
  }

  // Properties that must be applied AFTER children are parented (layout/constraints
  // and gradients read their siblings). Applying them last avoids transient errors.
  const DEFERRED = new Set(["UIAspectRatioConstraint", "UISizeConstraint", "UITextSizeConstraint", "UIGridLayout", "UIListLayout"]);

  // Build a property-literal line for a node. Returns array of `inst.PROP = X` lines.
  function propLines(node, varName) {
    const schema = model.SCHEMA[node.class];
    const lines = [];
    for (const [key, def] of Object.entries(schema.props)) {
      const value = node.props[key];
      let lit;
      if (def.type === "enum") lit = enumLiteral(key, value);
      else lit = literal(def.type, value, key);
      lines.push(varName + "." + key + " = " + lit);
    }
    return lines;
  }

  // Special: UIGradient uses ColorSequence/NumberSequence, not two props.
  function gradientLines(node, varName) {
    const a = color3(node.props.ColorA);
    const b = color3(node.props.ColorB);
    return [
      varName + ".Color = ColorSequence.new(" + a + ", " + b + ")",
      varName + ".Rotation = " + num(node.props.Rotation),
      varName + ".Transparency = NumberSequence.new(" + num(node.props.TransparencyA) + ", " + num(node.props.TransparencyB) + ")",
    ];
  }

  function safeVar(node) {
    const base = String(node.name || node.class).replace(/[^A-Za-z0-9_]/g, "_");
    return (base && /^[A-Za-z_]/.test(base) ? base : "_") + "_" + String(node.id).replace(/[^A-Za-z0-9]/g, "").slice(-6);
  }

  // Emit Luau for one node (and subtree). Deferred props are collected into
  // a post-pass so constraints/layouts see siblings.
  function emitNode(node, parentVar, indent, deferred) {
    const pad = "  ".repeat(indent);
    const v = safeVar(node);
    const lines = [];
    lines.push(pad + "local " + v + " = Instance.new(" + JSON.stringify(node.class) + ")");
    lines.push(pad + v + ".Name = " + JSON.stringify(String(node.name)));
    if (node.class === "UIGradient") {
      for (const l of gradientLines(node, v)) lines.push(pad + l);
    } else {
      for (const l of propLines(node, v)) lines.push(pad + l);
    }
    if (DEFERRED.has(node.class)) deferred.push({ var: v, lines: propLines(node, v) });
    lines.push(pad + v + ".Parent = " + parentVar);
    // Children first? No: properties set, then parent, then recurse into children
    // so a child sees a parented parent (layout objects behave correctly).
    for (const child of (node.children || [])) {
      lines.push(...emitNode(child, v, indent, deferred));
    }
    return lines;
  }

  // Generate a module that builds one screen under StarterGui. Returns Luau text.
  function generateScreen(screen, tokens) {
    const lines = [];
    lines.push("-- Generated by ZeroScript Roblox UI Workspace");
    lines.push("-- Screen: " + String(screen.name));
    lines.push("-- This module builds real instances. No runtime styling loops.");
    lines.push("local Players = game:GetService(\"Players\")");
    lines.push("local StarterGui = game:GetService(\"StarterGui\")");
    lines.push("");
    lines.push("local function build(parent)");
    const deferred = [];
    const root = screen.root;
    const rootVar = safeVar(root);
    const pad = "  ";
    lines.push(pad + "local " + rootVar + " = Instance.new(\"ScreenGui\")");
    lines.push(pad + rootVar + ".Name = " + JSON.stringify(String(root.name)));
    for (const l of propLines(root, rootVar)) lines.push(pad + l);
    lines.push(pad + rootVar + ".Parent = parent");
    for (const child of (root.children || [])) {
      lines.push(...emitNode(child, rootVar, 1, deferred));
    }
    if (deferred.length) {
      lines.push(pad + "-- Deferred layout/constraint properties (applied after parenting)");
      for (const d of deferred) for (const l of d.lines) lines.push(pad + l);
    }
    lines.push(pad + "return " + rootVar);
    lines.push("end");
    lines.push("");
    lines.push("return build");
    return lines.join("\n") + "\n";
  }

  // Generate a combined module that builds every screen in the document.
  function generateDocument(doc) {
    const parts = [];
    parts.push("-- Generated by ZeroScript Roblox UI Workspace");
    parts.push("-- Document: " + String(doc.name));
    parts.push("local StarterGui = game:GetService(\"StarterGui\")");
    parts.push("");
    const builders = [];
    doc.screens.forEach((screen, index) => {
      const fn = "buildScreen" + index;
      const mod = generateScreen(screen, doc.tokens).split("\n");
      // Strip the leading comment header + return line, keep the build function body
      const start = mod.findIndex((l) => l.startsWith("local function build"));
      const end = mod.indexOf("end", mod.findIndex((l) => l.startsWith("return build")));
      const body = mod.slice(start, end).filter((l) => l.trim() !== "return build");
      parts.push("local function " + fn + "(parent)");
      parts.push(body.slice(1).join("\n")); // drop the `local function build(parent)` line
      parts.push("end");
      parts.push("");
      builders.push(fn);
    });
    parts.push("local function buildAll()");
    builders.forEach((fn) => parts.push("  " + fn + "(StarterGui)"));
    parts.push("end");
    parts.push("");
    parts.push("return { buildAll = buildAll }");
    return parts.join("\n") + "\n";
  }

  // Produce a flat instance description list used for Studio apply via MCP tools.
  function flattenForStudio(screen) {
    const out = [];
    function walk(node, parentName) {
      out.push({ className: node.class, name: node.name, parent: parentName, props: node.props });
      for (const child of (node.children || [])) walk(child, node.name);
    }
    walk(screen.root, "StarterGui");
    return out;
  }

  const API = { generateScreen, generateDocument, flattenForStudio, color3, udim2, safeVar };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  global.ZSRUI = global.ZSRUI || {};
  Object.assign(global.ZSRUI, { luau: API });
})(typeof globalThis !== "undefined" ? globalThis : this);
