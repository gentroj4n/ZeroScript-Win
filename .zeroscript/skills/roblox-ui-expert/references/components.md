# Component Recipes

Contents: How to use these recipes, Primary button, Icon button, Modal window, Progress bar, Currency pill, Shop or item card, Item grid, Tab bar, Toast, Tooltip, HUD corner cluster, Text hierarchy block, Building states without scripts.

## How to use these recipes

These are starting structures with sensible property values. Adjust to the style tokens. Values are Scale unless stated. Always verify with screenshots and property reads. Never copy a recipe without checking it against the reference.

Names in these trees are suggested. Keep naming role-based.

## Primary button

```
PlayButton (TextButton)   Text = "", AutoButtonColor = false, BackgroundColor3 = accent, Size = {0.2,0},{0.09,0}
  UICorner               CornerRadius = {0.3,0}
  UIStroke               Thickness = 3, Color = accent-dark, ApplyStrokeMode = Border
  UIGradient             Rotation = 90, accent-light -> accent
  Label (TextLabel)      Size = {1,0},{1,0}, BackgroundTransparency = 1, TextScaled = true, FontFace = heading
    UITextSizeConstraint MaxTextSize = 32, MinTextSize = 14
    UIStroke             Contextual, Thickness = 2, Color = dark shade of accent
    UIPadding            Top/Bottom 0.2, Left/Right 0.08
```

Optional lip: a Frame named `ButtonLip` behind the button, same size and corner, BackgroundColor3 = accent-dark, Position shifted down about 0.08 of the button height, ZIndex below.

Why `Text = ""` with a child Label: you control padding, outline, and scaling independently of the button's own text.

If you want free hover and press feedback without scripts, leave `AutoButtonColor = true` and skip the custom lip. It trades custom look for built-in darkening.

## Icon button (square or circular)

```
CloseButton (TextButton)  Size = {0.06,0},{0.06,0}, AnchorPoint top-right, UIAspectRatioConstraint = 1
  UICorner                CornerRadius = {0.5,0} for circle, {0.25,0} for rounded square
  UIStroke
  Glyph (TextLabel)       Text = "X" or built from two rotated Frames, centered
```

Keep the minimum tap size in mind: at the phone test size this should be at least about 44 px on its short side.

## Modal window

```
ModalGui (ScreenGui)      DisplayOrder high, IgnoreGuiInset/ScreenInsets chosen so backdrop covers full screen
  Backdrop (Frame)        Size = {1,0},{1,0}, BackgroundColor3 = black, BackgroundTransparency = 0.45
  Window (Frame)          AnchorPoint = (0.5,0.5), Position = {0.5,0},{0.5,0}, Size ~ {0.5,0},{0.7,0}
    UICorner, UIStroke, UIGradient (subtle)
    UIAspectRatioConstraint  (for example 1.4) with UISizeConstraint MaxSize to cap on big screens
    Header (Frame)        Size = {1,0},{0.14,0}
      TitleLabel (TextLabel) left aligned, UITextSizeConstraint
      CloseButton           top-right anchor
    Divider (Frame)       Size = {0.96,0},{0,2}, centered, color = stroke
    Content (Frame)       fills remaining height, UIPadding
    Footer (Frame)        action buttons, UIListLayout horizontal, HorizontalAlignment Right
```

Check on phone landscape: height budget. A 0.7-height window on a 390 px tall viewport is 273 px. Header plus footer plus content must fit. If it does not, raise Size.Y toward 0.85 and trim header and footer heights.

## Progress bar

```
HealthBar (Frame)         Size = {0.3,0},{0.035,0}, UIAspectRatioConstraint wide (for example 9) if shape must hold
  UICorner                CornerRadius = {0.5,0}
  UIStroke                Thickness = 2, Color = dark
  Track (Frame)           background fill, dark, UICorner 0.5
  Fill (Frame)            Size = {0.65,0},{1,0}, UICorner 0.5, UIGradient top lighter to bottom darker
  Sheen (Frame)           thin white at 0.8 transparency across upper half of Fill for gloss
  ValueLabel (TextLabel)  centered, "650 / 1000", UIStroke Contextual
```

The Fill's X scale is the value. Depict a realistic mid value (for example 0.65), not 0 or 1, so the review can judge the shape. Also check the near-empty state: a very small Fill with rounded corners can look broken. Add a MinSize or hide below a threshold in the later scripting stage and note it.

## Currency pill

```
CoinPill (Frame)          Size = {0.14,0},{0.06,0}, UICorner 0.5, UIStroke, panel color
  UIListLayout            horizontal, VerticalAlignment Center, Padding 0.05
  UIPadding               left/right 0.08
  Icon (Frame or ImageLabel)  UIAspectRatioConstraint 1, height = 0.8 of pill
  ValueLabel (TextLabel)  UIFlexItem grow, text right or left aligned, numeric font
  PlusButton (TextButton) small accent circle, optional
```

Numbers: use realistic values like "12,450", not "0", so width behavior is tested.

## Shop or item card

```
ItemCard (Frame)          Size = {0.22,0},{0.22,0}, UIAspectRatioConstraint 0.8 (portrait card)
  UICorner, UIStroke, UIGradient
  UIListLayout            vertical, Padding small
  UIPadding               0.06
  Preview (Frame)         top 55 percent, panel-alt background, UICorner, holds the item image or placeholder
    RarityBadge (TextLabel) top-left, small pill
  NameLabel (TextLabel)   TextScaled, max 22
  PriceButton (TextButton) bottom, accent, UICorner pill, contains coin icon and price
```

Vary the sample cards: different names, rarities, one sold-out or equipped variant, so the review sees real range. Do not populate with eight identical cards.

## Item grid

Responsive wrapping pattern (preferred):

```
ItemGrid (ScrollingFrame) Size = {1,0},{1,0}, AutomaticCanvasSize = Y, CanvasSize = {0,0},{0,0}, ScrollBarThickness thin
  UIPadding               all sides
  UIListLayout            FillDirection Horizontal, Wraps = true, Padding = {0.02,0}, HorizontalAlignment Left
  ItemCard x N            Size = {0.23,0},{0.23,0} + UIAspectRatioConstraint (DominantAxis = Width)
```

Width fraction plus padding must total at or under 1.0 per row (4 x 0.23 + 3 x 0.02 = 0.98). Verify the column count on phone portrait and adjust the width fraction at that design point. The aspect constraint makes cell height follow width.

Alternative for strict uniform cells: UIGridLayout with `CellSize` and `CellPadding` in Scale, verified with AbsoluteSize reads.

Keep one hidden template card (`ItemTemplate`, Visible = false) at the end for later scripting. Hidden items are skipped by layouts.

## Tab bar

```
TabBar (Frame)            Size = {1,0},{0.1,0}
  UIListLayout            horizontal, HorizontalFlex = Fill, Padding small
  TabFeatured, TabPets, TabBoosts (TextButton)  UICorner top corners via full UICorner plus a Frame masking the bottom, or pill style
    Label, UIStroke, UIGradient
```

Depict one tab as selected (accent fill, brighter text, optional underline Frame) and the rest as unselected (panel-alt, muted text). Equal-height buttons so scaled text matches.

## Toast / notification

```
ToastGui (ScreenGui)
  ToastStack (Frame)      AnchorPoint = (0.5,0), Position = {0.5,0},{0.04,0}, Size = {0.3,0},{0.3,0}, BackgroundTransparency = 1
    UIListLayout          vertical, Padding small, HorizontalAlignment Center
    Toast (Frame)         Size = {1,0},{0.2,0}, UICorner, UIStroke, UIGradient
      Icon, MessageLabel (UIListLayout horizontal)
```

Show one success toast and one error toast stacked so layout and color semantics get reviewed. Respect the top bar inset.

## Tooltip

Small Frame with UICorner, UIStroke, a TextLabel with padding, a tiny rotated square as the pointer. Anchor so the pointer lands on the target. Check it does not clip at the screen edge.

## HUD corner cluster

Each cluster hugs its corner:

```
TopLeftCluster (Frame)    AnchorPoint = (0,0), Position = {0.015,0},{0.02,0}, BackgroundTransparency = 1, Size set by contents
  UIListLayout            vertical, Padding 0.02
  CoinPill, GemPill, LevelBadge
BottomCenterBar (Frame)   AnchorPoint = (0.5,1), Position = {0.5,0},{0.97,0}
  UIListLayout            horizontal, Padding 0.015, HorizontalAlignment Center
  AbilityButton x4
```

Keep gameplay HUD out of the thumbstick (bottom-left) and jump (bottom-right) zones on mobile. Check the top-left against the chat button and top bar.

## Text hierarchy block

```
InfoBlock (Frame)         UIListLayout vertical, Padding 0.02
  OverlineLabel           caption size, text-muted, letter-spaced or uppercase
  TitleLabel              title size, text
  BodyLabel               body size, text-muted, TextWrapped = true, TextXAlignment Left
```

Use color and size to separate levels, not just size.

## Building states without scripts

Hover, press, disabled, and selected states need scripting to animate. You still own how they look, and you must depict and verify them.

1. **Free behavior:** `AutoButtonColor = true` on buttons gives built-in darken on hover and press with no scripts. Use it when custom art is not needed.
2. **State sheet:** build a separate ScreenGui named `_StateSheet` (Enabled = false when finished) with each state of each interactive element laid out side by side: Normal, Hover, Pressed, Disabled, Selected, plus Empty, Loading, Error for containers that need them. Screenshot it with Enabled = true, critique it like any screen, then disable it.
3. **Naming for later wiring:** name state variants predictably (`PlayButton_Hover` style) and keep their property differences minimal (color, offset, scale) so a scripter can tween between them.
4. **Do not fake interactivity with scripts.** Report that animation and logic wiring is a separate scripting task and list exactly what it needs to read (names, property targets).

Typical state differences:

- Hover: accent-light fill, +3 percent brightness, slight upward lip change.
- Pressed: accent-dark fill, lip reduced or button shifted down by the lip height.
- Disabled: desaturated fill, text-muted label, no stroke accent, no gradient.
- Selected: accent fill plus an outer stroke or underline, label at full contrast.
- Empty: panel-alt background, muted icon, helper text centered.
- Loading: placeholder bars at reduced contrast.
- Error: danger-colored stroke and message label.
