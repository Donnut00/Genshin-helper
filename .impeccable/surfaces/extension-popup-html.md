---
version: 1
slug: "extension-popup-html"
primary_target: "extension/popup.html"
related_targets: ["extension/popup.css","extension/popup.js"]
---

# Surface Brief: Extension Control Center Popup

- **Target:** `extension/popup.html` (`extension/popup.css`, `extension/popup.js`, `extension/locales/vi.json`, `extension/locales/en.json`)
- **Mode:** Operate (code-led)
- **Audience & Job:** Windows PC Genshin Impact players opening the Chrome/Edge/Brave toolbar popup mid-session for 2–3 seconds to control engine power, check live sync coordinates, toggle Mini-HUD/audio/display options, trigger tools (TSP, Export, LAN QR, Borderless), or manage hotkeys without leaving the HoYoLAB Interactive Map tab.
- **Constraints:** Fixed `520px` width, `<= 580px` height (`0-scroll` on Tab 1 and Tab 2), sticky header + sticky footer keeping connection status and voluntary Donate trigger permanently visible, 100% offline MV3 (zero CDN/eval/new permissions), strict `VI | EN` parity, all existing DOM IDs and `verify_i18n_and_ext_v5.py` invariants preserved.

## Direction contract

THESIS: A nocturnal 520×580px Celestial Observatory command deck that ends popup scrolling by pairing a unified Engine + Live Telemetry Horizon Strip with a balanced 2-column control grid and a permanently docked brass-accented Support footer—refusing the narrow 348px single-column scrolling modal and emoji-heavy dark SaaS default.

OWN-WORLD: Midnight-indigo observatory ground (`#0B0F19`), layered deep-slate panels (`#121826`), inset wells (`#0E131F`), hairline `#232D42` dividers, gilt-brass (`#D9A441` / `#F3D28B`) strictly for active tab selection, horizon framing, interactive utility chips, and the voluntary Support pill, celestial sky (`#38BDF8` / `#0284C7`) for primary CTA and active switches, semantic emerald/amber/crimson only for live status, and a single crisp 1.75px-stroke inline SVG icon grammar.

STORY: Players instantly verify Engine state (`Active` / `Standby` / `Offline`), current Teyvat region/floor/coordinates, and map-tab connection at first glance, toggle any tracking, HUD, audio, or display setting in one click without scrolling, and always see the voluntary Support trigger anchored at the bottom edge.

FIRST VIEWPORT: Fixed `520×580px` flexbox shell (`overflow: hidden`): 52px observatory header (`icon48.png` in brass-framed tile + `Genshin Helper v5.4.0` + `VI|EN` pill + live status pill), 38px 3-tab bar (`Điều khiển · Công cụ · Phím tắt`), full-width Celestial Horizon Strip uniting Engine Power switch (left) and live region/floor/coordinate telemetry (right), 2-column Control Deck (Tracking & Mini-HUD with grouped child sub-panel on left; Audio chimes + volume slider, Display modes, and primary `Mở HoYoLAB Map` + `Chuyển về Game` buttons on right), and a 40px sticky bottom footer anchoring Map Tab count and the gilt-brass `Ủng hộ` button (which opens an anchored slide-up Donate popover above the footer without shifting layout).

FORM: Celestial Observatory Planisphere (Candidate 1 — Impeccable’s Pick, matching `.impeccable/mocks/decision/model-pick.html`), `seed:af704f4ee36d87f9`.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
