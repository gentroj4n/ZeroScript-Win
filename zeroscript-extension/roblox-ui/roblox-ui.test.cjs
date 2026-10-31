// SPDX-License-Identifier: GPL-3.0-or-later
"use strict";

const assert = require("node:assert/strict");
const model = require("./model.js");
const luau = require("./luau.js");

// 1. Document creation and serialization.
{
  const doc = model.createDocument("My UI");
  assert.equal(doc.version, 1);
  assert.equal(doc.screens.length, 1);
  assert.equal(doc.screens[0].root.class, "ScreenGui");
  const text = model.serializeDocument(doc);
  const parsed = model.parseDocument(text);
  assert.equal(parsed.screens[0].root.class, "ScreenGui");
  assert.ok(parsed.screens[0].root.id, "root keeps a stable id across round-trip");
}

// 2. Component create/rename/reorder/duplicate/delete.
{
  const doc = model.createDocument();
  const screen = doc.screens[0];
  const frame = model.addNode(screen, screen.root.id, "Frame", "Panel");
  const btn1 = model.addNode(screen, frame.id, "TextButton", "A");
  const btn2 = model.addNode(screen, frame.id, "TextButton", "B");
  assert.deepEqual(frame.children.map((c) => c.name), ["A", "B"]);
  model.reorderNode(screen, btn2.id, "up");
  assert.deepEqual(frame.children.map((c) => c.name), ["B", "A"]);
  model.renameNode(screen, btn1.id, "Renamed");
  assert.equal(btn1.name, "Renamed");
  const dup = model.duplicateNode(screen, btn1.id);
  assert.notEqual(dup.id, btn1.id, "duplicate gets a fresh id");
  assert.equal(dup.name, "RenamedCopy");
  model.removeNode(screen, btn2.id);
  assert.ok(!model.findNode(screen.root, btn2.id));
}

// 3. Property validation and supported types.
{
  const doc = model.createDocument();
  const screen = doc.screens[0];
  const frame = model.addNode(screen, screen.root.id, "Frame");
  model.setProp(screen, frame.id, "BackgroundColor3", "#112233");
  assert.equal(frame.props.BackgroundColor3, "#112233");
  assert.throws(() => model.setProp(screen, frame.id, "BackgroundColor3", "not-a-color"), /expected #RRGGBB/);
  assert.throws(() => model.setProp(screen, frame.id, "ZIndex", 999), /above maximum/);
  assert.throws(() => model.setProp(screen, frame.id, "NonsenseProp", 1), /Unsupported property/);
}

// 4. Parent-child hierarchy rules.
{
  const doc = model.createDocument();
  const screen = doc.screens[0];
  const frame = model.addNode(screen, screen.root.id, "Frame");
  assert.throws(() => model.addNode(screen, screen.root.id, "UICorner"), /cannot contain/);
  model.addNode(screen, frame.id, "UICorner");
  assert.throws(() => model.addNode(screen, frame.id, "UICorner"), /already has a UICorner/);
  assert.throws(() => model.addNode(screen, frame.id, "ScreenGui"), /cannot contain/);
}

// 5. Validation of an unsupported property is a warning, not a hard failure.
{
  const report = model.validateNodeProps("Frame", { Size: { x: { scale: 1, offset: 0 }, y: { scale: 1, offset: 0 } }, Bogus: 1 });
  assert.ok(report.errors.some((e) => e.severity === "warning" && /unsupported/.test(e.message)));
}

// 7. Roblox-compatible output generation.
{
  const doc = model.createDocument();
  const screen = doc.screens[0];
  const frame = model.addNode(screen, screen.root.id, "Frame", "Panel");
  model.addNode(screen, frame.id, "UICorner");
  const code = luau.generateScreen(screen, doc.tokens);
  assert.match(code, /Instance\.new\("ScreenGui"\)/);
  assert.match(code, /Instance\.new\("Frame"\)/);
  assert.match(code, /Instance\.new\("UICorner"\)/);
  assert.match(code, /Color3\.fromRGB\(/);
  assert.match(code, /UDim2\.new\(/);
  assert.match(code, /Enum\./);
  assert.ok(!/undefined/.test(code), "generated Luau must not contain undefined literals");
}

// 8. flattenForStudio produces parent links that resolve.
{
  const doc = model.createDocument();
  const screen = doc.screens[0];
  const frame = model.addNode(screen, screen.root.id, "Frame", "Panel");
  const flat = luau.flattenForStudio(screen);
  const names = new Set(flat.map((f) => f.name));
  assert.equal(flat[0].parent, "StarterGui");
  assert.ok(names.has("Panel"));
  for (const item of flat.slice(1)) assert.ok(names.has(item.parent), "parent " + item.parent + " must exist");
}

// 9. Persistence round-trip preserves ids and repairs bad values.
{
  const doc = model.createDocument();
  const screen = doc.screens[0];
  const frame = model.addNode(screen, screen.root.id, "Frame", "Keep");
  frame.props.ZIndex = 999; // invalid, stored as-is
  const text = JSON.stringify(doc);
  const loaded = model.parseDocument(text);
  const loadedFrame = loaded.screens[0].root.children.find((c) => c.name === "Keep");
  assert.ok(loadedFrame, "frame survives round-trip");
  assert.equal(loadedFrame.id, frame.id, "id is stable");
  assert.ok(loadedFrame.props.ZIndex <= 50, "invalid stored value was sanitized to the default");
}

// 10. Invalid document is rejected with details.
{
  assert.throws(() => model.parseDocument(JSON.stringify({ version: 1, screens: [] })).root, /Invalid/);
  try {
    model.parseDocument(JSON.stringify({ version: 2, screens: [] }));
    assert.fail("should have thrown");
  } catch (err) {
    assert.ok(Array.isArray(err.details));
  }
}

// 11. Document-level validation catches a non-ScreenGui root.
{
  const doc = model.createDocument();
  doc.screens[0].root = model.createNode("Frame");
  const report = model.validateDocument(doc);
  assert.equal(report.valid, false);
  assert.ok(report.errors.some((e) => /root must be a ScreenGui/.test(e.message)));
}

process.stdout.write("Roblox UI workspace tests passed.\n");
