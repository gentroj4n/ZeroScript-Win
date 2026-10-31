# Style System

Contents: Tokens first, Style presets, Type scale, Spacing rhythm, Radius and stroke rules, Color usage, Gradients, Shadows and depth, Icons without asset IDs, Panels and 9-slice, Consistency rules.

## Tokens first

Before building, fix a small token set and use it everywhere. Never invent a one-off color or size mid-build. If you need a new value, add it to the tokens and apply it consistently.

Minimum token set:

- Colors: `bg`, `panel`, `panel-alt`, `accent`, `accent-dark`, `accent-light`, `text`, `text-muted`, `danger`, `success`, `warning`, `stroke`.
- Fonts: heading, body, numeric.
- Type scale: see below.
- Radius classes: panel, card, button, pill.
- Stroke: thickness and color rule per class.
- Spacing base unit and the multiples used.

Write the tokens into the spec (Phase 1) and re-read them when a review finds inconsistency.

## Style presets

When there is no reference, pick one that fits the game and commit. Adjust values to taste but keep the internal logic.

**Chunky Cartoon (simulators, tycoons, pet games)**
- Mood: loud, friendly, high contrast.
- Palette: bg #1B2A4A, panel #2E4A7D, panel-alt #3C5E98, accent #FFC93C, accent-dark #D9962B, text #FFFFFF, text-muted #B9C8E6, danger #F25C54, success #5CD65C.
- Shape: large radii (card 0.18 to 0.25 scale, button 0.3 scale or pill), thick dark strokes (3 to 4 px) in a darker shade of the fill, bottom "lip" on buttons, bold rounded font (for example Fredoka-style or Gotham Black), text with dark UIStroke outline.

**Clean Modern (menus, social, shops with a polished feel)**
- Mood: calm, minimal, premium.
- Palette: bg #0F1115, panel #181B22, panel-alt #1F232C, accent #4C8DFF, accent-dark #3668C7, text #F2F4F8, text-muted #8A92A3, danger #E5484D, success #30A46C, stroke #2A2F3A.
- Shape: medium radii (panel 0.04 to 0.06 scale, button 0.2 scale), 1 px hairline strokes, subtle gradients (3 to 6 percent brightness delta), generous padding, Builder Sans or Gotham, light weights for body and semibold for headings.

**Dark Sci-Fi (shooters, space, tech)**
- Mood: tense, technical.
- Palette: bg #07090D, panel #0F141C, panel-alt #151C27, accent #00E0C6, accent-dark #00A894, text #E6F1FF, text-muted #6F8196, danger #FF4D4D, warning #FFB020.
- Shape: small radii (0.0 to 0.03 scale), thin accent strokes, corner notches or cut corners built from rotated Frames, uppercase letter-spaced headings, monospace-ish numerics, accent glow via a transparent accent-colored stroke layer.

**Fantasy Ornate (RPG, adventure)**
- Mood: warm, crafted.
- Palette: bg #1A120B, panel #3B2A1A, panel-alt #4A3622, accent #E0B040, accent-dark #A87F1E, text #F5E9D0, text-muted #B9A27A, danger #B3392F, success #6E9B3F.
- Shape: moderate radii (0.05 scale), double strokes (outer dark, inner gold), parchment-feel gradients, serif or fantasy display font for headings and a readable body font.

If the user names a game or art direction, derive tokens from it instead.

## Type scale

Define as fractions of screen height so it scales, then clamp with UITextSizeConstraint. Typical hierarchy at 1080 px height:

| Role | Approx size | Notes |
|------|-------------|-------|
| Display | 48 to 64 px | Rare, hero numbers or titles |
| Title | 32 to 40 px | Window titles |
| Heading | 24 to 28 px | Section heads |
| Body | 18 to 22 px | Descriptions, labels |
| Caption | 14 to 16 px | Fine print, secondary labels |

Rules:
- Adjacent steps must be visibly different, at least a 1.25 ratio.
- Max two font families, usually one.
- Numeric values (currency, timers, damage) get the numeric font and tabular feel where available, and a strong color.
- Never let text drop below about 12 px effective on phone.

## Spacing rhythm

Pick a base unit as a fraction of the container (for example 0.02 of window height). Use only multiples of it for padding and gaps (1x, 2x, 3x). Inconsistent gaps are the most visible amateur tell. Verify with property reads.

## Radius and stroke rules

- One radius per element class. Cards share a radius, buttons share a radius.
- Nested rounded shapes: inner radius should be slightly smaller than outer (outer minus padding) so the corners look concentric.
- Strokes: use `UIStroke` with `ApplyStrokeMode = Border` on frames and buttons, `Contextual` on text for outlines. Thickness stays modest. Stroke color is a darker or lighter shade of the fill, not pure black unless the style demands it.
- Strokes use pixel thickness, so they feel heavier on small screens. Verify on phone and, if it is a problem, reduce thickness or wrap the panel in a UIScale-managed container.

## Color usage

- 60/30/10: about 60 percent bg and panel colors, 30 percent secondary surfaces, 10 percent accent. The accent marks the primary action and key numbers only.
- Do not use pure black (#000000) or pure white (#FFFFFF) for large surfaces unless the style demands it. Slightly tinted neutrals look more designed.
- Keep semantic colors consistent: danger always red, success always green, currency always its own color.
- Disabled elements: desaturate and lower contrast, do not just lower opacity.

## Gradients

- `UIGradient` with a two-stop `ColorSequence` and a small brightness delta (4 to 12 percent) gives surfaces depth without looking cheap. Rotation 90 for top-to-bottom.
- Stronger gradients are fine for buttons and bars in cartoon styles: top lighter, bottom darker, within the same hue family.
- Use the Transparency sequence of a UIGradient for fade-outs at scroll edges and glows.
- Because UIGradient multiplies the element's base color, set the base to white (or a light tone) when you want the gradient colors to appear true.

## Shadows and depth

Roblox UI has no native blur shadow on Frames. Options, from cheapest to richest:

1. **Offset shadow frame.** A sibling Frame behind the element, same size, same UICorner, black at 0.6 to 0.8 transparency, positioned down and slightly right by 1 to 3 percent. Good enough for most cards.
2. **Layered shadow.** Two or three such frames with increasing size and decreasing opacity fake softness.
3. **Bottom lip.** For chunky buttons, a darker Frame offset downward behind the button, sized to peek out 6 to 10 percent of button height.
4. **Image shadow.** A soft-shadow ImageLabel, 9-sliced, only if the user supplied a verified asset ID.
5. **Inner highlight.** A thin lighter Frame or gradient along the top edge inside a panel gives a bevel.

Name them clearly (`CardShadow`, `ButtonLip`) and keep them behind the main element in draw order.

## Icons without asset IDs

You cannot invent asset IDs. Options:

- Use only IDs the user supplied or that already exist in the place's tree.
- Build simple icons from primitives: circles (Frame plus UICorner 0.5), plus and minus signs (two Frames), close X (two rotated Frames or a bold "X" TextLabel), arrows via rotated squares, stars and gems approximated with rotated rounded squares and layered colors.
- Use text glyphs available in the chosen font for small markers.
- Where a real icon is needed (coin, gem, sword), build a clearly named placeholder (`Icon_Coin_Placeholder`) with the right size, position, and a recognizable simple construction, and list it in the final report so the user can swap in their asset by setting `Image`.

Keep all icons in a consistent frame (same size box, same visual weight) so swapping is trivial.

## Panels and 9-slice

- Solid Frames with UICorner and UIStroke are the default panel. They scale perfectly.
- If a user-supplied panel image is used, set `ScaleType = Slice`, set `SliceCenter` so corners do not stretch, and confirm it at several sizes.
- Layer panels: outer frame (stroke or border color), inner frame (panel fill) inset by a small padding, optional header strip. This looks more designed than a single rectangle.

## Consistency rules

- Same element class, same treatment. All primary buttons look identical apart from content.
- No third font, no extra accent color, no stray radius.
- When a review finds inconsistency, fix by correcting the offending instance to the token, not by adjusting the token.
