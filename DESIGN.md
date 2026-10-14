# Morphe Builder UI direction

Neo-brutalism, applied to the existing Jinja and Alpine admin. Loud, visible
structure: if it has no border it does not exist. Preserve the pipeline, discovery
and settings workflows; add no controls, icons, images or network assets.

## Tokens

- Light (default): canvas `#FFFDF5`, ink `#000` for all text, borders and shadows,
  white `#FFF` surfaces. Red `#FF6B6B` primary, yellow `#FFD93D` secondary and
  hover/focus, violet `#C4B5FD` card headers and notices. No grays.
- Dark (follows `prefers-color-scheme`, no toggle): canvas `#121212`, surface
  `#1E1E1E`, ink `#FFFDF5` for text, borders and shadows. Same accents. Text on any
  red, yellow or violet fill is `#000`.
- Contrast (WCAG 2.x): ink on canvas 20.6:1 light, 18.4:1 dark; black on red 7.6,
  yellow 15.2, violet 11.4; cream on `#1E1E1E` 16.4. Placeholder is ink at 65%
  (the source spec's 40% fails AA). Disabled controls are exempt and use opacity.
- Variables live in `tailwind/input.css`; Tailwind names them `ink`, `paper`,
  `canvas`, `pop-red`, `pop-yellow`, `pop-violet`.

## Type

Space Grotesk, vendored in `src/static/fonts/` (weights 500 and 700, latin, SIL OFL,
`font-display: swap`). Body, labels and buttons 700; 500 for secondary text only,
never 400. The family has no 900, so `font-black` renders the 700 face. Uppercase
for headings, labels, buttons, tabs and badges; tight tracking on headings, wide on
labels and badges.

## Shape, borders, shadows

- Radius 0. Borders 4px by default; 2px for separators, ghost buttons and badges; 8px
  for the header rule.
- Hard offset shadows, no blur: 4px small, 8px cards (6px below 768px), 12px dialogs
  (8px below 768px). Shadows use ink, so they turn cream in dark.
- No blur, gradients or alpha fills. The only alpha is the modal scrim and the page
  grid texture (ink at 10%, 40px), which sits on the canvas and never behind data.

## Components

Button: 48px minimum, uppercase bold; primary red, default outline, ghost with a
transparent border that fills yellow on hover; press moves 2px and drops the shadow.
Card: surface, 4px border, 8px shadow, violet `nb-head`, yellow `nb-foot`; pipeline
cards lift 4px on hover. Input: 56px, bold, focus turns yellow with a 4px shadow.
Navigation: bordered tabs, active and hover red with a shadow. Badge: bordered,
uppercase. Alerts: error red, warning yellow, success violet, info outlined.

## Layout and motion

7xl content width, gaps 24 to 40px. Rotation (1 to 3 degrees) only on the logo, the
Active Pipeline heading and badges; tables, forms, patch lists and dialogs stay
straight. Motion is 100 to 200ms, linear or ease-out, no loops except the existing
spinners. `prefers-reduced-motion` removes transitions and hover/press transforms.

## Accessibility

3px ink focus outline with 2px offset (black inside yellow and violet regions);
44px minimum targets; layouts reflow below 768px; status is never colour-only.

## Dials

ENERGY 3 / RHYTHM 2 / MOTION 1.

## Reasons

- Neo-brutalism is the owner's chosen identity; the admin keeps its hierarchy and
  gains structure from borders and colour blocking.
- Red is the one primary action colour; yellow marks hover, focus and selection;
  violet marks headers.
- The grid texture is the paper-canvas motif the spec asks for, kept off dense data.
- Space Grotesk is the spec's face, vendored so the page needs no network.
