# Morphe Builder UI direction

## Product surface

Restyle the existing Jinja and Alpine admin application. Preserve its current
pipeline, discovery and settings workflows, existing cards, dialogs and controls.
Do not add landing-page sections, new controls, images, icons, fonts or network
assets.

## Visual direction

- Keep the existing dark theme and system sans typography.
- Keep content cards solid and neutral; use a restrained frosted treatment only
  on the sticky header and the currently visible dialog surface.
- Use blue as the sole brand accent and neutral text. The supplied blue
  `#0080FF` is adjusted to lighter or darker blue shades where needed for text
  and control contrast. Do not use the supplied purple or pink as accents.
- Use compact 4px and 8px radii, an 8px spacing rhythm and a 1280px content
  maximum width.
- Use 2.25rem as the page-heading reference and 1rem as the body-text reference;
  retain smaller sizes for dense admin labels where needed for hierarchy.
- Use opaque dark surfaces when backdrop blur is unsupported.

## Accessibility and motion

- Maintain WCAG AA text contrast and visible keyboard focus.
- Interactive controls have at least 44px targets; layouts reflow below 768px.
- Restrict motion to brief hover and focus feedback, and honor reduced-motion
  preferences.

## Design dials

ENERGY 2 / RHYTHM 2 / MOTION 1.

## Reasons

- The existing dark theme and solid cards preserve the admin tool's familiar
  information hierarchy; glass is reserved for navigation and the active dialog
  to distinguish transient surfaces without flattening content.
- Blue is the only accent because the owner selected blue-only accents; its
  contrast-safe variants preserve that choice without sacrificing readability.
- Compact radii, readable neutral text and restrained transitions keep dense
  administrative tasks legible and predictable.
