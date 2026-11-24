"""
Verification suite for Track 1C (Genshin Helper v5):
1. Desktop bilingual VI/EN locale bundles (assets/locales/{vi,en}.json)
2. Extension bilingual VI/EN i18n (extension/i18n.js, extension/locales/{vi,en}.json)
   + static key coverage audit across popup.html, popup.js, page_main.js
3. Extension manifest pinned RSA key/ID & popup VI|EN toggle invariants
4. page_main.js in-place dual-rotation player marker (--gh-dir, --gh-rot, unwrapAngle),
   drag/touch auto-unpin, Safe Compact HoYoLAB Web UI + SPA safety guard, and window.__GH_BRIDGE__
5. JavaScript syntax + runtime behavior checks via Node.js (when available)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_EXT_ID = "nfkijdoblmphmdljfogooajhbldkeojh"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")


def _read_text(rel_path: str) -> str:
    full = os.path.join(PROJECT_ROOT, rel_path)
    with open(full, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_json(rel_path: str) -> dict:
    return json.loads(_read_text(rel_path))


def _assert_locale_pair(label: str, vi_path: str, en_path: str) -> tuple[dict, dict]:
    vi = _read_json(vi_path)
    en = _read_json(en_path)
    assert isinstance(vi, dict) and isinstance(en, dict), f"{label}: locale files must be JSON objects"
    assert len(vi) > 0 and len(en) > 0, f"{label}: locale files must not be empty"

    vi_keys = set(vi.keys())
    en_keys = set(en.keys())
    missing_in_en = sorted(vi_keys - en_keys)
    missing_in_vi = sorted(en_keys - vi_keys)
    assert not missing_in_en, f"{label}: keys missing in en.json: {missing_in_en}"
    assert not missing_in_vi, f"{label}: keys missing in vi.json: {missing_in_vi}"

    for key in sorted(vi_keys):
        v_val = vi[key]
        e_val = en[key]
        assert isinstance(v_val, str) and v_val.strip() != "", f"{label}: empty vi value for '{key}'"
        assert isinstance(e_val, str) and e_val.strip() != "", f"{label}: empty en value for '{key}'"
        v_ph = set(PLACEHOLDER_RE.findall(v_val))
        e_ph = set(PLACEHOLDER_RE.findall(e_val))
        assert v_ph == e_ph, f"{label}: placeholder mismatch on '{key}': vi={v_ph} vs en={e_ph}"

    print(f"  [OK] {label} parity & non-empty check passed ({len(vi_keys)} keys)")
    return vi, en


def verify_desktop_i18n() -> None:
    vi, en = _assert_locale_pair("Desktop locales", "assets/locales/vi.json", "assets/locales/en.json")

    # Domain coverage verification
    required_prefixes = (
        "tray.",
        "hud.state.tracking",
        "hud.state.arrived",
        "hud.state.floor_mismatch",
        "hud.state.lost",
        "hud.state.reacquiring",
        "toast.",
        "uipi.",
        "borderless.",
        "hdr.",
        "lan.",
        "export.",
    )
    for prefix in required_prefixes:
        assert any(k == prefix or k.startswith(prefix) for k in vi), f"Missing desktop i18n coverage for '{prefix}'"

    assert vi["hud.state.floor_mismatch"].format(player_floor="G", target_floor="B1") == "Khác tầng! (G → B1)"
    assert en["hud.state.floor_mismatch"].format(player_floor="G", target_floor="B1") == "Floor mismatch! (G → B1)"
    assert en["lan.enabled"].format(url="http://192.168.1.10:8765") == "LAN Companion enabled at http://192.168.1.10:8765"
    assert vi["export.success"].format(count=42, path="out.json") == "Đã xuất 42 điểm sang out.json"

    print("  [OK] assets/locales/{vi,en}.json domain coverage & interpolation passed")


def verify_extension_i18n_and_manifest() -> None:
    ext_vi, ext_en = _assert_locale_pair(
        "Extension locales", "extension/locales/vi.json", "extension/locales/en.json"
    )

    manifest = _read_json("extension/manifest.json")
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "5.6.0"
    assert "nativeMessaging" in manifest.get("permissions", [])
    assert "scripting" in manifest.get("permissions", [])
    assert manifest.get("key"), "Missing pinned RSA key in extension/manifest.json"
    der = base64.b64decode(manifest["key"])
    ext_id = "".join(chr(97 + (b >> 4)) + chr(97 + (b & 15)) for b in hashlib.sha256(der).digest()[:16])
    assert ext_id == EXPECTED_EXT_ID, f"Pinned extension ID changed: got {ext_id}, expected {EXPECTED_EXT_ID}"

    # Verify i18n wiring: bridge in the isolated world, i18n.js before page_main.js in MAIN.
    cs = manifest.get("content_scripts", [])
    assert any("i18n_bridge.js" in entry.get("js", []) and entry.get("world") != "MAIN" for entry in cs), (
        "i18n_bridge.js missing from isolated content_script"
    )
    assert any(
        entry.get("world") == "MAIN"
        and "i18n.js" in entry.get("js", [])
        and "page_main.js" in entry.get("js", [])
        and entry["js"].index("i18n.js") < entry["js"].index("page_main.js")
        for entry in cs
    ), "i18n.js must precede page_main.js in the MAIN world content_script"
    # Chromium injects each content-script file at most once per frame across ALL entries and
    # worlds: a file listed twice silently runs in only one world (this left window.GHI18n
    # undefined in the page and every page.* label showing its raw key).
    cs_files = [f for entry in cs for f in entry.get("js", [])]
    dup_files = sorted({f for f in cs_files if cs_files.count(f) > 1})
    assert not dup_files, f"content_scripts list the same file more than once: {dup_files}"
    war = manifest.get("web_accessible_resources", [])
    war_files = {r for entry in war for r in entry.get("resources", [])}
    assert "locales/vi.json" in war_files and "locales/en.json" in war_files

    # Verify popup.html structure & key coverage
    popup_html = _read_text("extension/popup.html")
    assert 'src="i18n.js"' in popup_html
    assert popup_html.index('src="i18n.js"') < popup_html.index('src="popup.js"'), "i18n.js must load before popup.js"
    for required_id in (
        "btn-lang-toggle",
        "btn-power-toggle",
        "btn-trigger-diag",
        "tab-btn-control",
        "tab-btn-settings",
        "toggle-auto-sync",
        "toggle-compact-ui",
        "btn-open-map",
        "btn-switch-game",
        "btn-save-hotkeys",
        "btn-hard-quit",
        "btn-donate-toggle",
        "donate-drawer",
    ):
        assert f'id="{required_id}"' in popup_html, f"Missing required element id='{required_id}' in popup.html"
    assert os.path.isfile(os.path.join(PROJECT_ROOT, "extension", "icons", "momo_qr.jpg")), "Missing extension/icons/momo_qr.jpg"

    html_keys = set(re.findall(r'data-(?:i18n(?:-[a-z]+)?|label-i18n)="([^"]+)"', popup_html))
    assert len(html_keys) >= 20, "Expected data-i18n attributes across popup.html"
    missing_html = sorted(html_keys - set(ext_en.keys()))
    assert not missing_html, f"Undefined i18n keys referenced in popup.html: {missing_html}"

    popup_js = _read_text("extension/popup.js")
    assert "GHI18n" in popup_js and "btn-lang-toggle" in popup_js
    assert "/set_language" in popup_js
    js_keys = set(re.findall(r"\bt\(\s*'([a-z0-9_.]+)'", popup_js))
    missing_popup_js = sorted(js_keys - set(ext_en.keys()))
    assert not missing_popup_js, f"Undefined i18n keys referenced in popup.js: {missing_popup_js}"

    page_main = _read_text("extension/page_main.js")
    page_keys = set(re.findall(r"\btr\(\s*'([a-z0-9_.]+)'", page_main))
    assert len(page_keys) >= 20, "Expected localized tr() calls in page_main.js"
    missing_page_js = sorted(page_keys - set(ext_en.keys()))
    assert not missing_page_js, f"Undefined i18n keys referenced in page_main.js: {missing_page_js}"

    # Verify zero eval() in all extension JS files
    ext_dir = os.path.join(PROJECT_ROOT, "extension")
    for fn in os.listdir(ext_dir):
        if fn.endswith(".js"):
            txt = _read_text(f"extension/{fn}")
            assert "eval(" not in txt, f"Disallowed eval() in extension/{fn}"

    print(
        f"  [OK] Extension manifest ({ext_id}), popup.html/popup.js/page_main.js key coverage "
        f"({len(html_keys | js_keys | page_keys)} unique keys verified)"
    )


def verify_page_main_v5_upgrades() -> None:
    src = _read_text("extension/page_main.js")

    # 1. Preserve v4.5.0 clean-surface invariants
    for banned in ('id="gh-card"', 'id="gh-pill"', "id='gh-card'", "id='gh-pill'", "gh-settings-modal"):
        assert banned not in src, f"Banned legacy overlay identifier '{banned}' found in page_main.js"
    for required in ("gh-mini-toast", "updatePlayerMarker", "showDiagnosticNoteModal", "ENGINE_STATE_UPDATED"):
        assert required in src, f"Missing required v4.5 symbol '{required}' in page_main.js"

    # 2. In-place dual-rotation player marker (--gh-dir, --gh-rot, unwrapAngle, no setIcon/innerHTML thrashing)
    for token in (
        "--gh-dir",
        "--gh-rot",
        "function unwrapAngle(",
        "gh-player-fov-svg",
        "gh-has-fov",
        "camera_heading",
        "M16 4.5 L25 24 L16 19.5 L7 24 Z",
        "translate3d(${(pt.x - 22).toFixed(2)}px, ${(pt.y - 22).toFixed(2)}px, 0)",
        "setLatLng(",
    ):
        assert token in src, f"Missing dual-rotation marker feature '{token}' in page_main.js"

    assert "setIcon(" not in src, "page_main.js MUST NOT call setIcon() on player marker updates"
    assert "playerDomMarkerEl.innerHTML" not in src, "page_main.js MUST NOT overwrite playerDomMarkerEl.innerHTML"

    # 3. Drag / Touch auto-unpin (> 100ms AND > 20px) + re-center button
    for token in (
        "bindDragUnpinListeners",
        "'mousedown'",
        "'touchstart'",
        "'mousemove'",
        "'touchmove'",
        "heldMs > 100",
        "distPx > 20",
        "gh-recenter-btn",
        "recenterToPlayer",
    ):
        assert token in src, f"Missing drag auto-unpin feature '{token}' in page_main.js"

    # 4. Safe Compact HoYoLAB Web UI & CRITICAL SPA SAFETY GUARD
    for selector in (
        "body.gh-compact-ui .mhy-header",
        "body.gh-compact-ui .hoyolab-header",
        "body.gh-compact-ui .mhy-hoyolab-app-header",
        "body.gh-compact-ui .map-notice-bar",
        "body.gh-compact-ui .bbs-qr",
        'body.gh-compact-ui [class*="bbs-qr"]',
        "body.gh-compact-ui .mihoyo-cookie-tips",
        "body.gh-compact-ui .strong-tip-closable",
        "body.gh-compact-ui .driver-popover",
        "body.gh-compact-ui .filter-panel",
        "body.gh-compact-ui .gh-compact-sidebar",
        "body.gh-compact-ui .gh-compact-hide",
        "function applyCompactDomTags(",
        "function isProtectedSpaNode(",
        '.gt-modal[data-modal-visible-count="0"]',
        ".map-loading-mask",
    ):
        assert selector in src, f"Missing Compact UI / safety guard selector '{selector}' in page_main.js"

    bg_src = _read_text("extension/background.js")
    assert "function reinjectOpenMapTabs(" in bg_src, "Missing reinjectOpenMapTabs in background.js"
    assert "RECONNECT_MAP_TABS" in bg_src, "Missing RECONNECT_MAP_TABS handler in background.js"
    assert "GET_I18N_BUNDLES" in bg_src, "Missing Service Worker GET_I18N_BUNDLES handler in background.js"
    bridge_src = _read_text("extension/i18n_bridge.js")
    assert "GET_I18N_BUNDLES" in bridge_src, "i18n_bridge.js must request bundles via Service Worker for Brave Shields resilience"
    assert "files: ['content_bridge.js', 'i18n_bridge.js']" in bg_src, "SW re-injection must use the isolated bridge file"
    assert "_refreshMarkedPinIds" in src and "mark_map_point_list" in src, "page_main.js must sync authoritative marked pins"

    # Extract style.textContent CSS and verify NO display:none on protected SPA selectors
    css_match = re.search(r"style\.textContent\s*=\s*`([\s\S]*?)`;", src)
    assert css_match, "Could not locate injectThemeStyles CSS block in page_main.js"
    css_body = re.sub(r"/\*[\s\S]*?\*/", "", css_match.group(1))
    css_rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css_body)
    protected_selectors = (".gt-modal", ".gt-popup-root", ".map-loading-mask")
    found_gt_modal_guard = False
    found_loading_mask_guard = False

    for sel_raw, decls in css_rules:
        sel = " ".join(sel_raw.split())
        decls_norm = re.sub(r"\s+", "", decls.lower())
        for prot in protected_selectors:
            if prot in sel:
                assert "display:none" not in decls_norm, (
                    f"CRITICAL SPA SAFETY VIOLATION: '{prot}' must NEVER have display:none (rule: {sel})"
                )
        if '.gt-modal[data-modal-visible-count="0"]' in sel and "pointer-events:none!important" in decls_norm:
            found_gt_modal_guard = True
        if ".map-loading-mask" in sel and "pointer-events:none!important" in decls_norm:
            found_loading_mask_guard = True

    assert found_gt_modal_guard, "Missing pointer-events:none !important guard on .gt-modal[data-modal-visible-count=\"0\"]"
    assert found_loading_mask_guard, "Missing pointer-events:none !important guard on .map-loading-mask"

    # Ensure JS never calls .remove() on protected SPA elements
    for prot in protected_selectors:
        pattern = re.compile(re.escape(prot) + r"[^\n;]{0,120}\.remove\s*\(")
        assert not pattern.search(src), f"CRITICAL SPA SAFETY VIOLATION: JS must never .remove() '{prot}'"

    # 5. Wave 4 integration bridge on window.__GH_BRIDGE__
    for bridge_prop in (
        "window.__GH_BRIDGE__",
        "queryNearbyUncollectedPins",
        "setLanguage:",
        "setCompactUI:",
        "toggleCompactUI:",
        "recenter:",
        "unwrapAngle",
    ):
        assert bridge_prop in src, f"Missing '{bridge_prop}' on window.__GH_BRIDGE__ in page_main.js"

    print("  [OK] page_main.js dual-rotation marker, drag auto-unpin, safe Compact UI & __GH_BRIDGE__ passed")


def verify_js_with_node() -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        print("  [SKIP] node not found on PATH; skipping JS syntax & runtime check")
        return

    js_files = [
        "extension/i18n.js",
        "extension/i18n_bridge.js",
        "extension/popup.js",
        "extension/page_main.js",
        "extension/content_bridge.js",
        "extension/background.js",
        "extension/route_hook.js",
    ]
    for rel in js_files:
        full = os.path.join(PROJECT_ROOT, rel)
        res = subprocess.run([node_bin, "--check", full], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"node --check failed on {rel}:\n{res.stderr}"

    # Runtime verification of extension/i18n.js & unwrapAngle continuous wrapping via Node
    node_script = r"""
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const root = process.argv[1];
const { resolveLanguage, createI18n, EMBEDDED_PAGE_BUNDLES } = require(path.join(root, 'extension', 'i18n.js'));

assert.strictEqual(resolveLanguage('vi'), 'vi');
assert.strictEqual(resolveLanguage('en-US'), 'en');

const viJson = JSON.parse(fs.readFileSync(path.join(root, 'extension', 'locales', 'vi.json'), 'utf8'));
const enJson = JSON.parse(fs.readFileSync(path.join(root, 'extension', 'locales', 'en.json'), 'utf8'));

assert(EMBEDDED_PAGE_BUNDLES && EMBEDDED_PAGE_BUNDLES.vi && EMBEDDED_PAGE_BUNDLES.en, 'Missing EMBEDDED_PAGE_BUNDLES');
const pageSrc = fs.readFileSync(path.join(root, 'extension', 'page_main.js'), 'utf8');
const usedPageKeys = Array.from(new Set(Array.from(pageSrc.matchAll(/\btr\(\s*['"]([a-z0-9_.]+)['"]/g), (m) => m[1])));
assert(usedPageKeys.length >= 20, 'Expected page tr() keys');
for (const k of usedPageKeys) {
  assert.strictEqual(EMBEDDED_PAGE_BUNDLES.vi[k], viJson[k], `EMBEDDED_PAGE_BUNDLES.vi mismatch for ${k}`);
  assert.strictEqual(EMBEDDED_PAGE_BUNDLES.en[k], enJson[k], `EMBEDDED_PAGE_BUNDLES.en mismatch for ${k}`);
}
const inst = createI18n({
  vi: { ...viJson, 'test.empty': '', 'test.blank': '   ' },
  en: { ...enJson, 'test.empty': 'EN fallback', 'test.blank': 'EN blank fallback' }
}, 'vi');

assert.strictEqual(inst.t('test.empty'), 'EN fallback');
assert.strictEqual(inst.t('test.blank'), 'EN blank fallback');
assert.strictEqual(inst.t('test.unknown.key'), 'test.unknown.key');
assert.strictEqual(inst.t('popup.footer.map_tabs', { count: 2 }), 'Tab Map đang kết nối: 2');
inst.setLangSync('en');
assert.strictEqual(inst.t('popup.footer.map_tabs', { count: 2 }), 'Connected Map tabs: 2');

// Extract unwrapAngle from page_main.js and test 0/360 continuity
const m = pageSrc.match(/function unwrapAngle\([\s\S]*?\n    \}/);
assert(m, 'unwrapAngle function not found');
const unwrapAngle = new Function(m[0] + '; return unwrapAngle;')();

assert.strictEqual(unwrapAngle(null, 359), 359);
assert.strictEqual(unwrapAngle(359, 1), 361);
assert.strictEqual(unwrapAngle(361, 2), 362);
assert.strictEqual(unwrapAngle(1, 359), -1);
assert.strictEqual(unwrapAngle(-1, 358), -2);
assert.strictEqual(unwrapAngle(-719, 359), -721);
console.log('NODE_RUNTIME_OK');
"""
    res = subprocess.run([node_bin, "-e", node_script, PROJECT_ROOT], capture_output=True, text=True, check=False)
    assert res.returncode == 0 and "NODE_RUNTIME_OK" in res.stdout, (
        f"Node runtime verification failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )

    print(f"  [OK] node --check ({len(js_files)} JS files) + Node runtime checks (i18n.js & unwrapAngle) passed")


def main() -> None:
    print("[Track 1C Verification] Running bilingual i18n + Extension v5 checks...")
    verify_desktop_i18n()
    verify_extension_i18n_and_manifest()
    verify_page_main_v5_upgrades()
    verify_js_with_node()
    print("ALL TRACK 1C I18N & EXTENSION V5 CHECKS PASSED!")


if __name__ == "__main__":
    main()
