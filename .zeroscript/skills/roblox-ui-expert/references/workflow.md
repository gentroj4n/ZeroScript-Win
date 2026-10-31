# Workflow Details

Contents: Phase 1 Intake, Phase 2 Plan, Phase 3 Blockout, Phase 4 Vision check, Phase 5 Fix loop, Phase 6 Polish, Phase 7 Responsive test, Phase 8 Final audit, Iteration log, Handling no reference, Handling edits to existing UI.

## Phase 1: Intake

Read the reference image, spec, or request. Write the spec below before touching Studio. Keep it terse. If something is ambiguous, decide and record the decision.

```
SPEC
Screen: <name>
Purpose: <one line, what the player does here>
Reference: <image provided / description only / none>

Regions (top to bottom, left to right):
- <Region>: <contents, approx share of screen as %>

Hierarchy (what the eye hits first, second, third):
1. ...
2. ...

Tokens:
- Palette: bg #xxxxxx, panel #xxxxxx, panel-alt #xxxxxx, accent #xxxxxx, accent-dark #xxxxxx, text #xxxxxx, text-muted #xxxxxx, danger #xxxxxx, success #xxxxxx
- Fonts: heading <font>, body <font>, numeric <font>
- Radius: <scale or px by element class>
- Stroke: <thickness, color rule>
- Spacing rhythm: base unit = <x% of screen height>, gaps are multiples of it

Elements needing states: <list>
States to depict: normal, hover, pressed, disabled, selected, empty, loading, error (only those relevant)

Decisions made on ambiguity:
- ...

Target devices: desktop, tablet, phone landscape, phone portrait (note any that do not apply)
```

If the user gave a reference image, extract colors by careful visual estimation and state them as approximate. Do not claim exact hex values you cannot verify. Pick the nearest clean value and keep it consistent everywhere.

## Phase 2: Plan

Write the full instance tree before building. Include layout objects and constraints. Example:

```
ShopGui (ScreenGui)
  Backdrop (Frame)
  Window (Frame) + UICorner + UIStroke + UIAspectRatioConstraint + UISizeConstraint
    Header (Frame) + UIPadding
      TitleLabel (TextLabel)
      CloseButton (TextButton) + UICorner + UIStroke
    TabBar (Frame) + UIListLayout
      TabFeatured, TabPets, TabBoosts (TextButton) + UICorner
    Content (Frame) + UIPadding
      ItemGrid (ScrollingFrame) + UIListLayout(Wraps)
        ItemCard x8 (Frame) + UICorner + UIAspectRatioConstraint
    Footer (Frame)
      CurrencyPill (Frame) + UIListLayout
```

Check the plan: does every visible element in the reference appear in it? If not, fix the plan, not the build later.

## Phase 3: Blockout

Build the ScreenGui, major containers, and layout objects with flat, high-contrast placeholder colors so bounds are visible. Real content sizes and real text. No polish yet. Capture a screenshot. The goal is correct proportions and structure.

Build order: ScreenGui, root containers, regions, layout objects and constraints, then leaf elements. Parent before child. Set properties in as few calls as the tool allows.

## Phase 4: Vision check

Compare the screenshot to the reference side by side. Produce a numbered delta list. Each delta uses this format:

```
[severity] Element: observed vs expected, evidence, fix
```

Severity:

- blocker: broken layout, missing element, unreadable text, wrong overall structure.
- major: clearly noticeable mismatch in size, spacing, color, hierarchy, alignment.
- minor: small polish difference a careful reviewer would still notice.

Example:

```
[major] TitleLabel: cap height ~28px at 1920x1080, reference ~40px. Header feels weak. Raise MaxTextSize constraint to 40 and header height from 0.09 to 0.11 scale.
[major] ItemGrid: card gaps ~18px, header gap ~32px. Inconsistent rhythm. Set UIListLayout.Padding to match base unit.
[minor] CloseButton: icon sits ~3px left of center. Switch Text alignment or add UIPadding right 0.03 scale.
```

Rules: at least five observations on the first review. Reference exact instance names. Prefer numbers (estimated pixels, ratios, hex differences) over adjectives. See `visual-qa.md` for how to measure.

## Phase 5: Fix loop

Apply the fixes. Capture. Run Phase 4 again on the new screenshot. That is one cycle. Run at least three cycles per screen. Stop only when a full cycle finds no major or blocker deltas and you cannot name a measurable minor flaw you consider worth fixing.

Do not trust that a fix worked. The new screenshot must confirm it. If a fix did not change what you expected, diagnose before stacking more changes (wrong instance, a layout object overriding the property, a constraint fighting another constraint, ZIndex ordering).

## Phase 6: Polish

In this order: strokes, gradients, shadow layers, icons and glyphs, text hierarchy (size, weight, color), small details (dividers, badges, notches, highlights). Capture after each group, not after each property. Run a delta pass after polish.

## Phase 7: Responsive test

Test every device in the matrix from `layout-and-responsive.md`. For each, capture, then check: clipping, overlap, text legibility, stretched images, tap target size, thumb zones, top bar inset, off-screen elements. Fix at the root cause (usually a constraint or a wrong Scale), not by tuning one device and breaking another. Re-test every device after any non-trivial fix.

## Phase 8: Final audit

Walk the instance tree against `audit-checklist.md`. Delete all debug helpers. Take one last clean screenshot on the primary device.

## Iteration log

Keep a running log in your working notes (not in the place). One entry per cycle:

```
Cycle N | Device: <size> | Deltas found: <count by severity> | Fixed: <summary> | Remaining: <summary>
```

Use it in the final report to show what vision checks changed.

## Handling no reference

If the user gives only a description ("make a shop UI"):

1. Pick a style preset from `style-system.md` that fits the game genre, or define a custom token set. Decide fast.
2. State the direction in three lines: mood, palette, shape language.
3. Write the spec and build. Do not ask what style they want. Offer to restyle afterwards, which is cheap because tokens are centralized.
4. Treat your own spec as the reference during vision checks. Critique against it as hard as you would a mockup.

## Handling edits to existing UI

1. Read the existing tree first. Map names, layout objects, constraints, and tokens already in use.
2. Screenshot the current state on the primary device before changing anything. This is your baseline.
3. Match the existing token system unless asked to change it. Do not introduce a second palette.
4. Make changes, then run the same vision and responsive cycles as a new build, scoped to the affected area plus anything it neighbors.
5. If the existing UI breaks the hard rules (Offset layouts, default styling, bad names), fix them in the area you touch and flag the rest in the report instead of silently rewriting the whole thing.
