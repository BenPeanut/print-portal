# Extension Order Popup Parity Fix Notes

## Goal
Make the Q-triggered extension popup match the website order popup behavior and appearance as closely as possible, while preserving extension auth/session transport.

## Canonical Source Used
- `templates/browse_models.html`:
  - modal markup: `#featured-order-modal`
  - modal CSS blocks: `.modal`, `.config-modal-content`, `.profile-pill`, `.swatch-btn`, etc.
  - modal logic functions: profile normalization, profile/parts rendering, availability checks, confirm flow

## Files Changed
- `makerworld_capture_extension/overlay.html`
- `makerworld_capture_extension/overlay.css`
- `makerworld_capture_extension/overlay.js`

## What Was Replaced
1. Replaced extension-specific overlay layout (dark panel, custom header, text-input parts) with the same website modal structure:
   - `#featured-order-modal`
   - `#featured-modal-title`, `#featured-modal-description`, `#featured-modal-price`
   - `#featured-modal-profile-pills`
   - `#featured-modal-parts`
   - `#featured-modal-cancel`, `#featured-modal-confirm`

2. Replaced extension styling with website modal styling:
   - same visual language for profile pills
   - same swatch grid and unavailable red-X marker treatment
   - same section spacing and button style patterns

3. Replaced extension JS behavior with website modal logic pattern:
   - profile normalization/dedupe
   - part derivation per profile
   - image override by profile customization
   - availability checks combining manual rules + stock/remaining grams
   - confirm button validation and payload shaping

## Extension-Specific Adapters (Necessary Differences)
These are required for extension context and are not parity regressions:
1. Data load source:
   - Uses `/extension-api/hover-order-item` to get modal item data for hovered model
   - Uses `/extension-api/app-data` to get filament catalog for swatches
2. Auth transport:
   - sends `X-Extension-Auth` header when token exists
   - includes `ext_auth` in request body for `/create_featured_order`
3. Close/success channel:
   - posts message to parent frame (`mw-extension-overlay`) to close host overlay and show toast
4. Cart drawer handoff:
   - website calls `window.addToCart` and opens website drawer
   - extension cannot directly use website drawer context, so it confirms server order and closes overlay with success toast

## Regression Checklist
1. Q opens in-page popup (not new tab).
2. Popup layout matches website modal structure and style.
3. Profile pills appear for <=4 profiles; select appears for >4 profiles.
4. Part colors render as swatches (not text fields).
5. Unavailable swatches are disabled and show red-X marker.
6. Price updates when profile changes.
7. Confirm validates all parts have selected available filament.
8. Confirm posts order successfully to `/create_featured_order`.
9. Popup closes on Cancel, backdrop click, Escape, and success.
10. Extension login token remains accepted without forcing re-login each Q run.

## If This Breaks Again
Compare these files side-by-side before changing logic:
- `templates/browse_models.html`
- `makerworld_capture_extension/overlay.html`
- `makerworld_capture_extension/overlay.css`
- `makerworld_capture_extension/overlay.js`

If mismatch is intentional, add a note in this file under "Extension-Specific Adapters".
