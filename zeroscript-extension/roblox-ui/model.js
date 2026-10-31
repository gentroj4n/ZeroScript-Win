// SPDX-License-Identifier: GPL-3.0-or-later
// Roblox UI Workspace - component model, schema, validation and document CRUD.
// Loaded as a classic script; exposes globalThis.ZSRUI.model.
(function (global) {
  "use strict";

  const UDIM2 = (xs, xo, ys, yo) => ({ x: { scale: xs, offset: xo }, y: { scale: ys, offset: yo } });

  const SCHEMA = {
    ScreenGui: {
      category: "screen", canHaveChildren: true,
      props: {
        ResetOnSpawn: { type: "boolean", default: false },
        ZIndexBehavior: { type: "enum", values: ["Sibling", "Global"], default: "Sibling" },
        IgnoreGuiInset: { type: "boolean", default: false },
        DisplayOrder: { type: "number", min: -100, max: 100, step: 1, default: 1 },
        Enabled: { type: "boolean", default: true },
      },
    },
    Frame: {
      category: "container", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.4, 0, 0.3, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#181B22" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        BorderSizePixel: { type: "number", min: 0, max: 10, step: 1, default: 0 },
        ClipsDescendants: { type: "boolean", default: false },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    ScrollingFrame: {
      category: "container", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.6, 0, 0.5, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#181B22" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        BorderSizePixel: { type: "number", min: 0, max: 10, step: 1, default: 0 },
        CanvasSize: { type: "udim2", default: UDIM2(0, 0, 0, 0) },
        AutomaticCanvasSize: { type: "enum", values: ["None", "X", "Y", "XY"], default: "Y" },
        ScrollingDirection: { type: "enum", values: ["X", "Y", "XY"], default: "Y" },
        ScrollBarThickness: { type: "number", min: 0, max: 32, step: 1, default: 6 },
        ClipsDescendants: { type: "boolean", default: true },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    TextLabel: {
      category: "text", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.3, 0, 0.08, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#000000" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 1 },
        BorderSizePixel: { type: "number", min: 0, max: 10, step: 1, default: 0 },
        Text: { type: "string", default: "Text" },
        TextColor3: { type: "color3", default: "#F2F4F8" },
        TextSize: { type: "number", min: 1, max: 100, step: 1, default: 18 },
        TextScaled: { type: "boolean", default: false },
        TextWrapped: { type: "boolean", default: false },
        TextXAlignment: { type: "enum", values: ["Left", "Center", "Right"], default: "Center" },
        TextYAlignment: { type: "enum", values: ["Top", "Center", "Bottom"], default: "Center" },
        Font: { type: "enum", values: ["SourceSans", "SourceSansBold", "Gotham", "GothamBold", "GothamBlack", "Code"], default: "SourceSans" },
        RichText: { type: "boolean", default: false },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    TextButton: {
      category: "text", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.2, 0, 0.09, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#4C8DFF" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        BorderSizePixel: { type: "number", min: 0, max: 10, step: 1, default: 0 },
        Text: { type: "string", default: "Button" },
        TextColor3: { type: "color3", default: "#FFFFFF" },
        TextSize: { type: "number", min: 1, max: 100, step: 1, default: 18 },
        TextScaled: { type: "boolean", default: false },
        TextXAlignment: { type: "enum", values: ["Left", "Center", "Right"], default: "Center" },
        Font: { type: "enum", values: ["SourceSans", "SourceSansBold", "Gotham", "GothamBold", "GothamBlack", "Code"], default: "SourceSansBold" },
        AutoButtonColor: { type: "boolean", default: true },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    TextBox: {
      category: "text", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.3, 0, 0.07, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#1F232C" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        BorderSizePixel: { type: "number", min: 0, max: 10, step: 1, default: 0 },
        Text: { type: "string", default: "" },
        PlaceholderText: { type: "string", default: "Type here..." },
        TextColor3: { type: "color3", default: "#F2F4F8" },
        TextSize: { type: "number", min: 1, max: 100, step: 1, default: 16 },
        Font: { type: "enum", values: ["SourceSans", "SourceSansBold", "Gotham", "GothamBold", "Code"], default: "SourceSans" },
        ClearTextOnFocus: { type: "boolean", default: false },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    ImageLabel: {
      category: "image", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.2, 0, 0.2, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#000000" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 1 },
        Image: { type: "assetid", default: "" },
        ImageColor3: { type: "color3", default: "#FFFFFF" },
        ImageTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        ScaleType: { type: "enum", values: ["Stretch", "Slice", "Tile", "Fit", "Crop"], default: "Stretch" },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    ImageButton: {
      category: "image", canHaveChildren: true,
      props: {
        Size: { type: "udim2", default: UDIM2(0.12, 0, 0.12, 0) },
        Position: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        AnchorPoint: { type: "udim2", default: UDIM2(0.5, 0, 0.5, 0) },
        BackgroundColor3: { type: "color3", default: "#000000" },
        BackgroundTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 1 },
        Image: { type: "assetid", default: "" },
        ImageColor3: { type: "color3", default: "#FFFFFF" },
        ImageTransparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        ScaleType: { type: "enum", values: ["Stretch", "Slice", "Tile", "Fit", "Crop"], default: "Stretch" },
        Visible: { type: "boolean", default: true },
        ZIndex: { type: "number", min: 0, max: 50, step: 1, default: 1 },
      },
    },
    UICorner: {
      category: "modifier", canHaveChildren: false, singleton: "UICorner",
      props: { CornerRadius: { type: "udim", default: { scale: 0.08, offset: 0 } } },
    },
    UIStroke: {
      category: "modifier", canHaveChildren: false, singleton: "UIStroke",
      props: {
        Color: { type: "color3", default: "#2A2F3A" },
        Thickness: { type: "number", min: 0, max: 12, step: 0.5, default: 2 },
        Transparency: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        ApplyStrokeMode: { type: "enum", values: ["Contextual", "Border"], default: "Border" },
      },
    },
    UIPadding: {
      category: "modifier", canHaveChildren: false, singleton: "UIPadding",
      props: {
        PaddingTop: { type: "udim", default: { scale: 0.02, offset: 0 } },
        PaddingBottom: { type: "udim", default: { scale: 0.02, offset: 0 } },
        PaddingLeft: { type: "udim", default: { scale: 0.02, offset: 0 } },
        PaddingRight: { type: "udim", default: { scale: 0.02, offset: 0 } },
      },
    },
    UIListLayout: {
      category: "modifier", canHaveChildren: false, singleton: "UIListLayout",
      props: {
        FillDirection: { type: "enum", values: ["Horizontal", "Vertical"], default: "Vertical" },
        Padding: { type: "udim", default: { scale: 0, offset: 6 } },
        HorizontalAlignment: { type: "enum", values: ["Left", "Center", "Right"], default: "Left" },
        VerticalAlignment: { type: "enum", values: ["Top", "Center", "Bottom"], default: "Top" },
        SortOrder: { type: "enum", values: ["LayoutOrder", "Name", "ClassIndex"], default: "LayoutOrder" },
      },
    },
    UIGridLayout: {
      category: "modifier", canHaveChildren: false, singleton: "UIGridLayout",
      props: {
        CellSize: { type: "udim2", default: UDIM2(0.2, 0, 0.2, 0) },
        CellPadding: { type: "udim2", default: UDIM2(0.01, 0, 0.01, 0) },
        FillDirectionMaxCells: { type: "number", min: 0, max: 12, step: 1, default: 0 },
        FillDirection: { type: "enum", values: ["Horizontal", "Vertical"], default: "Horizontal" },
      },
    },
    UIAspectRatioConstraint: {
      category: "modifier", canHaveChildren: false, singleton: "UIAspectRatioConstraint",
      props: {
        AspectRatio: { type: "number", min: 0.1, max: 10, step: 0.01, default: 1 },
        DominantAxis: { type: "enum", values: ["Width", "Height"], default: "Width" },
        AspectType: { type: "enum", values: ["FitWithinMaxSize", "ScaleWithParentSize"], default: "FitWithinMaxSize" },
      },
    },
    UISizeConstraint: {
      category: "modifier", canHaveChildren: false, singleton: "UISizeConstraint",
      props: {
        MaxSize: { type: "vector2", default: { x: 1000, y: 1000 } },
        MinSize: { type: "vector2", default: { x: 0, y: 0 } },
      },
    },
    UITextSizeConstraint: {
      category: "modifier", canHaveChildren: false, singleton: "UITextSizeConstraint",
      props: {
        MaxTextSize: { type: "number", min: 1, max: 100, step: 1, default: 32 },
        MinTextSize: { type: "number", min: 1, max: 100, step: 1, default: 12 },
      },
    },
    UIGradient: {
      category: "modifier", canHaveChildren: false, singleton: "UIGradient",
      props: {
        ColorA: { type: "color3", default: "#4C8DFF" },
        ColorB: { type: "color3", default: "#3668C7" },
        Rotation: { type: "number", min: -360, max: 360, step: 1, default: 90 },
        TransparencyA: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
        TransparencyB: { type: "number", min: 0, max: 1, step: 0.01, default: 0 },
      },
    },
  };

  const MODIFIER_CLASSES = Object.keys(SCHEMA).filter((k) => SCHEMA[k].category === "modifier");
  const SCREEN_CHILD_CLASSES = Object.keys(SCHEMA).filter((k) => SCHEMA[k].category !== "modifier" && k !== "ScreenGui");
  const CONTAINER_CLASSES = new Set(["Frame", "ScrollingFrame", "TextLabel", "TextButton", "TextBox", "ImageLabel", "ImageButton"]);

  function canContain(parentClass, childClass) {
    if (!SCHEMA[childClass] || !SCHEMA[parentClass]) return false;
    // A ScreenGui is always a top-level root; nothing may parent another.
    if (childClass === "ScreenGui") return false;
    if (parentClass === "ScreenGui") return SCHEMA[childClass].category !== "modifier";
    if (SCHEMA[childClass].category === "modifier") return CONTAINER_CLASSES.has(parentClass);
    return CONTAINER_CLASSES.has(parentClass);
  }

  let _counter = 0;
  function generateId(prefix) {
    _counter = (_counter + 1) % 100000;
    return (prefix || "n") + "_" + Date.now().toString(36) + "_" + _counter.toString(36);
  }

  function clone(value) {
    return value == null || typeof value !== "object" ? value : JSON.parse(JSON.stringify(value));
  }

  function defaultProps(className) {
    const schema = SCHEMA[className];
    if (!schema) throw new Error("Unknown class: " + className);
    const out = {};
    for (const [key, def] of Object.entries(schema.props)) out[key] = clone(def.default);
    return out;
  }

  function createNode(className, name) {
    if (!SCHEMA[className]) throw new Error("Unknown class: " + className);
    return { id: generateId("n"), class: className, name: name || className, props: defaultProps(className), children: [] };
  }

  function validateValue(type, value, def) {
    const errors = [];
    const push = (m) => errors.push(m);
    if (type === "boolean") { if (typeof value !== "boolean") push("expected boolean"); }
    else if (type === "string") { if (typeof value !== "string") push("expected string"); }
    else if (type === "assetid") {
      if (typeof value !== "string") push("expected string asset id");
      else if (value !== "" && !/^rbxassetid:\/\/\d+$/.test(value)) push("asset id must be rbxassetid://<digits> or empty");
    } else if (type === "number") {
      if (typeof value !== "number" || !isFinite(value)) push("expected finite number");
      else {
        if (def && typeof def.min === "number" && value < def.min) push("below minimum " + def.min);
        if (def && typeof def.max === "number" && value > def.max) push("above maximum " + def.max);
      }
    } else if (type === "enum") { if (!def.values.includes(value)) push("must be one of " + def.values.join(", ")); }
    else if (type === "color3") { if (typeof value !== "string" || !/^#[0-9a-fA-F]{6}$/.test(value)) push("expected #RRGGBB color"); }
    else if (type === "udim") { if (!value || typeof value !== "object" || typeof value.scale !== "number" || typeof value.offset !== "number") push("expected {scale, offset}"); }
    else if (type === "udim2") {
      if (!value || typeof value !== "object" || !value.x || !value.y || typeof value.x.scale !== "number" || typeof value.x.offset !== "number" || typeof value.y.scale !== "number" || typeof value.y.offset !== "number") push("expected {x:{scale,offset}, y:{scale,offset}}");
    } else if (type === "vector2") {
      if (!value || typeof value !== "object" || typeof value.x !== "number" || typeof value.y !== "number") push("expected {x, y}");
    }
    return errors;
  }

  function validateNodeProps(className, props) {
    const schema = SCHEMA[className];
    const errors = [];
    const sanitized = {};
    if (!schema) return { valid: false, errors: [{ path: "", message: "unknown class " + className, severity: "error" }], sanitized: {} };
    for (const [key, def] of Object.entries(schema.props)) {
      const provided = props ? props[key] : undefined;
      if (provided === undefined) { sanitized[key] = clone(def.default); continue; }
      const issues = validateValue(def.type, provided, def);
      if (issues.length) {
        errors.push({ path: key, message: issues.join("; "), severity: "error" });
        sanitized[key] = clone(def.default);
      } else sanitized[key] = clone(provided);
    }
    if (props) for (const key of Object.keys(props)) {
      if (!(key in schema.props)) errors.push({ path: key, message: "unsupported property for " + className, severity: "warning" });
    }
    return { valid: errors.every((e) => e.severity !== "error"), errors, sanitized };
  }

  function validateTree(node, parentClass, seen) {
    const errors = [];
    if (!node || typeof node !== "object") { errors.push({ path: "", message: "node must be an object" }); return errors; }
    if (!SCHEMA[node.class]) { errors.push({ path: node.id || "?", message: "unknown class " + node.class }); return errors; }
    if (!node.id || typeof node.id !== "string") errors.push({ path: node.name || "?", message: "node is missing a stable id" });
    if (seen) {
      if (seen.has(node.id)) errors.push({ path: node.id, message: "duplicate node id" });
      seen.add(node.id);
    }
    if (parentClass && !canContain(parentClass, node.class)) errors.push({ path: node.name, message: node.class + " cannot be a child of " + parentClass });
    const props = validateNodeProps(node.class, node.props);
    for (const e of props.errors) errors.push({ path: (node.name || node.id) + "." + e.path, message: e.message, severity: e.severity });
    const childSeen = new Set();
    for (const child of (node.children || [])) {
      errors.push(...validateTree(child, node.class, seen));
      if (childSeen.has(child.class) && SCHEMA[child.class] && SCHEMA[child.class].singleton) {
        errors.push({ path: node.name + "." + child.name, message: "duplicate singleton modifier " + child.class });
      }
      childSeen.add(child.class);
    }
    return errors;
  }

  function validateDocument(doc) {
    const errors = [];
    if (!doc || typeof doc !== "object") return { valid: false, errors: [{ path: "", message: "document must be an object" }] };
    if (doc.version !== 1) errors.push({ path: "version", message: "unsupported document version" });
    if (!Array.isArray(doc.screens)) errors.push({ path: "screens", message: "screens must be an array" });
    else if (doc.screens.length === 0) errors.push({ path: "screens", message: "a document needs at least one screen" });
    const ids = new Set();
    for (const screen of (doc.screens || [])) {
      if (!screen.id || typeof screen.id !== "string") errors.push({ path: "screen", message: "screen missing id" });
      if (ids.has(screen.id)) errors.push({ path: screen.id, message: "duplicate screen id" });
      ids.add(screen.id);
      if (!screen.root) errors.push({ path: screen.name || screen.id, message: "screen has no root node" });
      else {
        if (screen.root.class !== "ScreenGui") errors.push({ path: screen.name || screen.id, message: "screen root must be a ScreenGui" });
        errors.push(...validateTree(screen.root, null, ids));
      }
    }
    return { valid: errors.every((e) => e.severity === "warning"), errors };
  }

  function createScreen(name) {
    return { id: generateId("scr"), name: name || "Screen1", root: createNode("ScreenGui", name || "ScreenGui") };
  }

  function defaultTokens() {
    return {
      palette: {
        bg: "#0F1115", panel: "#181B22", panelAlt: "#1F232C",
        accent: "#4C8DFF", accentDark: "#3668C7", accentLight: "#7FB0FF",
        text: "#F2F4F8", textMuted: "#8A92A3", stroke: "#2A2F3A",
        danger: "#E5484D", success: "#30A46C", warning: "#E5A23D",
      },
      fonts: { heading: "GothamBold", body: "SourceSans", numeric: "Code" },
      radii: { panel: 0.04, card: 0.06, button: 0.2, pill: 0.5 },
      spacing: 0.02,
    };
  }

  function createDocument(name) {
    return { version: 1, name: name || "Untitled UI", tokens: defaultTokens(), screens: [createScreen("Main")] };
  }

  function findNode(root, id) {
    if (!root) return null;
    if (root.id === id) return root;
    for (const child of (root.children || [])) { const f = findNode(child, id); if (f) return f; }
    return null;
  }

  function findParent(root, id) {
    if (!root) return null;
    for (const child of (root.children || [])) {
      if (child.id === id) return root;
      const f = findParent(child, id); if (f) return f;
    }
    return null;
  }

  function addNode(screen, parentId, className, name) {
    const parent = findNode(screen.root, parentId);
    if (!parent) throw new Error("Parent node not found: " + parentId);
    if (!canContain(parent.class, className)) throw new Error(parent.class + " cannot contain " + className);
    if (SCHEMA[className] && SCHEMA[className].singleton && (parent.children || []).some((c) => c.class === className)) {
      throw new Error(parent.name + " already has a " + className);
    }
    const node = createNode(className, name);
    parent.children = parent.children || [];
    parent.children.push(node);
    return node;
  }

  function removeNode(screen, id) {
    const parent = findParent(screen.root, id);
    if (!parent) throw new Error("Cannot remove the screen root");
    const index = parent.children.findIndex((c) => c.id === id);
    if (index < 0) throw new Error("Node not found: " + id);
    return parent.children.splice(index, 1)[0];
  }

  function renameNode(screen, id, name) {
    const node = findNode(screen.root, id);
    if (!node) throw new Error("Node not found: " + id);
    const clean = String(name || "").trim();
    if (!clean) throw new Error("Name must not be empty");
    node.name = clean;
    return node;
  }

  function reorderNode(screen, id, direction) {
    const parent = findParent(screen.root, id);
    if (!parent) throw new Error("Cannot reorder the screen root");
    const children = parent.children;
    const index = children.findIndex((c) => c.id === id);
    const target = direction === "up" ? index - 1 : index + 1;
    if (target < 0 || target >= children.length) return children[index];
    const tmp = children[index]; children[index] = children[target]; children[target] = tmp;
    return children[target];
  }

  function reassignIds(node) {
    node.id = generateId("n");
    for (const child of (node.children || [])) reassignIds(child);
  }

  function uniqueChildName(parent, base) {
    const names = new Set((parent.children || []).map((c) => c.name));
    if (!names.has(base)) return base;
    let i = 2;
    while (names.has(base + i)) i += 1;
    return base + i;
  }

  function duplicateNode(screen, id) {
    const parent = findParent(screen.root, id);
    if (!parent) throw new Error("Cannot duplicate the screen root");
    const source = findNode(screen.root, id);
    const copy = clone(source);
    reassignIds(copy);
    copy.name = uniqueChildName(parent, source.name + "Copy");
    const index = parent.children.findIndex((c) => c.id === id);
    parent.children.splice(index + 1, 0, copy);
    return copy;
  }

  function setProp(screen, id, key, value) {
    const node = findNode(screen.root, id);
    if (!node) throw new Error("Node not found: " + id);
    const schema = SCHEMA[node.class];
    const def = schema.props[key];
    if (!def) throw new Error("Unsupported property for " + node.class + ": " + key);
    const issues = validateValue(def.type, value, def);
    if (issues.length) throw new Error(key + ": " + issues.join("; "));
    node.props[key] = clone(value);
    return node;
  }

  function serializeDocument(doc) { return JSON.stringify(doc, null, 2); }

  function sanitizeTree(node) {
    node.props = validateNodeProps(node.class, node.props).sanitized;
    for (const child of (node.children || [])) sanitizeTree(child);
  }

  function parseDocument(text) {
    const parsed = typeof text === "string" ? JSON.parse(text) : text;
    const report = validateDocument(parsed);
    // Structural problems (wrong version, missing root, unknown class, illegal
    // nesting, duplicate ids) are unrecoverable and reject the document.
    // Out-of-range stored property VALUES are repairable: we sanitize them on
    // load rather than discard the user's whole design (spec section 5).
    const structural = report.errors.filter((e) => e.severity === undefined);
    if (structural.length) {
      const err = new Error("Invalid UI document");
      err.details = structural;
      throw err;
    }
    for (const screen of parsed.screens) sanitizeTree(screen.root);
    return parsed;
  }

  const API = {
    SCHEMA, MODIFIER_CLASSES, SCREEN_CHILD_CLASSES, CONTAINER_CLASSES,
    canContain, createNode, createScreen, createDocument, defaultTokens,
    validateValue, validateNodeProps, validateDocument, validateTree,
    findNode, findParent, addNode, removeNode, renameNode, reorderNode,
    duplicateNode, setProp, serializeDocument, parseDocument, generateId, clone,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = API;
  global.ZSRUI = global.ZSRUI || {};
  Object.assign(global.ZSRUI, { model: API });
})(typeof globalThis !== "undefined" ? globalThis : this);
