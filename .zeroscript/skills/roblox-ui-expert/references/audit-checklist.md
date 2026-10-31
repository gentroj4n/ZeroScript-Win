# Final Audit Checklist

Walk the instance tree and check each line. Fix failures, then re-screenshot. Report the result in the final summary. Do not tick a line you did not actually verify.

## Structure

- [ ] All UI is under `StarterGui > ScreenGui` (or intentionally separate ScreenGuis such as a modal layer with higher DisplayOrder).
- [ ] No Script, LocalScript, or ModuleScript exists in the place from this work.
- [ ] Each ScreenGui has a clear name, deliberate `ResetOnSpawn`, `ZIndexBehavior = Sibling`, deliberate inset behavior, and sensible `DisplayOrder`.
- [ ] Hierarchy is logical: containers group related elements, no deeply nested pointless wrappers, no orphaned elements.

## Naming

- [ ] Every instance has a role-based PascalCase name.
- [ ] No default names (`Frame`, `TextLabel`, `Frame2`, `ImageLabel`) remain.
- [ ] Layout objects and constraints may keep default class names, but any ambiguity is resolved by naming.
- [ ] No `_Debug`, `_ViewportSim`, or other temporary helpers remain. `_StateSheet` is present only if intended and set to `Enabled = false`.

## Layout

- [ ] No Offset-based Size or Position except hairline details, and each exception is justified.
- [ ] Repeated elements use layout objects, not hand-set positions.
- [ ] Containers that grow with content use AutomaticSize or AutomaticCanvasSize correctly.
- [ ] Shapes that must hold proportion have UIAspectRatioConstraint. Windows and large panels have UISizeConstraint caps.
- [ ] Every AnchorPoint and Position pair is deliberate and consistent.
- [ ] ScrollingFrames have padding at start and end, styled scrollbars, and no clipped last item.
- [ ] No layout object fights manual positioning on its children.

## Style

- [ ] Palette matches the tokens. No stray colors, no default grey surfaces.
- [ ] Fonts match tokens. No more than two families.
- [ ] Radius is consistent per element class. Nested corners are concentric.
- [ ] Strokes are consistent in thickness and color logic.
- [ ] Text has sufficient contrast against its background.
- [ ] Type hierarchy is clear. Size steps are visibly distinct.
- [ ] Depth treatments (shadows, lips, highlights) are consistent and sit behind the elements they belong to.
- [ ] Icons share a consistent box and visual weight. Placeholders are named and listed.
- [ ] No unverified asset IDs.

## Text

- [ ] No text clipped, shrunk below legibility, or overflowing (TextBounds checked against AbsoluteSize on small devices).
- [ ] Sibling labels that should match have equal-height boxes.
- [ ] UITextSizeConstraint present on scaled text, with sane min and max.
- [ ] Realistic sample content used, including long names and large numbers.

## Draw order

- [ ] No overlap bugs: decorative layers are not hidden behind panels, popups sit above content.
- [ ] ZIndex values are small and follow a simple scheme.
- [ ] Modals and overlays live in a higher DisplayOrder ScreenGui.

## Responsive

- [ ] Screenshots exist for every device in the matrix (or documented exclusions).
- [ ] No clipping, overlap, stretching, or unreadable text on any device.
- [ ] Tap targets at least about 44 px on the short side at phone size.
- [ ] Top bar, chat, thumbstick, and jump zones are clear on mobile where relevant.
- [ ] Grids and rows reflow sensibly on narrow screens.

## States

- [ ] Required states (hover, pressed, disabled, selected, empty, loading, error) are depicted and screenshot-reviewed, or explicitly out of scope.
- [ ] State variants are named predictably for later scripting.

## Fidelity

- [ ] Every element in the reference or spec is present.
- [ ] Layout, spacing rhythm, color, hierarchy, and detail level match the reference.
- [ ] The last full critique cycle found no major or blocker deltas and no measurable minor flaws worth fixing.

## Report

- [ ] Final report lists what was built, tokens, vision-driven changes, devices verified, compromises and placeholders, and what remains for scripting or assets.
