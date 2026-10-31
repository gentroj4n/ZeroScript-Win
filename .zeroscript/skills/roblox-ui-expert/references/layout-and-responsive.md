# Layout and Responsive Design

Contents: Core principles, Constraint toolkit, Anchoring patterns, Safe areas and mobile zones, Device matrix, Text scaling, ZIndex and draw order, Layout failure patterns.

## Core principles

1. **Scale first.** Size and Position in Scale so the UI adapts to any viewport. Offset is for hairlines only.
2. **Design for the smallest hard case, verify on the rest.** Phone landscape (short height) is where most UI breaks. If it works there, desktop usually works.
3. **Let layout objects own repeated positioning.** Hand-placing five buttons is a bug waiting for the second device.
4. **Constrain, do not hope.** Use constraints to cap growth on large screens and protect shape on odd aspect ratios.
5. **Height is the scarce axis on phones.** Plan vertical budgets explicitly: header, content, footer percentages that sum to 1.0 with padding.

## Constraint toolkit

**UIAspectRatioConstraint**
- Keeps a shape consistent across aspect ratios. Use on windows, cards, icon boxes, avatars, square buttons.
- `AspectRatio` = width / height. `DominantAxis` decides which axis drives the other. `AspectType = FitWithinMaxSize` keeps it inside the parent, `ScaleWithParentSize` scales with it.
- Put it on the element, not its children, and verify the element still fits on phone landscape.

**UISizeConstraint**
- `MinSize` and `MaxSize` in pixels. Use MaxSize so windows do not become enormous on large monitors and MinSize so tap targets and text stay usable on small screens.
- Pair with an aspect ratio carefully. If both clamp, the final size can surprise you, so verify with AbsoluteSize reads on each device.

**UITextSizeConstraint**
- `MinTextSize` and `MaxTextSize`. Use with `TextScaled = true` so text grows with its box but is capped and floored.
- Typical: body MinTextSize 12, MaxTextSize 24. Titles MaxTextSize 36 to 48. Check legibility at MinTextSize on phone.

**UIScale**
- Scales an entire subtree. Good for a quick whole-panel fit on small screens when proportions are right but everything is slightly too big. Not a substitute for fixing layout. Note that stroke thickness and Offset values scale with it.

**UIPadding**
- Use Scale-based padding for inner margins. Apply to containers instead of insetting each child.

**UIListLayout**
- `FillDirection`, `HorizontalAlignment`, `VerticalAlignment`, `SortOrder = LayoutOrder`, `Padding` (UDim, prefer Scale), `Wraps` for flowing rows.
- Flex: `HorizontalFlex` / `VerticalFlex` set to `Fill`, `SpaceBetween`, `SpaceAround`, `SpaceEvenly` to distribute children without math. Use `UIFlexItem` on a child to control grow and shrink.
- Children with `Visible = false` are skipped by layouts.

**UIGridLayout**
- `CellSize`, `CellPadding`, `FillDirectionMaxCells`, `StartCorner`. Good for uniform cells. For responsive grids whose columns should reflow, prefer a wrapping UIListLayout with fixed-fraction child widths plus UIAspectRatioConstraint on the children.

**ScrollingFrame**
- `AutomaticCanvasSize = Y` (or X) with `CanvasSize` zero so content drives scroll length. Set `ScrollingDirection` explicitly. Add a UIPadding inside so first and last items are not flush to the edges, including bottom padding so the last row can scroll clear.
- Style the scrollbar: `ScrollBarThickness` modest, `ScrollBarImageColor3` from tokens, transparent track.

**Rounded corners**
- `UICorner` with a Scale radius is relative to the smaller dimension of the element. Scale 0.5 makes a pill or circle. Use Scale radii for elements that vary in size so corners stay proportional, and Offset radii for fixed-size details.

## Anchoring patterns

Always set `AnchorPoint` deliberately and pair it with a Position that matches.

- Centered element: AnchorPoint (0.5, 0.5), Position (0.5, 0, 0.5, 0).
- Top-right corner element: AnchorPoint (1, 0), Position (1 - margin, 0, margin, 0).
- Bottom-left: AnchorPoint (0, 1), Position (margin, 0, 1 - margin, 0).
- Bottom-center action bar: AnchorPoint (0.5, 1), Position (0.5, 0, 1 - margin, 0).

Use a consistent screen margin (for example 0.015 to 0.025 of screen height converted to the relevant axis) for HUD elements so the screen feels designed.

Prefer anchoring to the nearest relevant edge rather than centering everything. HUD pieces should hug their corner so they stay in place as aspect ratio changes.

## Safe areas and mobile zones

- Set the ScreenGui's inset behavior deliberately. `ScreenInsets` (or `IgnoreGuiInset` on older setups) controls whether the UI sits below the top bar and inside device safe areas. Default to respecting insets for interactive UI. Use full-screen only for backdrops and effects that are meant to bleed.
- The Roblox top bar and chat button occupy the top-left and top area. Do not put essential UI there unless you offset for it.
- On mobile, the default thumbstick sits bottom-left and the jump button bottom-right. Keep important tap targets out of those zones during gameplay HUDs. Mid-bottom and upper regions are safer for action buttons.
- Notched devices and rounded corners clip screen edges. Keep a margin on all four sides.
- Tap targets: aim for at least 44 px on the short side at the phone test size. Smaller targets get missed. If a button is below that on phone, enlarge it or add padding, not a bigger label.

## Device matrix

Verify every screen on these. Screenshot each.

| Device | Approx viewport | Why it matters |
|--------|-----------------|----------------|
| Desktop 16:9 | 1920 x 1080 | Primary reference, MaxSize caps |
| Laptop | 1366 x 768 | Common smaller desktop |
| Tablet landscape | 1180 x 820 | 4:3-ish ratio stresses aspect handling |
| Tablet portrait | 820 x 1180 | Vertical layouts |
| Phone landscape | 844 x 390 | Hardest case, very short height |
| Phone portrait | 390 x 844 | Narrow, tall |
| Small phone landscape | 667 x 375 | Worst realistic case |

Skip a device only if the screen genuinely never appears there (for example, a landscape-only game with portrait locked) and state that in the report.

Use Studio's device emulator when the toolchain exposes it. If it does not, use the viewport simulation fallback in `tooling.md`.

For each device check:

- Nothing clips or overlaps.
- Text is readable at its actual size.
- Images and icons keep their aspect ratio.
- Tap targets are large enough.
- Thumb zones and the top bar are clear.
- Layouts reflow sensibly (grids change columns instead of crushing cells).
- The overall composition still feels intentional.

## Text scaling

- `TextScaled = true` with a `UITextSizeConstraint` is the default approach for responsive labels. Give the label a sensible height so scaled text has room.
- Siblings that should look equal (a row of tab labels) must have equal-height boxes, or their scaled text sizes will differ and look inconsistent. Check this.
- Set `TextWrapped = true` for body copy and `TextTruncate` or a deliberate max length for single-line values like names.
- Use `RichText` for inline emphasis rather than extra labels.
- Prefer `FontFace` to legacy `Font` when you need a specific weight.
- Do not scale numeric counters so they jump around. Right-size the box and constrain text size.

## ZIndex and draw order

- With `ZIndexBehavior = Sibling`, draw order is decided among siblings by `ZIndex`, then by order in the parent (later siblings draw on top). Parents always draw below children.
- Fix overlap by ordering siblings and setting ZIndex sparingly. Avoid ZIndex numbers in the hundreds. Use a small consistent scheme: background 0, content 1, overlays 2, popups 3.
- Backdrops and modals go in their own ScreenGui with a higher `DisplayOrder` so they always sit above gameplay HUD.

## Layout failure patterns

Look for these specifically and fix at the root.

- **Offset sizes** that look right on desktop and break on phone. Convert to Scale.
- **Everything is a fixed aspect ratio** and the UI becomes a tiny box on a wide screen. Use aspect ratio on shapes, Scale elsewhere.
- **Stretched images.** Missing aspect ratio constraint, or wrong `ScaleType`. Use `Fit` or `Crop` for photos, `Slice` with `SliceCenter` for panels.
- **Text overflow** after localization or longer values. Leave room and constrain.
- **Grid collapse** on phone portrait: cells become tiny. Reduce columns by changing child widths or switching layout at that size.
- **Layout fights**: a UIListLayout plus manual Position on children, or multiple constraints disagreeing. Remove the manual positioning.
- **Padding applied twice** (parent UIPadding plus child margins).
- **Clipping from ClipsDescendants** on a container that is a few pixels too small.
- **Backdrop not covering** the full screen because of inset settings.
