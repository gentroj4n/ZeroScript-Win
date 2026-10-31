# Visual QA: How to Actually Review a Screenshot

Contents: The review mindset, Two sources of truth, Measurement techniques, Debug overlays, Checklist per screenshot, Blind spots, Comparing to a reference, Writing good deltas, What "no deltas" really means.

## The review mindset

A screenshot review is adversarial. You built it, so you are biased to see what you intended instead of what is there. Counter that by assuming the build is flawed and hunting for the flaws. A review that returns "looks good" has not looked.

Review in passes, not one glance:

1. Whole-screen pass: overall composition, balance, hierarchy, what the eye hits first.
2. Region pass: each region on its own (header, content, footer). Spacing and alignment inside it.
3. Element pass: each button, label, card. Text size, padding, corner radius, stroke, color.
4. Edge pass: edges of the screen and of containers. Clipping, overflow, things touching that should not be.

If the tool supports cropping or zooming, use it for the element and edge passes. Small flaws hide at full-screen scale.

## Two sources of truth

Eyes can mislead and numbers can mislead. Use both.

- **Pixels (the screenshot)** tell you what the player sees.
- **Properties (AbsolutePosition, AbsoluteSize, AbsoluteRotation, TextBounds, TextFits)** tell you exactly what the engine computed. Read them with the property tool instead of estimating from the image when you need to verify alignment, equal spacing, or whether text fits.

Use properties to confirm a suspicion from the screenshot, and use the screenshot to catch what properties cannot tell you (wrong color feel, weak hierarchy, ugly proportions).

## Measurement techniques

**Ratios beat pixels.** Screenshot scaling makes absolute pixel guesses unreliable. Measure relative to something stable: header height as a fraction of window height, gap as a fraction of card width, text cap height as a fraction of button height. Compare those ratios to the reference.

**Equal spacing check.** For a row or column of N elements, read AbsolutePosition and AbsoluteSize for each. Gaps should be identical (or follow the intended rhythm). A mismatch of more than 1 to 2 px at the test resolution is a delta.

**Alignment check.** Elements meant to share an edge or center must have identical AbsolutePosition.X (left edges), AbsolutePosition.X + AbsoluteSize.X (right edges), or identical center values. Read them. Do not eyeball.

**Margin consistency.** Inner padding on a panel should be equal on comparable sides unless deliberately asymmetric. Check left, right, top, bottom against the base unit.

**Text fit.** Read `TextBounds` against the label's `AbsoluteSize`. If TextBounds exceeds the label, text is clipping or shrinking. Check `TextFits` where available. Also check wrapped text for orphans and awkward line breaks.

**Contrast.** Judge text against its immediate background, not the page. Body text on a mid-tone panel is the usual failure. Light text on a light gradient or dark text on a saturated color often fails. If you doubt it, it fails: darken the background or change the text color. Aim for strong contrast on anything the player must read quickly (currency, prices, timers, button labels).

**Visual weight.** Squint at the screenshot or imagine it blurred. The primary action and the most important number should dominate. If three things compete, two of them are too loud.

## Debug overlays

Temporary helpers, always prefixed `_Debug`, always deleted before finishing.

- `_DebugGrid`: a full-size Frame with thin semi-transparent lines at 10 percent intervals (build as a few Frames with 1px lines, or a grid of bordered cells). Place it above everything with the highest ZIndex. Use it to judge alignment and proportions against the reference's imagined grid.
- `_DebugTint`: temporarily set distinct BackgroundColor3 and BackgroundTransparency = 0.5 on invisible container Frames (those with BackgroundTransparency = 1) so you can see their true bounds. Restore afterwards.
- `_DebugCenter`: crosshair lines at the exact center of a container to verify centering.

Record in your notes which helpers you added. The audit fails if any remain.

## Checklist per screenshot

Run this each time and write findings, do not just think them.

- Composition: balanced, anchored to the correct edges, nothing floating without reason.
- Hierarchy: clear first, second, third read.
- Spacing: consistent rhythm, no cramped or loose outliers, padding equal where intended.
- Alignment: edges and centers line up (verified with properties where it matters).
- Typography: size steps are distinct, line heights comfortable, no clipped or shrunk text, correct font and weight, correct alignment (left, center, right).
- Color: palette matches tokens, no stray default greys, accent used sparingly, contrast adequate.
- Shape language: corner radii consistent by element class, strokes consistent, no mixture of sharp and round without reason.
- Depth: shadows and layering make sense, nothing visually flat that should pop, nothing floating wrongly.
- Details: icons centered and sized consistently, dividers present where needed, badges and notches positioned precisely.
- Edges: no clipping at screen or container boundaries, nothing under the top bar or thumb controls.
- Completeness: every element in the spec or reference is present.

## Blind spots to watch for

These are the mistakes reviewers (human and AI) most often miss.

- Text centered in its Label but not in its Button, because of mismatched padding or Text alignment.
- Icons that are 2 to 4 px off-center. Check with properties.
- Gaps that look equal but differ by 20 to 30 percent.
- Rounded corners that differ across sibling elements.
- Stroke thickness that looks fine on desktop and heavy on phone (Offset stroke on a smaller screen). Strokes use Offset thickness, so verify them on small devices.
- A shadow layer that peeks out on one side only.
- ZIndex or sibling-order problems that hide a decorative element behind a panel.
- Clipped descendants from `ClipsDescendants` on a parent that is slightly too small.
- ScrollingFrame content that starts flush against the scroll edge with no padding, or whose last item is cut off with no bottom padding.
- Text that fits on desktop but wraps or shrinks on phone.
- Anything near the top-left or top edge sitting under the Roblox top bar or chat button.
- Bottom corners of the screen colliding with the mobile thumbstick or jump button zone.

## Comparing to a reference

1. Match the viewport aspect ratio of the reference first. Comparing a 16:9 screenshot to a 4:3 mockup produces false deltas.
2. Compare at the same scale in your head: use ratios, not raw pixels.
3. Build a mental overlay: where does each element's center sit relative to the reference? Estimate offsets as percentages of screen width and height.
4. List every element in the reference and confirm presence, position, size, color, and detail level in yours.
5. If the reference has details you cannot reproduce exactly (custom artwork, illustrated icons), approximate honestly with shapes and glyphs, match placement and size, and note the gap for the report. Do not drop the element.

## Writing good deltas

Bad: "The header looks off." "Spacing could be better." "Colors are slightly different."

Good: "Header height is ~0.08 of window height, reference ~0.11. Title cap height is ~60 percent of reference. Raise Header.Size.Y.Scale to 0.11 and title MaxTextSize to 40."

Every delta names an instance, a measurement, and a fix. If you cannot state the fix, you do not understand the problem yet, so investigate with property reads.

## What "no deltas" really means

You may declare the review clean only when you have run every pass above, read properties for every alignment and spacing claim that matters, compared against the reference or spec element by element, and still cannot state a measurable flaw. If your first review of a new build finds fewer than five issues, you reviewed too gently. Look again.
