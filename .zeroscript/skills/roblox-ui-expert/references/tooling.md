# Tooling

Contents: Capability checklist, Mapping tools to jobs, If something is missing, Screenshot discipline, Build mechanics, Error recovery, Execute-Luau note.

## Capability checklist

Run this at the start. Inspect the tool list of the connected Studio MCP server or integration. Tool names vary, so map by function.

| Need | What to look for | If missing |
|------|------------------|------------|
| Create instance | create / insert / add instance with class and parent | Use any batch or execute tool as a build channel (see note below). If none exists, tell the user the toolchain cannot build UI |
| Set properties | set property / update instance | Same as above |
| Read properties | get property, get instance info (AbsolutePosition, AbsoluteSize, TextBounds) | Fall back to screenshot measurement only and say so; verification is weaker |
| Read tree | list children, get hierarchy | Track the tree in your notes and re-verify with screenshots |
| Delete / reparent | delete, move instance | Needed for cleanup of debug helpers; find another route |
| Screenshot | screenshot, capture viewport, capture UI | Hard blocker. Stop and tell the user (two sentences) |
| Viewport / device control | device emulator, set viewport size | Use the viewport simulation fallback below |

State in one line what you found and what you will use. Do not write a long inventory.

## Mapping tools to jobs

- Blockout and polish: create instance, set property.
- Vision check: screenshot, plus get property for alignment and spacing verification.
- Responsive test: device emulator or viewport fallback, then screenshot.
- Audit: read tree, read properties, delete debug helpers.

If the server offers search, bulk property set, or clone, use them to cut round trips, but keep the screenshot cadence.

## If something is missing

**No device emulator.** Use viewport simulation:

1. Create `_ViewportSim` as a sibling ScreenGui (or Frame) containing a Frame named `_SimFrame` with AnchorPoint (0.5, 0.5), Position (0.5, 0, 0.5, 0), and Size chosen to match the target aspect ratio within the screen (for example for 844 x 390, a frame about 0.9 wide and height = width x 390 / 844 converted to the screen's aspect).
2. Reparent or duplicate the UI's root container into `_SimFrame`, with the container set to Size (1, 0, 1, 0).
3. Screenshot. Scale-based layouts will behave as they would at that aspect. Offset-based pieces will visibly fail to scale, which helps catch them.
4. Delete `_ViewportSim` and put things back before finishing.

Caveats: this does not reproduce top bar insets, safe areas, or true pixel density. Say so in the report if you relied on it, and use the real emulator for final checks whenever possible.

**No property reads.** Use screenshots and the debug grid. Verification is weaker, so lean on the 3-cycle minimum and consider adding more cycles.

**Screenshots show only the 3D view, not UI.** Try a different capture target (Play test, device preview, UI-specific capture), or toggle the ScreenGui into view. If you cannot get UI into a screenshot at all, treat it as the hard blocker.

## Screenshot discipline

- Capture after every meaningful change. Meaningful means a reviewer would notice it. A color swap on one label is not, a new layout object is.
- Never batch five unrelated changes between screenshots. When one breaks something you cannot tell which.
- Always capture the same device between cycles for fair comparison, then sweep other devices in Phase 7.
- If a screenshot fails, is blank, or is clearly stale, fix the capture and retry. Do not proceed on assumption.
- Name or number captures in your notes so the report can cite them.
- If the tool allows zoom or crop, use it for element and edge checks.

## Build mechanics

- Parent first, then children. Set Name immediately on creation.
- Set the full property set for an instance in one call when possible.
- Set `ZIndexBehavior` and the inset behavior on the ScreenGui before building children.
- Create layout objects (UIListLayout, UIPadding, constraints) before populating children so you see the real result at each screenshot.
- Keep `LayoutOrder` explicit on layout children so order is deterministic.
- For repeated elements, build one fully styled, verify it, then clone or duplicate. Fix one before multiplying.
- Use transparent containers (`BackgroundTransparency = 1`) for pure layout holders, and set `BorderSizePixel = 0` on everything unless you want the legacy border.

## Error recovery

- Tool call fails: read the error, check class name, property name, parent path, and value type. Retry with the correction.
- Property set silently has no effect: a layout object or constraint probably controls it (UIListLayout overrides Position, UIAspectRatioConstraint overrides one axis of Size, UIGridLayout overrides Size). Edit the controlling object, not the child.
- Instance not found: re-read the tree. Names may have been auto-suffixed.
- Result differs from expectation: diagnose before piling on more changes. Read AbsoluteSize and AbsolutePosition of the instance and its parent.
- Two distinct failed approaches in a row on the same problem: only then report a blocker, with the exact errors and what you tried.

## Execute-Luau note

Some Studio MCP servers expose a run-code tool. The hard rule is that no scripts remain in the place and no UI is generated at runtime. Using such a tool for a one-time batch of instance creation is acceptable when native tools are too slow, provided the resulting UI exists as real instances and nothing is left behind. Prefer native tools. Do not use run-code to hide screenshots, skip verification, or generate UI behavior.
