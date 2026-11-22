//! Pure `WH_KEYBOARD_LL` + `WH_MOUSE_LL` hotkey listener (`src/hotkey.py`).
//!
//! Invariant: Read-only against the game window — strictly ZERO `SendInput`,
//! suppress trigger VK only (never modifiers), gated by foreground game/HUD window.
