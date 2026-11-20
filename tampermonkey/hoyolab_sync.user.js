// ==UserScript==
// @name         HoYoLAB Genshin Helper (Auto Sync)
// @namespace    https://github.com/genshin-helper
// @version      4.3.0
// @description  Auto Map (M) Live Player Marker, Proportional Zoom & Exact Cave Layer Sync for HoYoLAB Interactive Map
// @match        *://act.hoyolab.com/*
// @match        https://act.hoyolab.com/*
// @grant        none
// @run-at       document-idle
// @updateURL    http://127.0.0.1:8765/hoyolab_sync.user.js
// @downloadURL  http://127.0.0.1:8765/hoyolab_sync.user.js
// ==/UserScript==

(function () {
    'use strict';

    if (!window.location.hostname.includes('hoyolab.com') || !window.location.pathname.includes('/interactive-map/')) {
        return;
    }

    const SCRIPT_VERSION = 'v4.3.0';

    // Auto Hot-Reload from local server during development (falls back cleanly to bundled copy)
    if (!window.__gh_hotloaded && window.location.hostname.includes('hoyolab.com')) {
        window.__gh_hotloaded = true;
        fetch('http://127.0.0.1:8765/hoyolab_sync.user.js?v=' + Date.now(), { cache: 'no-store' })
            .then(r => {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.text();
            })
            .then(code => {
                try {
                    (0, eval)(code);
                } catch (evalErr) {
                    console.warn('[Genshin Helper] Hot-reload eval fallback:', evalErr);
                    bootGenshinHelper();
                }
            })
            .catch(() => {
                bootGenshinHelper();
            });
        return;
    }

    function bootGenshinHelper() {
    if (window.__gh_booted_version === SCRIPT_VERSION) return;
    window.__gh_booted_version = SCRIPT_VERSION;

    const API_POLL_URL = 'http://127.0.0.1:8765/poll';
    const API_REPORT_URL = 'http://127.0.0.1:8765/report_state';
    const API_AUTO_SYNC_URL = 'http://127.0.0.1:8765/set_auto_sync';
    const API_SAVE_DIAG_NOTE_URL = 'http://127.0.0.1:8765/save_diagnostic_note';
    let autoPanEnabled = true;
    let autoSwitchLayerEnabled = true;
    let autoSyncMapMEnabled = true;
    let isConnected = false;
    let currentHotkeySyncDisplay = 'Alt+F11';
    let currentHotkeyDebugDisplay = 'Alt+F12';
    let playerMarkerLayer = null;
    let playerMarkerMapRef = null;
    let playerDomMarkerEl = null;
    let playerDomMapRef = null;
    let playerLastState = null;
    let layerTimers = [];
    let lastNoteModalFolder = '';
    let lastNoteModalTs = 0;
    let lastEventSeq = -1;
    try {
        const savedSync = localStorage.getItem('gh_hotkey_sync_display') || localStorage.getItem('gh_hotkey_display');
        if (savedSync) currentHotkeySyncDisplay = savedSync;
        const savedDebug = localStorage.getItem('gh_hotkey_debug_display');
        if (savedDebug) currentHotkeyDebugDisplay = savedDebug;
        const savedAutoM = localStorage.getItem('gh_auto_sync_m');
        if (savedAutoM === '0' || savedAutoM === 'false') autoSyncMapMEnabled = false;
    } catch(e) {}

    console.log('%c[Genshin Helper v4.3.0] Loaded (Multi-Tab Cursor + Fast Snap + Vuex AreaSync)', 'color: #38bdf8; font-size: 16px; font-weight: bold;');

    function setAutoSyncUI(enabled) {
        autoSyncMapMEnabled = !!enabled;
        try { localStorage.setItem('gh_auto_sync_m', autoSyncMapMEnabled ? '1' : '0'); } catch(e) {}
        const chk = document.getElementById('gh-auto-sync-toggle');
        if (chk) chk.checked = autoSyncMapMEnabled;
    }

    function clearLayerTimers() {
        for (const tid of layerTimers) clearTimeout(tid);
        layerTimers = [];
    }

    function scheduleLayerTimer(fn, delay) {
        const tid = setTimeout(() => {
            layerTimers = layerTimers.filter(t => t !== tid);
            fn();
        }, delay);
        layerTimers.push(tid);
        return tid;
    }

    // =========================================================================
    // 1. UI Panel & On-Screen Log
    // =========================================================================
    function setLog(msg, color) {
        console.log('[Genshin Helper v3.3]', msg);
        const logEl = document.getElementById('gh-log');
        if (logEl) {
            logEl.innerHTML = `<span style="color: ${color || '#38bdf8'}">${msg}</span>`;
        }
    }

    function updateHotkeyDisplay(displayName, action = 'sync') {
        if (!displayName) return;
        if (action === 'debug') {
            currentHotkeyDebugDisplay = displayName;
            try { localStorage.setItem('gh_hotkey_debug_display', displayName); } catch(e) {}
        } else {
            currentHotkeySyncDisplay = displayName;
            const badgeText = document.getElementById('gh-hotkey-text');
            const pillHotkey = document.getElementById('gh-pill-hotkey');
            if (badgeText) badgeText.textContent = displayName;
            if (pillHotkey) pillHotkey.textContent = displayName;
            try {
                localStorage.setItem('gh_hotkey_sync_display', displayName);
                localStorage.setItem('gh_hotkey_display', displayName);
            } catch(e) {}
        }
    }

    function toggleWidget(collapse) {
        const pill = document.getElementById('gh-pill');
        const card = document.getElementById('gh-card');
        if (!pill || !card) return;

        const isCurrentlyCollapsed = (card.style.display === 'none');
        const shouldCollapse = (collapse !== undefined) ? collapse : !isCurrentlyCollapsed;

        if (shouldCollapse) {
            card.style.display = 'none';
            pill.style.display = 'flex';
            try { localStorage.setItem('gh_widget_collapsed', '1'); } catch(e) {}
        } else {
            card.style.display = 'flex';
            pill.style.display = 'none';
            try { localStorage.setItem('gh_widget_collapsed', '0'); } catch(e) {}
        }
    }

    function injectThemeStyles() {
        if (document.getElementById('gh-theme-styles')) return;

        const style = document.createElement('style');
        style.id = 'gh-theme-styles';
        style.textContent = `
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap');

            .gh-root, .gh-root * {
                box-sizing: border-box;
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
                text-rendering: optimizeLegibility;
                font-variant-numeric: lining-nums tabular-nums !important;
                -webkit-font-feature-settings: "lnum" 1, "tnum" 1 !important;
                font-feature-settings: "lnum" 1, "tnum" 1 !important;
            }

            .gh-mono {
                font-family: 'JetBrains Mono', 'SF Mono', Consolas, Monaco, monospace !important;
                font-variant-numeric: tabular-nums lining-nums !important;
                letter-spacing: -0.01em;
            }

            /* --- Widget Header Buttons --- */
            .gh-btn-icon {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                color: #94a3b8;
                width: 26px;
                height: 26px;
                border-radius: 7px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 12px;
                line-height: 1;
                transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
            }
            .gh-btn-icon:hover {
                background: rgba(255, 255, 255, 0.22);
                color: #ffffff;
                border-color: rgba(255, 255, 255, 0.35);
                transform: translateY(-1px);
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
            }
            .gh-btn-icon:active {
                transform: translateY(0) scale(0.95);
            }

            .gh-btn-pinned {
                background: rgba(56, 189, 248, 0.2) !important;
                color: #38bdf8 !important;
                border-color: rgba(56, 189, 248, 0.5) !important;
                box-shadow: 0 0 10px rgba(56, 189, 248, 0.35);
            }
            .gh-btn-pinned:hover {
                background: rgba(56, 189, 248, 0.32) !important;
                color: #e0f2fe !important;
                border-color: rgba(56, 189, 248, 0.8) !important;
                box-shadow: 0 0 14px rgba(56, 189, 248, 0.6);
            }

            /* --- Hotkey Button in Card --- */
            #gh-hotkey-btn {
                transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
            }
            #gh-hotkey-btn:hover {
                background: rgba(56, 189, 248, 0.28) !important;
                border-color: rgba(56, 189, 248, 0.75) !important;
                color: #e0f2fe !important;
                transform: translateY(-1.5px);
                box-shadow: 0 4px 14px rgba(56, 189, 248, 0.35);
            }
            #gh-hotkey-btn:active {
                transform: translateY(0) scale(0.96);
            }

            /* --- Floating Pill --- */
            #gh-pill {
                transition: all 0.22s cubic-bezier(0.4, 0, 0.2, 1);
            }
            #gh-pill:hover {
                border-color: rgba(56, 189, 248, 0.75) !important;
                transform: translateY(-2px);
                box-shadow: 0 10px 28px rgba(56, 189, 248, 0.35);
            }
            #gh-pill-hotkey {
                transition: all 0.15s ease;
            }
            #gh-pill-hotkey:hover {
                background: rgba(56, 189, 248, 0.3) !important;
                border-color: rgba(56, 189, 248, 0.6) !important;
                color: #ffffff !important;
                transform: scale(1.05);
            }

            /* --- Modal Controls & Buttons --- */
            #gh-modal-close {
                transition: all 0.15s ease;
                border-radius: 6px;
                width: 26px;
                height: 26px;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            #gh-modal-close:hover {
                color: #ffffff !important;
                background: rgba(255, 255, 255, 0.16) !important;
                transform: scale(1.1);
            }

            .gh-preset-btn {
                display: inline-flex;
                align-items: center;
                gap: 5px;
                line-height: 1.2;
                font-variant-numeric: lining-nums tabular-nums !important;
                transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1) !important;
            }
            .gh-preset-btn:hover {
                background: rgba(56, 189, 248, 0.28) !important;
                border-color: rgba(56, 189, 248, 0.75) !important;
                color: #ffffff !important;
                transform: translateY(-1.5px);
                box-shadow: 0 4px 12px rgba(56, 189, 248, 0.25);
            }
            .gh-preset-btn:active {
                transform: translateY(0) scale(0.96);
            }

            #gh-key-recorder {
                font-variant-numeric: lining-nums tabular-nums !important;
                transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            }
            #gh-key-recorder:hover {
                border-color: rgba(56, 189, 248, 0.8) !important;
                background: rgba(56, 189, 248, 0.08) !important;
                box-shadow: 0 0 18px rgba(56, 189, 248, 0.25);
            }
            #gh-key-recorder:hover #gh-recorder-badge {
                background: rgba(56, 189, 248, 0.28);
                border-color: rgba(56, 189, 248, 0.6);
                color: #ffffff;
            }

            #gh-modal-cancel {
                transition: all 0.15s ease;
            }
            #gh-modal-cancel:hover {
                background: rgba(255, 255, 255, 0.16) !important;
                border-color: rgba(255, 255, 255, 0.3) !important;
                color: #ffffff !important;
                transform: translateY(-1px);
            }
            #gh-modal-cancel:active {
                transform: translateY(0);
            }

            #gh-modal-save {
                transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
            }
            #gh-modal-save:hover {
                background: #0ea5e9 !important;
                box-shadow: 0 4px 16px rgba(14, 165, 233, 0.5) !important;
                transform: translateY(-1.5px);
            }
            #gh-modal-save:active {
                transform: translateY(0) scale(0.97);
            }

            /* --- Live Directional Player Marker --- */
            @keyframes gh-player-pulse {
                0% { transform: translate(-50%, -50%) scale(0.72); opacity: 0.85; }
                75% { transform: translate(-50%, -50%) scale(1.65); opacity: 0; }
                100% { transform: translate(-50%, -50%) scale(1.65); opacity: 0; }
            }
            .gh-player-marker-wrap {
                position: relative;
                width: 44px;
                height: 44px;
                pointer-events: none;
            }
            .gh-player-pulse-ring {
                position: absolute;
                left: 50%;
                top: 50%;
                width: 34px;
                height: 34px;
                border-radius: 50%;
                border: 2px solid #38bdf8;
                background: rgba(56, 189, 248, 0.22);
                animation: gh-player-pulse 1.8s cubic-bezier(0.2, 0.6, 0.3, 1) infinite;
            }
            .gh-player-arrow-svg {
                position: absolute;
                left: 50%;
                top: 50%;
                width: 30px;
                height: 30px;
                filter: drop-shadow(0 2px 6px rgba(2, 132, 199, 0.85));
            }
        `;
        document.head.appendChild(style);
    }

    function showSettingsModal() {
        injectThemeStyles();
        const existing = document.getElementById('gh-settings-modal') || document.getElementById('gh-hotkey-modal');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = 'gh-settings-modal';
        overlay.className = 'gh-root';
        overlay.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.75); backdrop-filter: blur(12px);
            z-index: 2147483647; display: flex; align-items: center; justify-content: center;
            user-select: none;
        `;

        overlay.innerHTML = `
            <div style="background: rgba(15, 23, 42, 0.98); border: 1px solid rgba(56, 189, 248, 0.5); border-radius: 18px; padding: 22px 26px; max-width: 520px; width: 94%; max-height: 90vh; overflow-y: auto; color: #fff; box-shadow: 0 24px 60px rgba(0,0,0,0.85); display: flex; flex-direction: column; gap: 16px;">
                <!-- Header -->
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 10px;">
                    <div style="display: flex; align-items: center; gap: 9px;">
                        <span style="font-size: 20px;">⚙️</span>
                        <div>
                            <strong style="color: #38bdf8; font-size: 16px; font-weight: 700;">Cài Đặt Phím Tắt & Chẩn Đoán</strong>
                            <div style="font-size: 11px; color: #94a3b8;">Genshin Helper v4.2 • Zero-Memory Computer Vision</div>
                        </div>
                    </div>
                    <button id="gh-modal-close" style="background: transparent; border: none; color: #94a3b8; font-size: 18px; cursor: pointer; line-height: 1;">✕</button>
                </div>

                <!-- Section 1: Sync & Mark Hotkey -->
                <div style="background: rgba(2, 6, 23, 0.5); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span style="font-size: 14px;">🎯</span>
                            <strong style="font-size: 13px; color: #38bdf8;">Phím Đồng Bộ & Ghim (Sync & Mark)</strong>
                        </div>
                        <span id="gh-sync-hint" style="font-size: 11px; color: #64748b;">Mặc định: Alt+F11</span>
                    </div>
                    <div style="font-size: 11.5px; color: #94a3b8; line-height: 1.4;">
                        Nhấn phím này khi mở Map (M) trong game để tự động định vị & trượt web map tới vị trí người chơi.
                    </div>

                    <!-- Sync Recorder Box -->
                    <div id="gh-sync-recorder" tabindex="0" title="Click để bắt phím mới cho tính năng Sync" style="background: rgba(15, 23, 42, 0.7); border: 2px dashed rgba(56, 189, 248, 0.45); border-radius: 9px; padding: 9px 14px; color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: space-between; transition: all 0.2s ease; outline: none;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span id="gh-sync-icon" style="font-size: 18px;">⌨️</span>
                            <div>
                                <div id="gh-sync-display" class="gh-mono" style="font-size: 14px; font-weight: 700; color: #38bdf8;">${currentHotkeySyncDisplay}</div>
                                <div id="gh-sync-subtext" style="font-size: 10.5px; color: #64748b;">Click vào đây rồi nhấn phím hoặc nút chuột</div>
                            </div>
                        </div>
                        <div id="gh-sync-badge" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 6px; padding: 3px 8px; font-size: 11px; color: #7dd3fc; font-weight: 600;">
                            Đổi phím
                        </div>
                    </div>

                    <!-- Sync Presets -->
                    <div style="display: flex; flex-wrap: wrap; gap: 5px; margin-top: 2px;">
                        <button class="gh-preset-btn" data-target="sync" data-key="mouse4" style="background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #38bdf8; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">🖱️ Mouse 4</button>
                        <button class="gh-preset-btn" data-target="sync" data-key="mouse5" style="background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #38bdf8; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">🖱️ Mouse 5</button>
                        <button class="gh-preset-btn" data-target="sync" data-key="middle" style="background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #38bdf8; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">🖱️ Chuột giữa</button>
                        <button class="gh-preset-btn" data-target="sync" data-key="alt+mouse4" style="background: rgba(168, 85, 247, 0.12); border: 1px solid rgba(168, 85, 247, 0.35); color: #c084fc; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">🖱️ Alt + Mouse 4</button>
                        <button class="gh-preset-btn" data-target="sync" data-key="alt+f11" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">⌨️ Alt + F11</button>
                        <button class="gh-preset-btn" data-target="sync" data-key="f10" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">⌨️ F10</button>
                    </div>
                </div>

                <!-- Section 2: Diagnostic Capture Hotkey -->
                <div style="background: rgba(2, 6, 23, 0.5); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span style="font-size: 14px;">📸</span>
                            <strong style="font-size: 13px; color: #fbbf24;">Phím Chụp Chẩn Đoán & Debug</strong>
                        </div>
                        <span id="gh-debug-hint" style="font-size: 11px; color: #64748b;">Mặc định: Alt+F12</span>
                    </div>
                    <div style="font-size: 11.5px; color: #94a3b8; line-height: 1.4;">
                        Chụp đồng thời màn hình Game + Browser Web Map và gom toàn bộ log CV/định vị thành 1 bundle để gửi phân tích & fix lỗi.
                    </div>

                    <!-- Debug Recorder Box -->
                    <div id="gh-debug-recorder" tabindex="0" title="Click để bắt phím mới cho tính năng Debug" style="background: rgba(15, 23, 42, 0.7); border: 2px dashed rgba(245, 158, 11, 0.45); border-radius: 9px; padding: 9px 14px; color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: space-between; transition: all 0.2s ease; outline: none;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span id="gh-debug-icon" style="font-size: 18px;">⌨️</span>
                            <div>
                                <div id="gh-debug-display" class="gh-mono" style="font-size: 14px; font-weight: 700; color: #fbbf24;">${currentHotkeyDebugDisplay}</div>
                                <div id="gh-debug-subtext" style="font-size: 10.5px; color: #64748b;">Click vào đây rồi nhấn phím hoặc nút chuột</div>
                            </div>
                        </div>
                        <div id="gh-debug-badge" style="background: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.35); border-radius: 6px; padding: 3px 8px; font-size: 11px; color: #fcd34d; font-weight: 600;">
                            Đổi phím
                        </div>
                    </div>

                    <!-- Debug Presets -->
                    <div style="display: flex; flex-wrap: wrap; gap: 5px; margin-top: 2px;">
                        <button class="gh-preset-btn" data-target="debug" data-key="alt+f12" style="background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.35); color: #fbbf24; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">⌨️ Alt + F12</button>
                        <button class="gh-preset-btn" data-target="debug" data-key="f12" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">⌨️ F12</button>
                        <button class="gh-preset-btn" data-target="debug" data-key="f9" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">⌨️ F9</button>
                        <button class="gh-preset-btn" data-target="debug" data-key="ctrl+f12" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">⌨️ Ctrl + F12</button>
                        <button class="gh-preset-btn" data-target="debug" data-key="mouse5" style="background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #38bdf8; border-radius: 5px; padding: 3px 7px; font-size: 11px; cursor: pointer;">🖱️ Mouse 5</button>
                    </div>
                </div>

                <!-- Section 3: Manual Diagnostic Trigger Button -->
                <div style="background: rgba(2, 6, 23, 0.35); border: 1px solid rgba(51, 65, 85, 0.6); border-radius: 12px; padding: 11px 14px; display: flex; align-items: center; justify-content: space-between; gap: 12px;">
                    <div>
                        <strong style="font-size: 12.5px; color: #e2e8f0;">Kiểm tra chụp chẩn đoán ngay:</strong>
                        <div id="gh-diag-status" style="font-size: 11px; color: #64748b; margin-top: 2px;">Chụp ngay mà không cần ấn phím tắt</div>
                    </div>
                    <button id="gh-btn-trigger-diag" style="background: rgba(245, 158, 11, 0.18); border: 1px solid rgba(245, 158, 11, 0.5); color: #fbbf24; border-radius: 8px; padding: 7px 14px; font-size: 11.5px; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all 0.15s ease;">
                        📸 Chụp Ngay
                    </button>
                </div>

                <!-- Error & Status Text -->
                <div id="gh-modal-error" style="color: #ef4444; font-size: 11.5px; min-height: 16px; line-height: 1.4;"></div>

                <!-- Footer Actions -->
                <div style="display: flex; justify-content: flex-end; gap: 9px; border-top: 1px solid rgba(51, 65, 85, 0.6); padding-top: 12px;">
                    <button id="gh-modal-cancel" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); color: #94a3b8; border-radius: 8px; padding: 8px 18px; cursor: pointer; font-size: 12px; font-weight: 500;">Hủy</button>
                    <button id="gh-modal-save" style="background: #0284c7; border: none; color: #fff; font-weight: 600; border-radius: 8px; padding: 8px 20px; cursor: pointer; font-size: 12px; box-shadow: 0 2px 10px rgba(2,132,199,0.4); transition: all 0.15s;">Lưu cài đặt</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const syncBox = document.getElementById('gh-sync-recorder');
        const syncDisplay = document.getElementById('gh-sync-display');
        const syncSubtext = document.getElementById('gh-sync-subtext');
        const syncIcon = document.getElementById('gh-sync-icon');
        const syncBadge = document.getElementById('gh-sync-badge');

        const debugBox = document.getElementById('gh-debug-recorder');
        const debugDisplay = document.getElementById('gh-debug-display');
        const debugSubtext = document.getElementById('gh-debug-subtext');
        const debugIcon = document.getElementById('gh-debug-icon');
        const debugBadge = document.getElementById('gh-debug-badge');

        const triggerDiagBtn = document.getElementById('gh-btn-trigger-diag');
        const diagStatus = document.getElementById('gh-diag-status');

        const closeBtn = document.getElementById('gh-modal-close');
        const cancelBtn = document.getElementById('gh-modal-cancel');
        const saveBtn = document.getElementById('gh-modal-save');
        const errEl = document.getElementById('gh-modal-error');

        let recordedSyncVal = currentHotkeySyncDisplay.toLowerCase().replace(/\s+/g, '');
        let recordedSyncDisplay = currentHotkeySyncDisplay;

        let recordedDebugVal = currentHotkeyDebugDisplay.toLowerCase().replace(/\s+/g, '');
        let recordedDebugDisplay = currentHotkeyDebugDisplay;

        let activeTarget = null; // 'sync' or 'debug' or null

        function stopRecording(newVal, newDisplay) {
            if (!activeTarget) return;
            const target = activeTarget;
            activeTarget = null;

            removeRecordingListeners();

            if (target === 'sync') {
                syncBox.style.borderColor = 'rgba(56, 189, 248, 0.45)';
                syncBox.style.borderStyle = 'dashed';
                syncBox.style.background = 'rgba(15, 23, 42, 0.7)';
                syncBox.style.boxShadow = 'none';

                if (newVal && newDisplay) {
                    recordedSyncVal = newVal;
                    recordedSyncDisplay = newDisplay;
                    syncDisplay.textContent = newDisplay;
                    syncDisplay.style.color = '#38bdf8';
                    syncSubtext.textContent = 'Đã chọn! Nhấn [Lưu cài đặt] để áp dụng.';
                    syncIcon.textContent = '✅';
                    syncBadge.textContent = 'Đã chọn';
                    syncBadge.style.background = 'rgba(34, 197, 94, 0.15)';
                    syncBadge.style.borderColor = 'rgba(34, 197, 94, 0.4)';
                    syncBadge.style.color = '#4ade80';
                } else {
                    syncDisplay.textContent = recordedSyncDisplay;
                    syncDisplay.style.color = '#38bdf8';
                    syncSubtext.textContent = 'Click vào đây rồi nhấn phím hoặc nút chuột';
                    syncIcon.textContent = '⌨️';
                    syncBadge.textContent = 'Đổi phím';
                    syncBadge.style.background = 'rgba(56, 189, 248, 0.15)';
                    syncBadge.style.borderColor = 'rgba(56, 189, 248, 0.35)';
                    syncBadge.style.color = '#7dd3fc';
                }
            } else if (target === 'debug') {
                debugBox.style.borderColor = 'rgba(245, 158, 11, 0.45)';
                debugBox.style.borderStyle = 'dashed';
                debugBox.style.background = 'rgba(15, 23, 42, 0.7)';
                debugBox.style.boxShadow = 'none';

                if (newVal && newDisplay) {
                    recordedDebugVal = newVal;
                    recordedDebugDisplay = newDisplay;
                    debugDisplay.textContent = newDisplay;
                    debugDisplay.style.color = '#fbbf24';
                    debugSubtext.textContent = 'Đã chọn! Nhấn [Lưu cài đặt] để áp dụng.';
                    debugIcon.textContent = '✅';
                    debugBadge.textContent = 'Đã chọn';
                    debugBadge.style.background = 'rgba(34, 197, 94, 0.15)';
                    debugBadge.style.borderColor = 'rgba(34, 197, 94, 0.4)';
                    debugBadge.style.color = '#4ade80';
                } else {
                    debugDisplay.textContent = recordedDebugDisplay;
                    debugDisplay.style.color = '#fbbf24';
                    debugSubtext.textContent = 'Click vào đây rồi nhấn phím hoặc nút chuột';
                    debugIcon.textContent = '⌨️';
                    debugBadge.textContent = 'Đổi phím';
                    debugBadge.style.background = 'rgba(245, 158, 11, 0.15)';
                    debugBadge.style.borderColor = 'rgba(245, 158, 11, 0.35)';
                    debugBadge.style.color = '#fcd34d';
                }
            }
        }

        function startRecording(target) {
            if (activeTarget) {
                stopRecording(null, null);
            }
            activeTarget = target;

            if (target === 'sync') {
                syncBox.style.borderColor = '#38bdf8';
                syncBox.style.borderStyle = 'solid';
                syncBox.style.background = 'rgba(56, 189, 248, 0.14)';
                syncBox.style.boxShadow = '0 0 20px rgba(56, 189, 248, 0.35)';
                syncDisplay.textContent = 'Đang nghe phím Sync...';
                syncDisplay.style.color = '#e2e8f0';
                syncSubtext.textContent = 'Nhấn phím bàn phím hoặc click nút chuột bất kỳ';
                syncIcon.textContent = '🔴';
                syncBadge.textContent = 'Đang nghe...';
                syncBadge.style.background = 'rgba(239, 68, 68, 0.2)';
                syncBadge.style.borderColor = 'rgba(239, 68, 68, 0.5)';
                syncBadge.style.color = '#f87171';
                syncBox.focus();
            } else if (target === 'debug') {
                debugBox.style.borderColor = '#fbbf24';
                debugBox.style.borderStyle = 'solid';
                debugBox.style.background = 'rgba(245, 158, 11, 0.14)';
                debugBox.style.boxShadow = '0 0 20px rgba(245, 158, 11, 0.35)';
                debugDisplay.textContent = 'Đang nghe phím Debug...';
                debugDisplay.style.color = '#e2e8f0';
                debugSubtext.textContent = 'Nhấn phím bàn phím hoặc click nút chuột bất kỳ';
                debugIcon.textContent = '🔴';
                debugBadge.textContent = 'Đang nghe...';
                debugBadge.style.background = 'rgba(239, 68, 68, 0.2)';
                debugBadge.style.borderColor = 'rgba(239, 68, 68, 0.5)';
                debugBadge.style.color = '#f87171';
                debugBox.focus();
            }

            addRecordingListeners();
        }

        function preventDefaultAction(e) {
            if (activeTarget) {
                e.preventDefault();
                e.stopPropagation();
            }
        }

        function getMouseButton(e) {
            if (e.button === 1) return { val: 'middle', name: 'Chuột Giữa (Middle)' };
            if (e.button === 2) return { val: 'right', name: 'Chuột Phải' };
            if (e.button === 3) return { val: 'mouse4', name: 'Mouse 4 (Chuột hông)' };
            if (e.button === 4) return { val: 'mouse5', name: 'Mouse 5 (Chuột hông)' };

            if (e.buttons) {
                if (e.buttons & 4) return { val: 'middle', name: 'Chuột Giữa (Middle)' };
                if (e.buttons & 2) return { val: 'right', name: 'Chuột Phải' };
                if (e.buttons & 8) return { val: 'mouse4', name: 'Mouse 4 (Chuột hông)' };
                if (e.buttons & 16) return { val: 'mouse5', name: 'Mouse 5 (Chuột hông)' };
            }

            if (e.which === 2) return { val: 'middle', name: 'Chuột Giữa (Middle)' };
            if (e.which === 3) return { val: 'right', name: 'Chuột Phải' };
            if (e.which === 4) return { val: 'mouse4', name: 'Mouse 4 (Chuột hông)' };
            if (e.which === 5) return { val: 'mouse5', name: 'Mouse 5 (Chuột hông)' };

            return null;
        }

        function onGenericMouseEvent(e) {
            if (!activeTarget) return;

            if (e.target && typeof e.target.closest === 'function' && e.target.closest('#gh-modal-close, #gh-modal-cancel')) {
                stopRecording(null, null);
                return;
            }

            const btn = getMouseButton(e);
            if (!btn) {
                if (e.button === 0 && (e.altKey || e.ctrlKey || e.shiftKey)) {
                    e.preventDefault();
                    e.stopPropagation();
                    const modsVal = [];
                    const modsDisplay = [];
                    if (e.ctrlKey) { modsVal.push('ctrl'); modsDisplay.push('Ctrl'); }
                    if (e.altKey) { modsVal.push('alt'); modsDisplay.push('Alt'); }
                    if (e.shiftKey) { modsVal.push('shift'); modsDisplay.push('Shift'); }
                    modsVal.push('left');
                    modsDisplay.push('Chuột Trái');
                    stopRecording(modsVal.join('+'), modsDisplay.join(' + '));
                }
                return;
            }

            e.preventDefault();
            e.stopPropagation();

            const modsVal = [];
            const modsDisplay = [];
            if (e.ctrlKey) { modsVal.push('ctrl'); modsDisplay.push('Ctrl'); }
            if (e.altKey) { modsVal.push('alt'); modsDisplay.push('Alt'); }
            if (e.shiftKey) { modsVal.push('shift'); modsDisplay.push('Shift'); }

            modsVal.push(btn.val);
            modsDisplay.push(btn.name);

            stopRecording(modsVal.join('+'), modsDisplay.join(' + '));
        }

        function onKeyDown(e) {
            if (!activeTarget) return;
            e.preventDefault();
            e.stopPropagation();

            if (e.key === 'Escape') {
                stopRecording(null, null);
                return;
            }

            if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) {
                const activeMods = [];
                if (e.ctrlKey) activeMods.push('Ctrl');
                if (e.altKey) activeMods.push('Alt');
                if (e.shiftKey) activeMods.push('Shift');
                const previewStr = activeMods.join(' + ') + ' + ...';
                if (activeTarget === 'sync') syncDisplay.textContent = previewStr;
                else if (activeTarget === 'debug') debugDisplay.textContent = previewStr;
                return;
            }

            const modsVal = [];
            const modsDisplay = [];
            if (e.ctrlKey) { modsVal.push('ctrl'); modsDisplay.push('Ctrl'); }
            if (e.altKey) { modsVal.push('alt'); modsDisplay.push('Alt'); }
            if (e.shiftKey) { modsVal.push('shift'); modsDisplay.push('Shift'); }

            let keyName = e.key.toLowerCase();
            let keyDisplayName = e.key.toUpperCase();

            if (e.code.startsWith('F') && !isNaN(parseInt(e.code.slice(1)))) {
                keyName = e.code.toLowerCase();
                keyDisplayName = e.code.toUpperCase();
            } else if (e.code === 'Space') {
                keyName = 'space';
                keyDisplayName = 'Space';
            } else if (e.code.startsWith('Key')) {
                keyName = e.code.slice(3).toLowerCase();
                keyDisplayName = e.code.slice(3).toUpperCase();
            } else if (e.code.startsWith('Digit')) {
                keyName = e.code.slice(5);
                keyDisplayName = keyName;
            }

            modsVal.push(keyName);
            modsDisplay.push(keyDisplayName);

            stopRecording(modsVal.join('+'), modsDisplay.join(' + '));
        }

        function addRecordingListeners() {
            window.addEventListener('keydown', onKeyDown, { capture: true });
            window.addEventListener('pointerdown', onGenericMouseEvent, { capture: true });
            window.addEventListener('mousedown', onGenericMouseEvent, { capture: true });
            window.addEventListener('pointerup', onGenericMouseEvent, { capture: true });
            window.addEventListener('mouseup', onGenericMouseEvent, { capture: true });
            window.addEventListener('auxclick', onGenericMouseEvent, { capture: true });
            window.addEventListener('contextmenu', preventDefaultAction, { capture: true });
        }

        function removeRecordingListeners() {
            window.removeEventListener('keydown', onKeyDown, { capture: true });
            window.removeEventListener('pointerdown', onGenericMouseEvent, { capture: true });
            window.removeEventListener('mousedown', onGenericMouseEvent, { capture: true });
            window.removeEventListener('pointerup', onGenericMouseEvent, { capture: true });
            window.removeEventListener('mouseup', onGenericMouseEvent, { capture: true });
            window.removeEventListener('auxclick', onGenericMouseEvent, { capture: true });
            setTimeout(() => {
                window.removeEventListener('contextmenu', preventDefaultAction, { capture: true });
            }, 300);
        }

        // Attach clicks for sync box
        syncBox.addEventListener('click', () => {
            if (activeTarget !== 'sync') startRecording('sync');
        });
        syncBox.addEventListener('pointerdown', (e) => {
            if ([1, 2, 3, 4].includes(e.button)) {
                e.preventDefault(); e.stopPropagation();
                if (activeTarget !== 'sync') startRecording('sync');
                onGenericMouseEvent(e);
            }
        });

        // Attach clicks for debug box
        debugBox.addEventListener('click', () => {
            if (activeTarget !== 'debug') startRecording('debug');
        });
        debugBox.addEventListener('pointerdown', (e) => {
            if ([1, 2, 3, 4].includes(e.button)) {
                e.preventDefault(); e.stopPropagation();
                if (activeTarget !== 'debug') startRecording('debug');
                onGenericMouseEvent(e);
            }
        });

        // Presets handler
        overlay.querySelectorAll('.gh-preset-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const target = btn.getAttribute('data-target') || 'sync';
                const key = btn.getAttribute('data-key');
                const label = btn.innerText.replace(/[🖱️⌨️]/g, '').trim();
                activeTarget = target;
                stopRecording(key, label);
            });
        });

        // Manual Diagnostic Trigger Button
        triggerDiagBtn.addEventListener('click', () => {
            triggerDiagBtn.disabled = true;
            triggerDiagBtn.textContent = '📸 Đang chụp...';
            diagStatus.textContent = 'Đang đồng thời chụp Game + Browser và phân tích CV...';
            diagStatus.style.color = '#fbbf24';

            fetch('http://127.0.0.1:8765/trigger_diagnostic', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: 'web_settings_modal' })
            })
            .then(r => r.json())
            .then(res => {
                triggerDiagBtn.disabled = false;
                triggerDiagBtn.textContent = '📸 Chụp Ngay';
                if (res && res.ok && res.bundle) {
                    playBeep();
                    const fname = res.bundle.folder_name || 'diag_bundle';
                    diagStatus.textContent = `✅ Đã lưu vào [${fname}]!`;
                    diagStatus.style.color = '#4ade80';
                    setLog(`📸 Chụp chẩn đoán thành công: [${fname}]`, '#22c55e');
                    closeModal();
                    showDiagnosticNoteModal(fname, Boolean(res.bundle.has_world_coords), res.bundle.reason || '');
                } else {
                    diagStatus.textContent = '❌ Lỗi khi chụp chẩn đoán. Kiểm tra tool desktop!';
                    diagStatus.style.color = '#ef4444';
                }
            })
            .catch(err => {
                triggerDiagBtn.disabled = false;
                triggerDiagBtn.textContent = '📸 Chụp Ngay';
                diagStatus.textContent = '❌ Không thể kết nối tới server (127.0.0.1:8765)!';
                diagStatus.style.color = '#ef4444';
            });
        });

        const closeModal = () => {
            stopRecording(null, null);
            overlay.remove();
        };

        closeBtn.addEventListener('click', closeModal);
        cancelBtn.addEventListener('click', closeModal);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });

        // Save All Settings
        saveBtn.addEventListener('click', () => {
            const syncVal = recordedSyncVal.trim().toLowerCase();
            const debugVal = recordedDebugVal.trim().toLowerCase();

            if (!syncVal || !debugVal) {
                errEl.textContent = 'Vui lòng chọn đầy đủ phím tắt cho cả hai tính năng!';
                return;
            }

            saveBtn.disabled = true;
            saveBtn.textContent = 'Đang lưu...';
            errEl.textContent = '';

            fetch('http://127.0.0.1:8765/set_hotkeys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    hotkey_sync: syncVal,
                    hotkey_debug: debugVal
                })
            })
            .then(r => r.json())
            .then(res => {
                if (res && res.ok && res.results) {
                    if (res.results.sync && res.results.sync.ok) {
                        updateHotkeyDisplay(res.results.sync.display_name, 'sync');
                    }
                    if (res.results.debug && res.results.debug.ok) {
                        updateHotkeyDisplay(res.results.debug.display_name, 'debug');
                    }
                    playBeep();
                    setLog(`✅ Đã lưu cài đặt phím tắt! Sync: [${currentHotkeySyncDisplay}] | Debug: [${currentHotkeyDebugDisplay}]`, '#22c55e');
                    closeModal();
                } else {
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Lưu cài đặt';
                    errEl.textContent = 'Không thể lưu một trong các phím tắt này. Thử phím khác!';
                }
            })
            .catch(() => {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Lưu cài đặt';
                errEl.textContent = 'Lỗi kết nối tới Server (hãy chắc chắn tool desktop đang chạy)!';
            });
        });
    }

    // Backward compatibility alias
    const showHotkeyModal = showSettingsModal;

    function createUI() {
        const old = document.getElementById('genshin-helper-panel');
        if (old) old.remove();

        injectThemeStyles();

        // Clean Vector SVG Icons
        const SVG_PIN = `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>`;
        const SVG_UNPIN = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>`;
        const SVG_MIN = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"></line></svg>`;
        const SVG_GEAR = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>`;

        let isSavedCollapsed = false
        try { isSavedCollapsed = (localStorage.getItem('gh_widget_collapsed') === '1'); } catch(e) {}

        let isPinned = false;
        try { isPinned = (localStorage.getItem('gh_widget_pinned') === '1'); } catch(e) {}

        let savedPos = null;
        try {
            const raw = localStorage.getItem('gh_widget_pos');
            if (raw) savedPos = JSON.parse(raw);
        } catch(e) {}

        const panel = document.createElement('div');
        panel.id = 'genshin-helper-panel';
        panel.className = 'gh-root';

        // Compute initial position
        let initialPosStyle = 'bottom: 20px; right: 20px;';
        if (savedPos && typeof savedPos.x === 'number' && typeof savedPos.y === 'number') {
            const safeX = Math.max(10, Math.min(window.innerWidth - 360, savedPos.x));
            const safeY = Math.max(10, Math.min(window.innerHeight - 200, savedPos.y));
            initialPosStyle = `left: ${safeX}px; top: ${safeY}px;`;
        }

        panel.innerHTML = `
            <div id="gh-container" style="position: fixed; ${initialPosStyle} z-index: 2147483647; user-select: none;">
                <!-- Collapsed Floating Pill -->
                <div id="gh-pill" style="display: ${isSavedCollapsed ? 'flex' : 'none'}; align-items: center; gap: 8px; background: rgba(15, 23, 42, 0.92); backdrop-filter: blur(16px); border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 30px; padding: 7px 14px; color: #fff; cursor: ${isPinned ? 'pointer' : 'grab'}; box-shadow: 0 8px 24px rgba(0,0,0,0.6); transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);">
                    <span id="gh-pill-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #16a34a; box-shadow: 0 0 8px #16a34a;"></span>
                    <strong style="color: #38bdf8; font-size: 12px; font-weight: 700; letter-spacing: -0.2px;">Genshin Sync</strong>
                    <span id="gh-pill-hotkey" class="gh-mono" title="Phím tắt đồng bộ hiện tại (Click để mở cài đặt)" style="font-size: 11px; color: #7dd3fc; background: rgba(56, 189, 248, 0.14); border: 1px solid rgba(56, 189, 248, 0.3); padding: 1px 7px; border-radius: 5px; font-weight: 600; cursor: pointer;">${currentHotkeySyncDisplay}</span>
                    <button id="gh-pill-settings-btn" class="gh-btn-icon" title="Cài đặt phím tắt (Sync & Chẩn đoán)" style="width: 22px; height: 22px; border-radius: 6px;">
                        ${SVG_GEAR}
                    </button>
                    <span title="Mở rộng widget" style="font-size: 11px; color: #38bdf8; margin-left: 2px;">▲</span>
                </div>

                <!-- Expanded Card -->
                <div id="gh-card" style="display: ${isSavedCollapsed ? 'none' : 'flex'}; flex-direction: column; gap: 9px; min-width: 320px; max-width: 360px; background: rgba(15, 23, 42, 0.94); backdrop-filter: blur(20px); border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 14px; padding: 12px 16px; color: #fff; box-shadow: 0 16px 36px -8px rgba(0, 0, 0, 0.75), 0 0 0 1px rgba(255, 255, 255, 0.05); transition: box-shadow 0.2s ease;">
                    <!-- Draggable Header -->
                    <div id="gh-card-header" title="${isPinned ? 'Vị trí đã ghim cố định' : 'Click giữ để kéo thả • Click đúp để reset vị trí'}" style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 8px; cursor: ${isPinned ? 'default' : 'grab'};">
                        <div style="display: flex; align-items: center; gap: 7px;">
                            <span id="gh-status-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #16a34a; box-shadow: 0 0 8px #16a34a;"></span>
                            <strong style="color: #38bdf8; font-size: 13px; font-weight: 700; letter-spacing: -0.2px;">Genshin Helper</strong>
                            <span id="gh-version-tag" style="font-size: 10px; background: rgba(56,189,248,0.12); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.25); border-radius: 4px; padding: 1px 5px; font-weight: 600; line-height: 1.2; display: inline-flex; align-items: center;">${SCRIPT_VERSION}</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span id="gh-status" style="font-size: 10px; padding: 2px 7px; border-radius: 10px; background: #16a34a; font-weight: 600; line-height: 1.2; display: inline-flex; align-items: center;">Connected</span>
                            <button id="gh-pin-btn" class="gh-btn-icon ${isPinned ? 'gh-btn-pinned' : ''}" title="${isPinned ? 'Vị trí đã GHIM (Click để mở khóa kéo thả)' : 'Chưa ghim (Kéo giữ thanh tiêu đề để di chuyển, click để ghim)'}">
                                ${isPinned ? SVG_PIN : SVG_UNPIN}
                            </button>
                            <button id="gh-min-btn" class="gh-btn-icon" title="Thu nhỏ widget">${SVG_MIN}</button>
                        </div>
                    </div>

                    <!-- Coords & Settings Gear Row -->
                    <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; gap: 8px;">
                        <div style="display: flex; flex-direction: column; gap: 2px; overflow: hidden;">
                            <span style="color: #64748b; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;">TỌA ĐỘ GAME</span>
                            <span id="gh-coords" style="color: #38bdf8; font-weight: 600; font-size: 12px; font-variant-numeric: tabular-nums lining-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">Sẵn sàng</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                            <span id="gh-hotkey-text" class="gh-mono" title="Phím tắt đồng bộ hiện tại" style="font-size: 10.5px; color: #7dd3fc; background: rgba(56, 189, 248, 0.14); border: 1px solid rgba(56, 189, 248, 0.3); padding: 2px 7px; border-radius: 5px; font-weight: 600;">${currentHotkeySyncDisplay}</span>
                            <button id="gh-settings-btn" class="gh-btn-icon" title="Cài đặt phím tắt (Sync & Chẩn đoán Debug)" style="background: rgba(56, 189, 248, 0.16); border-color: rgba(56, 189, 248, 0.4); color: #38bdf8;">
                                ${SVG_GEAR}
                            </button>
                        </div>
                    </div>

                    <!-- Auto Sync on Map (M) Toggle -->
                    <label title="Tự động cập nhật vị trí & tầng khi mở Map (M) trong game (không tự click Ghim)" style="display: flex; align-items: center; justify-content: space-between; background: rgba(2, 6, 23, 0.45); border: 1px solid rgba(51, 65, 85, 0.55); border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 11.5px; color: #e2e8f0;">
                        <span>⚡ Tự đồng bộ khi mở Map (M)</span>
                        <input id="gh-auto-sync-toggle" type="checkbox" ${autoSyncMapMEnabled ? 'checked' : ''} style="accent-color: #38bdf8; width: 15px; height: 15px; cursor: pointer;">
                    </label>

                    <!-- Live Log Box -->
                    <div id="gh-log" style="font-size: 11.5px; background: rgba(2, 6, 23, 0.6); border: 1px solid rgba(51, 65, 85, 0.5); border-radius: 8px; padding: 7px 11px; min-height: 22px; color: #94a3b8; line-height: 1.45; word-break: break-word;">
                        Mở Map (M) để tự đồng bộ vị trí; nhấn phím tắt để Ghim điểm gần.
                    </div>
                </div>
            </div>
        `;

        (document.body || document.documentElement).appendChild(panel);
        console.log('[Genshin Helper v4.0] UI Panel attached!');

        const autoSyncToggle = document.getElementById('gh-auto-sync-toggle');
        if (autoSyncToggle) {
            autoSyncToggle.addEventListener('change', () => {
                const want = !!autoSyncToggle.checked;
                setAutoSyncUI(want);
                fetch(API_AUTO_SYNC_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_sync_m: want })
                })
                .then(r => r.json())
                .then(res => {
                    if (res && typeof res.auto_sync_m === 'boolean') {
                        setAutoSyncUI(res.auto_sync_m);
                    }
                    setLog(want ? '⚡ Đã BẬT tự đồng bộ khi mở Map (M).' : '⏸️ Đã TẮT tự đồng bộ Map (M) (vẫn hỗ trợ phím tắt thủ công).', want ? '#22c55e' : '#fbbf24');
                })
                .catch(() => {
                    setLog(want ? '⚡ Tự đồng bộ Map (M): BẬT (offline)' : '⏸️ Tự đồng bộ Map (M): TẮT (offline)', '#fbbf24');
                });
            });
        }

        const container = document.getElementById('gh-container');
        const cardHeader = document.getElementById('gh-card-header');
        const pinBtn = document.getElementById('gh-pin-btn');
        const minBtn = document.getElementById('gh-min-btn');
        const pill = document.getElementById('gh-pill');
        const settingsBtn = document.getElementById('gh-settings-btn');
        const pillSettingsBtn = document.getElementById('gh-pill-settings-btn');
        const pillHotkey = document.getElementById('gh-pill-hotkey');
        const hotkeyText = document.getElementById('gh-hotkey-text');

        // =====================================================================
        // Dragging & Positioning System
        // =====================================================================
        let lastDragEndTime = 0;

        function setupDraggable(handleEl, isPillMode) {
            if (!handleEl || !container) return;

            handleEl.addEventListener('pointerdown', (e) => {
                if (isPinned) return;
                if (e.target.closest('button, input, a, [data-nodrag]')) return;

                e.preventDefault();
                const rect = container.getBoundingClientRect();
                const startX = e.clientX;
                const startY = e.clientY;
                const initialLeft = rect.left;
                const initialTop = rect.top;

                let hasDragged = false;
                handleEl.style.cursor = 'grabbing';
                container.style.transition = 'none';

                function onPointerMove(moveEvent) {
                    const dx = moveEvent.clientX - startX;
                    const dy = moveEvent.clientY - startY;
                    if (Math.hypot(dx, dy) > 4) {
                        hasDragged = true;
                    }

                    let newX = initialLeft + dx;
                    let newY = initialTop + dy;

                    const curRect = container.getBoundingClientRect();
                    const maxX = window.innerWidth - curRect.width - 8;
                    const maxY = window.innerHeight - curRect.height - 8;
                    newX = Math.max(8, Math.min(maxX, newX));
                    newY = Math.max(8, Math.min(maxY, newY));

                    container.style.left = newX + 'px';
                    container.style.top = newY + 'px';
                    container.style.right = 'auto';
                    container.style.bottom = 'auto';
                }

                function onPointerUp() {
                    document.removeEventListener('pointermove', onPointerMove);
                    document.removeEventListener('pointerup', onPointerUp);
                    handleEl.style.cursor = isPinned ? 'default' : 'grab';
                    container.style.transition = 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)';

                    if (hasDragged) {
                        lastDragEndTime = Date.now();

                        // Suppress trailing click event from this drag gesture
                        const suppressClick = (clickEvent) => {
                            clickEvent.stopPropagation();
                            clickEvent.preventDefault();
                            window.removeEventListener('click', suppressClick, true);
                        };
                        window.addEventListener('click', suppressClick, true);
                        setTimeout(() => {
                            window.removeEventListener('click', suppressClick, true);
                        }, 200);

                        const finalRect = container.getBoundingClientRect();
                        localStorage.setItem('gh_widget_pos', JSON.stringify({
                            x: Math.round(finalRect.left),
                            y: Math.round(finalRect.top)
                        }));
                    }
                }

                document.addEventListener('pointermove', onPointerMove);
                document.addEventListener('pointerup', onPointerUp);
            });
        }

        setupDraggable(cardHeader, false);
        setupDraggable(pill, true);

        // Double click card header to reset position to bottom-right
        if (cardHeader) {
            cardHeader.addEventListener('dblclick', (e) => {
                if (e.target.closest('button, input')) return;
                container.style.left = 'auto';
                container.style.top = 'auto';
                container.style.right = '20px';
                container.style.bottom = '20px';
                try { localStorage.removeItem('gh_widget_pos'); } catch(err) {}
                setLog('📍 Đã đặt lại vị trí mặc định (góc dưới bên phải)!', '#38bdf8');
            });
        }

        // =====================================================================
        // Pin / Lock Position Toggle
        // =====================================================================
        if (pinBtn) {
            pinBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                isPinned = !isPinned;
                try { localStorage.setItem('gh_widget_pinned', isPinned ? '1' : '0'); } catch(err) {}

                if (isPinned) {
                    pinBtn.classList.add('gh-btn-pinned');
                    pinBtn.innerHTML = SVG_PIN;
                    pinBtn.title = 'Vị trí đã GHIM (Click để mở khóa kéo thả)';
                    if (cardHeader) {
                        cardHeader.style.cursor = 'default';
                        cardHeader.title = 'Vị trí đã ghim cố định';
                    }
                    if (pill) pill.style.cursor = 'pointer';
                    setLog('📌 Đã ghim vị trí widget cố định!', '#38bdf8');
                } else {
                    pinBtn.classList.remove('gh-btn-pinned');
                    pinBtn.innerHTML = SVG_UNPIN;
                    pinBtn.title = 'Chưa ghim (Kéo giữ thanh tiêu đề để di chuyển, click để ghim)';
                    if (cardHeader) {
                        cardHeader.style.cursor = 'grab';
                        cardHeader.title = 'Click giữ để kéo thả • Click đúp để reset vị trí';
                    }
                    if (pill) pill.style.cursor = 'grab';
                    setLog('📍 Đã mở khóa! Bạn có thể kéo thả widget tự do khắp màn hình.', '#38bdf8');
                }
            });
        }

        // Minimize / Expand interactions
        if (minBtn) {
            minBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleWidget(true);
            });
        }

        if (pill) {
            pill.addEventListener('click', (e) => {
                // If user was dragging pill, don't trigger expand
                if (Date.now() - lastDragEndTime < 250) return;
                if (e.target.closest('#gh-pill-hotkey, #gh-pill-settings-btn')) return;
                toggleWidget(false);
            });
        }

        if (settingsBtn) {
            settingsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                showSettingsModal();
            });
        }

        if (pillSettingsBtn) {
            pillSettingsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                showSettingsModal();
            });
        }

        if (pillHotkey) {
            pillHotkey.addEventListener('click', (e) => {
                e.stopPropagation();
                showSettingsModal();
            });
        }

        if (hotkeyText) {
            hotkeyText.addEventListener('click', (e) => {
                e.stopPropagation();
                showSettingsModal();
            });
        }
    }

    function setStatus(connected) {
        isConnected = connected;
        const statusEl = document.getElementById('gh-status');
        const statusDot = document.getElementById('gh-status-dot');
        const pillDot = document.getElementById('gh-pill-dot');

        const color = connected ? '#16a34a' : '#dc2626';
        const text = connected ? 'Connected' : 'Disconnected';

        if (statusEl) {
            statusEl.textContent = text;
            statusEl.style.background = color;
        }
        if (statusDot) {
            statusDot.style.background = color;
            statusDot.style.boxShadow = `0 0 8px ${color}`;
        }
        if (pillDot) {
            pillDot.style.background = color;
            pillDot.style.boxShadow = `0 0 8px ${color}`;
        }
    }

    // =========================================================================
    // 2. Deep Map Context & Native MarkerList Resolver
    // =========================================================================
    function findMapContext() {
        let map = null;
        let ciLayer = null;
        let markerList = null;
        let vm = null;

        const container = document.querySelector('.leaflet-container');
        if (container) {
            if (container._leaflet_map) map = container._leaflet_map;
            let el = container;
            while (el) {
                if (el.__vue__) {
                    const v = el.__vue__;
                    if (v.map) map = v.map;
                    if (v.ciLayer) ciLayer = v.ciLayer;
                    if (Array.isArray(v.markerList) && v.markerList.length > 0) markerList = v.markerList;
                    if (v.map && (v.ciLayer || v.markerList)) {
                        vm = v;
                        break;
                    }
                }
                el = el.parentElement;
            }
        }

        if (!markerList || !map) {
            const all = document.querySelectorAll('*');
            for (let i = 0; i < all.length; i++) {
                const v = all[i].__vue__;
                if (v) {
                    if (!map && v.map && v.map._layers) map = v.map;
                    if (!ciLayer && v.ciLayer) ciLayer = v.ciLayer;
                    if (!markerList && Array.isArray(v.markerList) && v.markerList.length > 0) markerList = v.markerList;
                    if (map && markerList) {
                        vm = v;
                        break;
                    }
                }
            }
        }

        if (!map && window.map && window.map._layers) map = window.map;

        return { map, ciLayer, markerList, vm };
    }

    function getAllMarkers(ctx) {
        const { map, ciLayer, markerList } = ctx;
        const markers = [];

        if (Array.isArray(markerList) && markerList.length > 0 && map) {
            for (const item of markerList) {
                const m = item.marker;
                let latlng = null;
                if (m && typeof m.getLatLng === 'function') {
                    latlng = m.getLatLng();
                } else if (item.y_pos !== undefined && item.x_pos !== undefined) {
                    latlng = { lat: item.y_pos, lng: item.x_pos };
                }

                if (latlng && typeof map.latLngToContainerPoint === 'function') {
                    const pt = map.latLngToContainerPoint(latlng);
                    markers.push({
                        marker: m,
                        id: item.id,
                        labelId: item.label_id,
                        latlng: latlng,
                        point: pt,
                        rawItem: item,
                        source: 'markerList'
                    });
                }
            }
        }

        if (markers.length === 0 && ciLayer) {
            const rbushTree = ciLayer._markers || ciLayer._latlngMarkers;
            if (rbushTree) {
                let list = null;
                if (typeof rbushTree.all === 'function') list = rbushTree.all();
                else if (Array.isArray(rbushTree.data)) list = rbushTree.data;
                else if (rbushTree.data && Array.isArray(rbushTree.data.children)) list = rbushTree.data.children;

                if (list) {
                    for (const item of list) {
                        const m = item.data || item;
                        if (m && typeof m.getLatLng === 'function') {
                            const latlng = m.getLatLng();
                            const pt = map ? map.latLngToContainerPoint(latlng) : { x: (item.minX + item.maxX) / 2, y: (item.minY + item.maxY) / 2 };
                            markers.push({ marker: m, latlng, point: pt, source: 'ciLayer' });
                        }
                    }
                }
            }
        }

        return markers;
    }

    // =========================================================================
    // =========================================================================
    // 3. Universal Layered Exploration Manager (Caves & High-Altitude Floors)
    // =========================================================================
    function resolveGisVm(ctx = null) {
        if (ctx && ctx.vm && ctx.vm.$store) return ctx.vm;
        const direct = document.querySelector('.mhy-game-gis')?.__vue__;
        if (direct && direct.$store) return direct;
        const found = findMapContext();
        return (found && found.vm) ? found.vm : (direct || null);
    }

    function ensureAreaSwitch(ctx, areaId, areaName, isUnderground = false) {
        if (!areaId && !areaName) return 'none';
        try {
            const gisVm = resolveGisVm(ctx);
            const store = gisVm && gisVm.$store ? gisVm.$store : null;
            const targetCleanName = (areaName || '').trim().toLowerCase();

            // 1. Primary Strategy: Direct Vuex Store commit + marker reload (no camera fitBounds, works even when dropdown is folded)
            if (store && store.state && Array.isArray(store.state.areaList) && store.state.areaList.length > 0) {
                const map2Areas = store.state.areaList.filter(a => a && Number(a.map_id) === 2);
                const pool = map2Areas.length > 0 ? map2Areas : store.state.areaList;
                let targetArea = (areaId !== undefined && areaId !== null)
                    ? pool.find(a => Number(a.id) === Number(areaId))
                    : null;
                if (!targetArea && targetCleanName) {
                    targetArea = pool.find(a => a && a.name && a.name.toLowerCase().includes(targetCleanName)) || null;
                }
                if (targetArea) {
                    const curArea = store.state.areaInfo;
                    if (curArea && Number(curArea.id) === Number(targetArea.id)) {
                        return 'same';
                    }
                    store.commit('updateAreaInfo', targetArea);
                    if (!isUnderground) {
                        if (typeof gisVm.toggleAreaLayer === 'function') {
                            gisVm.toggleAreaLayer();
                        } else if (gisVm.$refs && gisVm.$refs.filterPanel && typeof gisVm.$refs.filterPanel.toggleAreaLayer === 'function') {
                            gisVm.$refs.filterPanel.toggleAreaLayer();
                        }
                    }
                    setLog(`🗺️ Tự động chuyển khu vực sang: [${targetArea.name}]!`, '#38bdf8');
                    return 'changed';
                }
            }

            // 2. Fallback DOM LI click (uses textContent so display:none items still match)
            const lis = Array.from(document.querySelectorAll('.area-list__dropdown li, .area-list li'));
            const activeLi = lis.find(li => li.classList && li.classList.contains('active'));
            const currentActiveText = activeLi ? (activeLi.querySelector('.area')?.textContent || '').trim().toLowerCase() : '';
            if (targetCleanName && currentActiveText && currentActiveText.includes(targetCleanName)) {
                return 'same';
            }
            let targetLi = null;
            if (targetCleanName) {
                targetLi = lis.find(li => {
                    const txt = (li.querySelector('.area')?.textContent || '').trim().toLowerCase();
                    return txt && (txt === targetCleanName || txt.includes(targetCleanName) || targetCleanName.includes(txt));
                });
            }
            if (targetLi && !isUnderground) {
                targetLi.click();
                setLog(`🗺️ Tự động chuyển khu vực sang: [${areaName || ('Vùng ' + areaId)}]!`, '#38bdf8');
                return 'changed';
            }
        } catch (e) {
            console.error('[Genshin Helper] Error in ensureAreaSwitch:', e);
        }
        return 'none';
    }

    function findMatchingPointGroup(targetLat, targetLng, targetFloorName, totalFloors = null, activeFloorIdx = null) {
        if (targetLat === null || targetLng === null) return null;
        const gisEl = document.querySelector('.mhy-game-gis');
        const vm = gisEl?.__vue__;
        const list = (vm && Array.isArray(vm.pointGroupList) && vm.pointGroupList.length > 0)
            ? vm.pointGroupList
            : (vm && vm.$store && Array.isArray(vm.$store.state.pointGroupList) ? vm.$store.state.pointGroupList : []);

        if (!list || list.length === 0) return null;

        const cleanTargetFloor = (targetFloorName || '').trim().toUpperCase();
        const candidates = [];

        for (const g of list) {
            if (!g.floors || g.floors.length === 0) continue;

            let insideGroup = false;
            let bestFloorInGroup = null;
            let bestFloorScore = -9999;

            // Elevator has 1 surface floor (G) + g.floors.length underground floors
            const expectedTotal = g.floors.length + 1;
            let floorCountScore = 0;
            if (totalFloors) {
                const floorDiff = Math.abs(expectedTotal - totalFloors);
                if (floorDiff === 0) {
                    floorCountScore = 2000; // Perfect match on total elevator floors
                } else {
                    floorCountScore = -floorDiff * 500; // Heavy penalty for mismatch
                }
            }

            for (let fIdx = 0; fIdx < g.floors.length; fIdx++) {
                const f = g.floors[fIdx];
                if (!f.overlay) continue;

                const ov = f.overlay;
                const minLat = Math.min(ov.l_y, ov.r_y);
                const maxLat = Math.max(ov.l_y, ov.r_y);
                const minLng = Math.min(ov.l_x, ov.r_x);
                const maxLng = Math.max(ov.l_x, ov.r_x);

                // Margin tolerance 60m
                if (targetLng >= minLng - 60 && targetLng <= maxLng + 60 &&
                    targetLat >= minLat - 60 && targetLat <= maxLat + 60) {

                    insideGroup = true;
                    let fScore = 0;
                    const s = (f.floor_name_short || '').trim().toUpperCase();
                    const n = (f.floor_name || '').trim().toUpperCase();

                    // Floor naming matching
                    if (cleanTargetFloor) {
                        if (s === cleanTargetFloor) {
                            fScore += 500;
                        } else if (n.includes(cleanTargetFloor)) {
                            fScore += 300;
                        } else if (cleanTargetFloor === 'B1' && (fIdx === 0 || s === '')) {
                            // First underground floor represents B1 even if short name is empty
                            fScore += 400;
                        }
                    }

                    // Active floor index matching (in-game activeFloorIdx: 0=Surface, 1=Underground1, 2=Underground2...)
                    if (activeFloorIdx !== null && activeFloorIdx > 0 && fIdx === (activeFloorIdx - 1)) {
                        fScore += 400;
                    }

                    // Distance to center of this floor overlay
                    const centerLat = (minLat + maxLat) / 2;
                    const centerLng = (minLng + maxLng) / 2;
                    const distSq = (targetLat - centerLat) ** 2 + (targetLng - centerLng) ** 2;
                    const distScore = Math.max(0, 100 - Math.sqrt(distSq) / 10);
                    fScore += distScore;

                    if (fScore > bestFloorScore) {
                        bestFloorScore = fScore;
                        bestFloorInGroup = f;
                    }
                }
            }

            if (insideGroup && bestFloorInGroup) {
                candidates.push({
                    group: g,
                    defaultFloor: bestFloorInGroup,
                    score: floorCountScore + bestFloorScore
                });
            }
        }

        if (candidates.length > 0) {
            candidates.sort((a, b) => b.score - a.score);
            return { group: candidates[0].group, defaultFloor: candidates[0].defaultFloor, gisVm: vm };
        }
        return null;
    }

    function computeTargetLeafletZoom(map, data) {
        const minZ = (map && typeof map.getMinZoom === 'function') ? map.getMinZoom() : -3;
        const maxZ = (map && typeof map.getMaxZoom === 'function') ? map.getMaxZoom() : 2;
        let z = 0.8;
        if (data && typeof data.zoom_ratio_t === 'number' && !isNaN(data.zoom_ratio_t)) {
            const t = Math.max(0, Math.min(1, data.zoom_ratio_t));
            z = -1.5 + 3.5 * t;
        }
        return Math.max(minZ, Math.min(maxZ, z));
    }

    function _buildPlayerMarkerHtml(data) {
        const heading = (data && typeof data.heading === 'number' && !isNaN(data.heading)) ? data.heading : 0;
        const latStr = (data && typeof data.lat === 'number') ? data.lat.toFixed(1) : '?';
        const lngStr = (data && typeof data.lng === 'number') ? data.lng.toFixed(1) : '?';
        const floorTag = (data && data.is_underground && (data.floor_short || data.floor_name)) ? ` • ${data.floor_short || data.floor_name}` : '';
        return `
            <div class="gh-player-marker-wrap" title="Player (${latStr}, ${lngStr})${floorTag}">
                <div class="gh-player-pulse-ring"></div>
                <svg class="gh-player-arrow-svg" style="transform: translate(-50%, -50%) rotate(${heading.toFixed(1)}deg);" viewBox="0 0 32 32">
                    <path d="M16 3 L26 27 L16 21 L6 27 Z" fill="#38bdf8" stroke="#ffffff" stroke-width="2" stroke-linejoin="round"/>
                </svg>
            </div>
        `;
    }

    function _repositionDomPlayerMarker() {
        try {
            if (!playerDomMarkerEl || !playerDomMapRef || !playerLastState) return;
            if (typeof playerDomMapRef.latLngToLayerPoint === 'function') {
                const pt = playerDomMapRef.latLngToLayerPoint([playerLastState.lat, playerLastState.lng]);
                playerDomMarkerEl.style.transform = `translate3d(${Math.round(pt.x - 22)}px, ${Math.round(pt.y - 22)}px, 0)`;
            }
        } catch(e) {}
    }

    function updatePlayerMarker(ctx, data) {
        try {
            if (!ctx || !ctx.map || !data || typeof data.lat !== 'number' || typeof data.lng !== 'number') return;
            const html = _buildPlayerMarkerHtml(data);
            playerLastState = { lat: data.lat, lng: data.lng, heading: data.heading || 0 };

            // Primary path: native Leaflet L.marker + L.divIcon if global window.L is exposed
            if (window.L && typeof window.L.marker === 'function' && typeof window.L.divIcon === 'function') {
                if (playerDomMarkerEl) {
                    playerDomMarkerEl.remove();
                    playerDomMarkerEl = null;
                }
                if (playerMarkerLayer && playerMarkerMapRef !== ctx.map) {
                    try { playerMarkerLayer.remove(); } catch(e) {}
                    playerMarkerLayer = null;
                }
                const icon = window.L.divIcon({
                    className: 'gh-player-leaflet-icon',
                    html: html,
                    iconSize: [44, 44],
                    iconAnchor: [22, 22]
                });
                if (!playerMarkerLayer) {
                    playerMarkerLayer = window.L.marker([data.lat, data.lng], {
                        icon: icon,
                        interactive: false,
                        zIndexOffset: 9000
                    }).addTo(ctx.map);
                    playerMarkerMapRef = ctx.map;
                } else {
                    playerMarkerLayer.setLatLng([data.lat, data.lng]);
                    playerMarkerLayer.setIcon(icon);
                }
                return;
            }

            // Fallback path for bundled HoYoLAB Leaflet (mounts into Leaflet markerPane via latLngToLayerPoint)
            const pane = (typeof ctx.map.getPanes === 'function' && ctx.map.getPanes() && ctx.map.getPanes().markerPane)
                ? ctx.map.getPanes().markerPane
                : (typeof ctx.map.getContainer === 'function' ? ctx.map.getContainer() : null);
            if (!pane) return;

            if (!playerDomMarkerEl || !playerDomMarkerEl.isConnected || playerDomMapRef !== ctx.map) {
                if (playerDomMarkerEl) playerDomMarkerEl.remove();
                playerDomMarkerEl = document.createElement('div');
                playerDomMarkerEl.id = 'gh-player-marker-dom';
                playerDomMarkerEl.style.cssText = 'position:absolute;left:0;top:0;width:44px;height:44px;z-index:9000;pointer-events:none;';
                pane.appendChild(playerDomMarkerEl);
                if (playerDomMapRef !== ctx.map && typeof ctx.map.on === 'function') {
                    playerDomMapRef = ctx.map;
                    ctx.map.on('move zoom viewreset zoomend', _repositionDomPlayerMarker);
                } else {
                    playerDomMapRef = ctx.map;
                }
            }
            playerDomMarkerEl.innerHTML = html;
            _repositionDomPlayerMarker();
        } catch (e) {
            console.error('[Genshin Helper] Error in updatePlayerMarker:', e);
        }
    }

    function selectElevatorFloorExact(data, targetFloor = null) {
        try {
            const elevator = document.querySelector('#underground-elevator, .underground-elevator');
            if (!elevator || elevator.offsetWidth === 0) return false;
            const domFloors = Array.from(elevator.querySelectorAll('.underground-elevator__floor'));
            if (domFloors.length === 0) return false;

            const elevatorVm = elevator.__vue__;
            const vmFloors = elevatorVm && Array.isArray(elevatorVm.floors) ? elevatorVm.floors : [];
            const targetFloorId = (targetFloor && targetFloor.id !== undefined) ? targetFloor.id : (data ? data.floor_id : null);

            let chosen = null;
            if (targetFloorId !== null && targetFloorId !== undefined && vmFloors.length === domFloors.length) {
                const vmIdx = vmFloors.findIndex(ef => ef && Number(ef.id) === Number(targetFloorId));
                if (vmIdx >= 0 && domFloors[vmIdx]) {
                    chosen = domFloors[vmIdx];
                }
            }

            if (!chosen) {
                const shortWant = ((targetFloor && (targetFloor.floor_name_short || targetFloor.floor_short)) || (data && (data.floor_short || data.floor_name)) || '').trim().toUpperCase();
                if (shortWant) {
                    chosen = domFloors.find(f => (f.innerText || f.textContent || '').trim().toUpperCase() === shortWant);
                }
            }

            if (!chosen && data && data.active_floor_idx !== undefined && data.active_floor_idx >= 0 && data.active_floor_idx < domFloors.length) {
                chosen = domFloors[data.active_floor_idx];
            }

            if (chosen) {
                dispatchFullClick(chosen);
                return true;
            }
        } catch(e) {}
        return false;
    }

    function forceRefreshCaveCanvas(gisVm) {
        try {
            const layer = gisVm && gisVm.canvasLayeredImageLayer;
            if (!layer) return false;
            if (typeof layer.setBackground === 'function') {
                layer.setBackground('rgba(0, 0, 0, 0.5)');
            }
            if (typeof layer._reset === 'function') {
                layer._reset();
            } else if (typeof layer.redraw === 'function') {
                layer.redraw();
            }
            if (layer._imageLookup && typeof layer._imageLookup === 'object') {
                Object.values(layer._imageLookup).forEach(entry => {
                    const img = Array.isArray(entry) ? entry[0] : null;
                    if (img && !img.complete && !img.__gh_onload_bound) {
                        img.__gh_onload_bound = true;
                        img.addEventListener('load', () => {
                            try {
                                if (typeof layer.setBackground === 'function') layer.setBackground('rgba(0, 0, 0, 0.5)');
                                if (typeof layer._reset === 'function') layer._reset();
                            } catch (e) {}
                        }, { once: true });
                    }
                });
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    function triggerElevatorGToFloorKickstart(gisVm, resolvedFloor, afterDone) {
        try {
            const el = document.querySelector('#underground-elevator, .underground-elevator');
            const vm = el ? el.__vue__ : null;
            if (vm && Array.isArray(vm.floors) && typeof vm.onSwitchFloor === 'function') {
                const gIdx = vm.floors.findIndex(f => f && (f.floor_index === -1 || (f.floor_name_short || '').trim().toUpperCase() === 'G'));
                let tIdx = resolvedFloor
                    ? vm.floors.findIndex(f => f && Number(f.id) === Number(resolvedFloor.id))
                    : -1;
                if (tIdx < 0 && resolvedFloor) {
                    const wantShort = (resolvedFloor.floor_name_short || '').trim().toUpperCase();
                    if (wantShort) {
                        tIdx = vm.floors.findIndex(f => f && (f.floor_name_short || '').trim().toUpperCase() === wantShort);
                    }
                }
                if (gIdx >= 0 && tIdx >= 0 && gIdx !== tIdx) {
                    vm.onSwitchFloor(gIdx, vm.floors[gIdx]);
                    scheduleLayerTimer(() => {
                        vm.onSwitchFloor(tIdx, vm.floors[tIdx]);
                        scheduleLayerTimer(() => {
                            forceRefreshCaveCanvas(gisVm);
                            if (typeof afterDone === 'function') afterDone();
                        }, 220);
                    }, 560);
                    return true;
                }
            }
            const domFloors = el ? Array.from(el.querySelectorAll('.underground-elevator__floor')) : [];
            const domG = domFloors.find(f => (f.innerText || f.textContent || '').trim().toUpperCase() === 'G');
            if (domG) {
                dispatchFullClick(domG);
                scheduleLayerTimer(() => {
                    selectElevatorFloorExact({}, resolvedFloor);
                    scheduleLayerTimer(() => {
                        forceRefreshCaveCanvas(gisVm);
                        if (typeof afterDone === 'function') afterDone();
                    }, 220);
                }, 560);
                return true;
            }
        } catch (e) {}
        return false;
    }

    function handleLayeredExploration(ctx, data, targetZoom = null, areaChanged = false) {
        try {
            clearLayerTimers();
            const isUnderground = Boolean(data && data.is_underground);
            const gisVm = resolveGisVm(ctx);
            const finalZoom = (targetZoom !== null && targetZoom !== undefined)
                ? targetZoom
                : computeTargetLeafletZoom(ctx ? ctx.map : null, data);

            const applyViewAndMarker = (animate = false) => {
                if (ctx && ctx.map && data && data.lat !== undefined && data.lng !== undefined) {
                    if (autoPanEnabled) {
                        ctx.map.setView([data.lat, data.lng], finalZoom, {
                            animate: Boolean(animate),
                            duration: animate ? 0.25 : 0
                        });
                    }
                    updatePlayerMarker(ctx, data);
                }
            };

            const activeInfo = gisVm && gisVm.$store && gisVm.$store.state ? gisVm.$store.state.layeredExplorationInfo : null;
            const hasCanvasLayer = Boolean(gisVm && gisVm.canvasLayeredImageLayer);
            const elevatorEl = document.querySelector('#underground-elevator, .underground-elevator');
            const inExploration = Boolean(activeInfo || hasCanvasLayer || (elevatorEl && elevatorEl.offsetWidth > 0));

            // Case 1: Surface
            if (!isUnderground) {
                if (inExploration) {
                    if (ctx && ctx.map && typeof ctx.map.setMaxBounds === 'function') {
                        ctx.map.setMaxBounds(null);
                    }
                    if (ctx && ctx.map && gisVm && gisVm.setMaxBoundsFn && typeof ctx.map.off === 'function') {
                        ctx.map.off('zoomend', gisVm.setMaxBoundsFn);
                    }
                    if (gisVm && typeof gisVm.onQuitExploreAPointGroup === 'function') {
                        setLog('🌤️ Đã tự động thoát phân tầng về mặt đất!', '#38bdf8');
                        Promise.race([
                            Promise.resolve(gisVm.onQuitExploreAPointGroup()).catch(err => console.error('[Genshin Helper] onQuitExploreAPointGroup error:', err)),
                            new Promise(r => setTimeout(r, 1500))
                        ]).finally(() => {
                            applyViewAndMarker(false);
                            scheduleLayerTimer(() => applyViewAndMarker(false), 480);
                        });
                    } else {
                        exitUndergroundExploration();
                        scheduleLayerTimer(() => applyViewAndMarker(false), 240);
                    }
                } else if (areaChanged && ctx && ctx.map && data && data.lat !== undefined && data.lng !== undefined) {
                    const guardDrift = () => {
                        try {
                            if (!ctx || !ctx.map || typeof ctx.map.getCenter !== 'function') return;
                            const c = ctx.map.getCenter();
                            if (c && Math.hypot(c.lat - data.lat, c.lng - data.lng) > 15) {
                                applyViewAndMarker(false);
                            }
                        } catch (e) {}
                    };
                    scheduleLayerTimer(guardDrift, 220);
                    scheduleLayerTimer(guardDrift, 520);
                }
                return;
            }

            // Case 2: Underground / Multilayer (prefer exact group_id + floor_id from backend)
            const list = (gisVm && Array.isArray(gisVm.pointGroupList) && gisVm.pointGroupList.length > 0)
                ? gisVm.pointGroupList
                : (gisVm && gisVm.$store && Array.isArray(gisVm.$store.state.pointGroupList) ? gisVm.$store.state.pointGroupList : []);

            if (list.length === 0) {
                if (!data.__gh_pg_retried) {
                    data.__gh_pg_retried = true;
                    scheduleLayerTimer(() => handleLayeredExploration(ctx, data, targetZoom), 450);
                }
                return;
            }

            let group = null;
            let targetFloor = null;
            if (data.group_id !== undefined && data.group_id !== null) {
                group = list.find(g => Number(g.id) === Number(data.group_id)) || null;
            }
            if (!group && data.lat !== undefined && data.lng !== undefined) {
                const match = findMatchingPointGroup(data.lat, data.lng, data.floor_short || data.floor_name, data.total_floors, data.active_floor_idx);
                if (match && match.group) {
                    group = match.group;
                    targetFloor = match.defaultFloor || null;
                }
            }

            if (group && Array.isArray(group.floors) && group.floors.length > 0) {
                if (data.floor_id !== undefined && data.floor_id !== null) {
                    targetFloor = group.floors.find(f => Number(f.id) === Number(data.floor_id)) || targetFloor;
                }
                if (!targetFloor) {
                    const cleanFloor = (data.floor_short || data.floor_name || '').trim().toUpperCase();
                    if (cleanFloor) {
                        targetFloor = group.floors.find((f, fIdx) => {
                            const s = (f.floor_name_short || '').trim().toUpperCase();
                            const n = (f.floor_name || '').trim().toUpperCase();
                            return s === cleanFloor || n.includes(cleanFloor) || (cleanFloor === 'B1' && fIdx === 0);
                        }) || null;
                    }
                }
                if (!targetFloor && data.active_floor_idx !== undefined && data.active_floor_idx > 0) {
                    targetFloor = group.floors[data.active_floor_idx - 1] || null;
                }
                if (!targetFloor) targetFloor = group.floors[0];

                let floorIndex = targetFloor
                    ? group.floors.findIndex(f => Number(f.id) === Number(targetFloor.id))
                    : -1;
                if (floorIndex < 0 && targetFloor) floorIndex = group.floors.indexOf(targetFloor);
                floorIndex = Math.max(0, floorIndex);
                const resolvedFloor = group.floors[floorIndex] || targetFloor;
                const label = resolvedFloor.floor_name || resolvedFloor.floor_name_short || group.name || 'Tầng ngầm';

                const elevatorVm = elevatorEl ? elevatorEl.__vue__ : null;
                const elevatorFloors = elevatorVm?.floors || [];
                const isSameGroup = Boolean(
                    (activeInfo && Number(activeInfo.id) === Number(group.id)) ||
                    (Array.isArray(elevatorFloors) && elevatorFloors.length > 0 &&
                        group.floors.some(gf => elevatorFloors.some(ef => ef && Number(ef.id) === Number(gf.id))))
                );

                // Already inside the same group AND cave image overlay is mounted -> switch floor cleanly
                if (isSameGroup && hasCanvasLayer) {
                    const alreadyOnFloor = Boolean(
                        activeInfo && (
                            Number(activeInfo.activeFloorId) === Number(resolvedFloor.id) ||
                            Number(activeInfo.activeIndex) === floorIndex
                        ) && (!elevatorVm || Number(elevatorVm.activeFloorId) === Number(resolvedFloor.id))
                    );
                    if (!alreadyOnFloor) {
                        const elevTIdx = (elevatorVm && Array.isArray(elevatorVm.floors))
                            ? elevatorVm.floors.findIndex(f => f && Number(f.id) === Number(resolvedFloor.id))
                            : -1;
                        if (elevTIdx >= 0 && typeof elevatorVm.onSwitchFloor === 'function') {
                            elevatorVm.onSwitchFloor(elevTIdx, elevatorVm.floors[elevTIdx]);
                            setLog(`🗺️ Chuyển tầng phân tầng: [${label}]!`, '#22c55e');
                            scheduleLayerTimer(() => { applyViewAndMarker(false); forceRefreshCaveCanvas(gisVm); }, 180);
                            scheduleLayerTimer(() => { applyViewAndMarker(false); forceRefreshCaveCanvas(gisVm); }, 600);
                            return;
                        }
                        if (gisVm && typeof gisVm.onSwitchExploreFloor === 'function') {
                            const floorPayload = Object.assign({}, resolvedFloor, {
                                name: resolvedFloor.floor_name || resolvedFloor.floor_name_short || ''
                            });
                            Promise.resolve(gisVm.onSwitchExploreFloor(floorPayload))
                                .catch(() => selectElevatorFloorExact(data, resolvedFloor))
                                .finally(() => {
                                    applyViewAndMarker(false);
                                    forceRefreshCaveCanvas(gisVm);
                                    scheduleLayerTimer(() => {
                                        applyViewAndMarker(false);
                                        forceRefreshCaveCanvas(gisVm);
                                    }, 600);
                                });
                            setLog(`🗺️ Chuyển tầng phân tầng: [${label}]!`, '#22c55e');
                            return;
                        }
                        selectElevatorFloorExact(data, resolvedFloor);
                    }
                    forceRefreshCaveCanvas(gisVm);
                    const hasTiles = Boolean(
                        gisVm.canvasLayeredImageLayer &&
                        gisVm.canvasLayeredImageLayer._latlngImageOverlays &&
                        gisVm.canvasLayeredImageLayer._latlngImageOverlays.total > 0
                    );
                    if (!hasTiles) {
                        triggerElevatorGToFloorKickstart(gisVm, resolvedFloor, () => applyViewAndMarker(false));
                    } else {
                        scheduleLayerTimer(() => {
                            applyViewAndMarker(false);
                            forceRefreshCaveCanvas(gisVm);
                        }, 160);
                    }
                    return;
                }

                // IMPORTANT: onEnterExploreAPointGroup(group, floorIndex) takes INTEGER floorIndex (0, 1, ...)
                const doEnter = () => {
                    if (gisVm && typeof gisVm.onEnterExploreAPointGroup === 'function') {
                        setLog(`🗺️ Tự động mở thám hiểm phân tầng: [${label}]!`, '#22c55e');
                        Promise.resolve(gisVm.onEnterExploreAPointGroup(group, floorIndex))
                            .catch(err => console.error('[Genshin Helper] onEnterExploreAPointGroup error:', err))
                            .finally(() => {
                                applyViewAndMarker(false);
                                forceRefreshCaveCanvas(gisVm);
                                scheduleLayerTimer(() => {
                                    applyViewAndMarker(false);
                                    forceRefreshCaveCanvas(gisVm);
                                    const activeNow = gisVm && gisVm.$store && gisVm.$store.state ? gisVm.$store.state.layeredExplorationInfo : null;
                                    if ((!activeNow || Number(activeNow.id) !== Number(group.id)) && !doEnter._retried) {
                                        doEnter._retried = true;
                                        applyViewAndMarker(false);
                                        doEnter();
                                        return;
                                    }
                                    const layerOk = Boolean(
                                        gisVm.canvasLayeredImageLayer &&
                                        gisVm.canvasLayeredImageLayer._latlngImageOverlays &&
                                        gisVm.canvasLayeredImageLayer._latlngImageOverlays.total > 0
                                    );
                                    const elevNow = document.querySelector('#underground-elevator, .underground-elevator')?.__vue__;
                                    const elevOk = !elevNow || Number(elevNow.activeFloorId) === Number(resolvedFloor.id);
                                    if (!layerOk || !elevOk) {
                                        triggerElevatorGToFloorKickstart(gisVm, resolvedFloor, () => applyViewAndMarker(false));
                                    }
                                }, 580);
                            });
                    }
                };

                if (inExploration) {
                    if (ctx && ctx.map && typeof ctx.map.setMaxBounds === 'function') {
                        ctx.map.setMaxBounds(null);
                    }
                    if (ctx && ctx.map && gisVm && gisVm.setMaxBoundsFn && typeof ctx.map.off === 'function') {
                        ctx.map.off('zoomend', gisVm.setMaxBoundsFn);
                    }
                    if (gisVm && typeof gisVm.onQuitExploreAPointGroup === 'function') {
                        Promise.race([
                            Promise.resolve(gisVm.onQuitExploreAPointGroup()).catch(() => {}),
                            new Promise(r => setTimeout(r, 1500))
                        ]).finally(() => {
                            scheduleLayerTimer(() => {
                                applyViewAndMarker(false);
                                doEnter();
                            }, 180);
                        });
                        return;
                    }
                    exitUndergroundExploration();
                    scheduleLayerTimer(() => {
                        applyViewAndMarker(false);
                        doEnter();
                    }, 240);
                    return;
                }

                doEnter();
                return;
            }

            if (elevatorEl && elevatorEl.offsetWidth > 0) {
                selectElevatorFloorExact(data, null);
            }
        } catch (e) {
            console.error('[Genshin Helper] Error in handleLayeredExploration:', e);
        }
    }

    // =========================================================================
    // 3.1 Universal Underground Floor Switcher (Index & Name 1:1 Matching + Kickstart)
    // =========================================================================
    function dispatchFullClick(el) {
        if (!el) return;
        try {
            const opts = { bubbles: true, cancelable: true, view: window };
            el.dispatchEvent(new MouseEvent('mouseenter', opts));
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.dispatchEvent(new MouseEvent('click', opts));
        } catch (e) {
            try { el.click(); } catch(err) {}
        }
    }

    function switchUndergroundFloor(floorIdx, totalFloors, floorName, forceKickstart = false) {
        try {
            const elevator = document.querySelector('#underground-elevator, .underground-elevator');
            if (elevator && elevator.offsetWidth > 0) {
                const floors = Array.from(elevator.querySelectorAll('.underground-elevator__floor'));
                if (floors.length > 0) {
                    let target = null;
                    if (floorName) {
                        const cleanName = floorName.toUpperCase().trim();
                        target = floors.find(f => (f.innerText || f.textContent || '').trim().toUpperCase() === cleanName);
                    }
                    if (!target && floorIdx !== undefined && floorIdx >= 0 && floorIdx < floors.length) {
                        target = floors[floorIdx];
                    }
                    if (!target) {
                        target = floors.find(f => (f.innerText || f.textContent || '').trim().toUpperCase() !== 'G') || floors[floors.length - 1];
                    }

                    if (target) {
                        const txt = (target.innerText || target.textContent || '').trim();
                        const floorG = floors.find(f => (f.innerText || f.textContent || '').trim().toUpperCase() === 'G');

                        // Kickstart toggle: Clicking G then clicking Target Floor forces HoYoLAB Leaflet to mount tiles
                        if (forceKickstart && floorG && floorG !== target) {
                            dispatchFullClick(floorG);
                            setTimeout(() => {
                                dispatchFullClick(target);
                                setLog(`✅ Đã kích hoạt phân tầng: [${txt || ('Tầng ' + floorIdx)}]!`, '#22c55e');
                            }, 130);
                        } else {
                            dispatchFullClick(target);
                            setLog(`✅ Đã chọn tầng phân tầng: [${txt || ('Tầng ' + floorIdx)}]!`, '#22c55e');
                        }
                        return true;
                    }
                }
            }
        } catch (e) {
            console.error('[Genshin Helper] Error in switchUndergroundFloor:', e);
        }
        return false;
    }

    function ensureFloorSwitch(floorIdx, totalFloors, floorName, ctx = null, targetLat = null, targetLng = null, maxAttempts = 12) {
        let count = 0;
        let kickstarted = false;
        const timer = setInterval(() => {
            count++;
            // On attempt 2, trigger Kickstart Toggle (G -> Target Floor) to unlock Leaflet tiles
            const shouldKickstart = (!kickstarted && count >= 2);
            const ok = switchUndergroundFloor(floorIdx, totalFloors, floorName, shouldKickstart);
            if (shouldKickstart && ok) {
                kickstarted = true;
            }
            if ((ok && count >= 4) || count >= maxAttempts) {
                clearInterval(timer);
                if (ctx && ctx.map && targetLat !== null && targetLng !== null && autoPanEnabled) {
                    setTimeout(() => {
                        const curZoom = ctx.map.getZoom();
                        const finalZoom = (curZoom < -0.5 || curZoom > 2.0) ? 0.8 : curZoom;
                        ctx.map.setView([targetLat, targetLng], finalZoom, { animate: true, duration: 0.5 });
                    }, 250);
                }
            }
        }, 180);
    }

    function exitUndergroundExploration() {
        try {
            const elevator = document.querySelector('#underground-elevator, .underground-elevator');
            const vm = elevator ? elevator.__vue__ : null;
            if (vm && typeof vm.onClickExit === 'function') {
                vm.onClickExit();
                setLog('🌤️ Đã tự động thoát phân tầng về mặt đất!', '#38bdf8');
                return true;
            }
            const exitBtn = document.querySelector('.underground-elevator__exit, [class*="underground-elevator__exit"], .underground-elevator [class*="exit"], #underground-elevator [class*="exit"]');
            if (exitBtn) {
                exitBtn.click();
                setLog('🌤️ Đã tự động thoát phân tầng về mặt đất!', '#38bdf8');
                return true;
            }
        } catch (e) {}
        return false;
    }

    // =========================================================================
    // 4. Server Polling & Live State Sync
    // =========================================================================
    function checkServerVersionAndReload(data) {
        if (data && data.script_version && data.script_version !== SCRIPT_VERSION) {
            try {
                if (sessionStorage.getItem('gh_reloaded_for_ver') === data.script_version) return false;
                sessionStorage.setItem('gh_reloaded_for_ver', data.script_version);
            } catch (e) {}
            setLog(`🔄 Phát hiện bản mới (${data.script_version}), đang tự cập nhật...`, '#38bdf8');
            setTimeout(() => window.location.reload(), 150);
            return true;
        }
        return false;
    }

    function pollServer() {
        const controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        const timer = controller ? setTimeout(() => controller.abort(), 20000) : null;
        const url = `${API_POLL_URL}?since=${lastEventSeq}`;
        fetch(url, { cache: 'no-store', signal: controller ? controller.signal : undefined })
            .then(res => res.json())
            .then(data => {
                if (timer) clearTimeout(timer);
                setStatus(true);
                if (checkServerVersionAndReload(data)) return;
                if (data && typeof data._seq === 'number') {
                    lastEventSeq = data._seq;
                }
                if (data && data.type && data.type !== 'HEARTBEAT') {
                    try {
                        handleServerMessage(data);
                    } catch (err) {
                        console.error('[Genshin Helper] handleServerMessage error:', err);
                    }
                }
                setTimeout(pollServer, 10);
            })
            .catch(() => {
                if (timer) clearTimeout(timer);
                setStatus(false);
                setTimeout(pollServer, 1000);
            });
    }

    function reportState() {
        try {
            const ctx = findMapContext();
            if (!ctx.map) return;
            const center = ctx.map.getCenter();
            const zoom = ctx.map.getZoom();
            const markersCount = (ctx.markerList && ctx.markerList.length) || (ctx.ciLayer && ctx.ciLayer._latlngMarkers && ctx.ciLayer._latlngMarkers.all ? ctx.ciLayer._latlngMarkers.all().length : 0);
            const gisVm = resolveGisVm(ctx);
            const st = gisVm && gisVm.$store && gisVm.$store.state ? gisVm.$store.state : {};
            const areaInfo = st.areaInfo || {};
            const layerInfo = st.layeredExplorationInfo || null;
            fetch(API_REPORT_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    center: [center.lat, center.lng],
                    zoom: zoom,
                    markersCount: markersCount,
                    activeAreaId: areaInfo.id !== undefined ? areaInfo.id : null,
                    activeAreaName: areaInfo.name || null,
                    layeredGroupId: layerInfo && layerInfo.id !== undefined ? layerInfo.id : null,
                    layeredFloorId: layerInfo && layerInfo.activeFloorId !== undefined ? layerInfo.activeFloorId : null,
                    scriptVersion: SCRIPT_VERSION
                })
            }).catch(() => {});
        } catch (e) {}
    }

    setInterval(reportState, 5000);

    function fetchInitialStatus() {
        fetch('http://127.0.0.1:8765/status', { cache: 'no-store' })
            .then(res => res.json())
            .then(data => {
                if (data) {
                    if (checkServerVersionAndReload(data)) return;
                    if (typeof data.latest_seq === 'number' && lastEventSeq < 0) {
                        lastEventSeq = data.latest_seq;
                    }
                    if (data.display_name_sync) updateHotkeyDisplay(data.display_name_sync, 'sync');
                    else if (data.display_name) updateHotkeyDisplay(data.display_name, 'sync');
                    if (data.display_name_debug) updateHotkeyDisplay(data.display_name_debug, 'debug');
                    if (typeof data.auto_sync_m === 'boolean') setAutoSyncUI(data.auto_sync_m);
                }
            })
            .catch(() => {});
    }

    function showDiagnosticNoteModal(folderName, hasWorldCoords = true, reason = '') {
        try {
            const fname = (folderName || 'diagnostics').trim();
            const now = Date.now();
            if (lastNoteModalFolder === fname && (now - lastNoteModalTs) < 2000) {
                return;
            }
            lastNoteModalFolder = fname;
            lastNoteModalTs = now;

            injectThemeStyles();
            const existing = document.getElementById('gh-diag-note-modal');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.id = 'gh-diag-note-modal';
            overlay.className = 'gh-root';
            overlay.setAttribute('data-folder', fname);
            overlay.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
                background: rgba(0, 0, 0, 0.72); backdrop-filter: blur(10px);
                z-index: 2147483647; display: flex; align-items: center; justify-content: center;
            `;

            const badgeBg = hasWorldCoords ? 'rgba(34, 197, 94, 0.16)' : 'rgba(245, 158, 11, 0.16)';
            const badgeBorder = hasWorldCoords ? 'rgba(34, 197, 94, 0.45)' : 'rgba(245, 158, 11, 0.45)';
            const badgeColor = hasWorldCoords ? '#4ade80' : '#fbbf24';
            const badgeLabel = hasWorldCoords ? 'Đã định vị tọa độ' : 'Chưa đủ mốc / Ngoại màn hình';

            const quickTags = [
                '[Sai vị trí]',
                '[Sai phân tầng]',
                '[Web không hiện ảnh hang]',
                '[Đang kéo map / Không có mũi tên]',
                '[Khác]',
            ];

            overlay.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.98); border: 1px solid rgba(245, 158, 11, 0.55); border-radius: 16px; padding: 20px 24px; max-width: 500px; width: 92%; color: #fff; box-shadow: 0 24px 60px rgba(0,0,0,0.85); display: flex; flex-direction: column; gap: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 10px;">
                        <div>
                            <strong style="color: #fbbf24; font-size: 15px; font-weight: 700;">📸 Chụp Chẩn Đoán Thành Công</strong>
                            <div id="gh-note-folder" class="gh-mono" style="font-size: 11.5px; color: #94a3b8; margin-top: 2px;"></div>
                        </div>
                        <span style="background: ${badgeBg}; border: 1px solid ${badgeBorder}; color: ${badgeColor}; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px; white-space: nowrap;">${badgeLabel}</span>
                    </div>
                    <div id="gh-note-reason" style="font-size: 11.5px; color: #cbd5e1; display: none;"></div>
                    <div id="gh-note-tags" style="display: flex; flex-wrap: wrap; gap: 6px;"></div>
                    <textarea id="gh-diag-note-input" rows="4" placeholder="Nhập ghi chú lỗi cụ thể tại đây (vd: kéo map sang Fontaine nhưng browser vẫn nhảy theo)..." style="width: 100%; box-sizing: border-box; background: rgba(2, 6, 23, 0.8); border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 10px; padding: 10px 12px; color: #f8fafc; font-size: 12.5px; line-height: 1.45; resize: vertical; outline: none; font-family: inherit;"></textarea>
                    <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px;">
                        <span style="font-size: 11px; color: #64748b;">Ctrl + Enter để lưu nhanh • Esc để bỏ qua</span>
                        <div style="display: flex; gap: 8px;">
                            <button id="gh-note-skip" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.16); color: #94a3b8; border-radius: 8px; padding: 7px 14px; font-size: 12px; cursor: pointer;">Bỏ qua</button>
                            <button id="gh-note-save" style="background: #d97706; border: none; color: #fff; font-weight: 600; border-radius: 8px; padding: 7px 16px; font-size: 12px; cursor: pointer;">Lưu ghi chú</button>
                        </div>
                    </div>
                    <div id="gh-note-error" style="color: #ef4444; font-size: 11.5px; min-height: 14px;"></div>
                </div>
            `;

            document.body.appendChild(overlay);
            overlay.querySelector('#gh-note-folder').textContent = `Bundle: ${fname}`;
            const reasonEl = overlay.querySelector('#gh-note-reason');
            if (reason) {
                reasonEl.textContent = reason;
                reasonEl.style.display = 'block';
            }

            const textarea = overlay.querySelector('#gh-diag-note-input');
            const tagsWrap = overlay.querySelector('#gh-note-tags');
            const skipBtn = overlay.querySelector('#gh-note-skip');
            const saveBtn = overlay.querySelector('#gh-note-save');
            const errEl = overlay.querySelector('#gh-note-error');

            quickTags.forEach(tag => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = tag;
                btn.style.cssText = 'background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); color: #7dd3fc; border-radius: 999px; padding: 3px 9px; font-size: 11px; cursor: pointer;';
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const cur = textarea.value;
                    if (!cur.includes(tag)) {
                        const sep = cur.trim().length > 0 && !cur.endsWith(' ') && !cur.endsWith('\n') ? ' ' : '';
                        textarea.value = cur + sep + tag + ' ';
                    }
                    textarea.focus();
                });
                tagsWrap.appendChild(btn);
            });

            const closeNoteModal = () => {
                document.removeEventListener('keydown', onModalKeyDown, true);
                overlay.remove();
            };

            const submitNote = () => {
                const noteText = textarea.value.trim();
                saveBtn.disabled = true;
                saveBtn.textContent = 'Đang lưu...';
                errEl.textContent = '';

                fetch(API_SAVE_DIAG_NOTE_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder_name: fname, note: noteText })
                })
                .then(r => r.json())
                .then(res => {
                    if (res && res.ok) {
                        closeNoteModal();
                        setLog(`📝 Đã lưu ghi chú lỗi vào [${fname}]/05_user_note.txt`, '#22c55e');
                    } else {
                        saveBtn.disabled = false;
                        saveBtn.textContent = 'Lưu ghi chú';
                        errEl.textContent = (res && res.error) ? res.error : 'Không lưu được ghi chú.';
                    }
                })
                .catch(() => {
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Lưu ghi chú';
                    errEl.textContent = 'Không thể kết nối server desktop (127.0.0.1:8765)!';
                });
            };

            const onModalKeyDown = (e) => {
                if (!document.body.contains(overlay)) {
                    document.removeEventListener('keydown', onModalKeyDown, true);
                    return;
                }
                if (e.key === 'Escape') {
                    e.preventDefault();
                    e.stopPropagation();
                    closeNoteModal();
                } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    e.stopPropagation();
                    submitNote();
                }
            };

            document.addEventListener('keydown', onModalKeyDown, true);
            skipBtn.addEventListener('click', closeNoteModal);
            saveBtn.addEventListener('click', submitNote);
            overlay.addEventListener('click', (e) => { if (e.target === overlay) closeNoteModal(); });
            setTimeout(() => textarea.focus(), 40);
        } catch (err) {
            console.error('[Genshin Helper] showDiagnosticNoteModal error:', err);
        }
    }

    function handleServerMessage(data) {
        if (data.type === 'HOTKEY_UPDATED') {
            const act = data.action || 'sync';
            if (data.display_name) {
                updateHotkeyDisplay(data.display_name, act);
                setLog(`⚙️ Phím tắt [${act.toUpperCase()}] đã đổi thành: [${data.display_name}]`, '#38bdf8');
            }
            return;
        }

        if (data.type === 'AUTO_SYNC_UPDATED') {
            if (typeof data.auto_sync_m === 'boolean') {
                setAutoSyncUI(data.auto_sync_m);
                setLog(data.auto_sync_m ? '⚡ Tự đồng bộ Map (M): BẬT' : '⏸️ Tự đồng bộ Map (M): TẮT', '#38bdf8');
            }
            return;
        }

        if (data.type === 'DIAGNOSTIC_CAPTURED') {
            playBeep();
            const folderName = data.folder_name || 'diagnostics';
            if (data.has_world_coords) {
                setLog(`📸 Đã chụp chẩn đoán: [${folderName}]! Hãy nhập ghi chú lỗi vào hộp thoại.`, '#22c55e');
            } else {
                setLog(`📸 Đã chụp chẩn đoán: [${folderName}]! ${data.reason || ''}`, '#fbbf24');
            }
            showDiagnosticNoteModal(folderName, Boolean(data.has_world_coords), data.reason || '');
            return;
        }

        if (data.type === 'SYNC_POSITION_ONLY' || data.type === 'SYNC_AND_MARK') {
            const markAfterSync = (data.type === 'SYNC_AND_MARK');
            if (data.detected === false) {
                if (markAfterSync) {
                    setLog('⚠️ Chưa tìm thấy vị trí game! Hãy mở Map (M) trong game rồi kích hoạt lại.', '#f59e0b');
                }
                return;
            }

            const coordsEl = document.getElementById('gh-coords');
            if (coordsEl) {
                if (data.lat !== undefined && data.lng !== undefined && data.has_world_coords) {
                    const mapTag = data.map_name ? `[${data.map_name}] ` : (data.map_id ? `[Map ${data.map_id}] ` : '');
                    const layerTag = data.is_underground ? ` • ${data.floor_short || data.floor_name || 'Ngầm'}` : '';
                    coordsEl.textContent = `${mapTag}(${data.lat.toFixed(0)}, ${data.lng.toFixed(0)})${layerTag}`;
                } else if (data.pixel_x) {
                    coordsEl.textContent = `Pixel: (${data.pixel_x.toFixed(0)}, ${data.pixel_y.toFixed(0)}) [Thiếu mốc]`;
                }
            }
            performAutoSyncAndMark(data, markAfterSync);
        }
    }

    // =========================================================================
    // 5. Core Auto-Sync & Mark (Multi-Map & World Navigation)
    // =========================================================================
    function getCurrentWebMapId() {
        const m = window.location.hash.match(/#\/map\/(\d+)/);
        if (m) return parseInt(m[1]);
        return 2; // Default to Teyvat
    }

    function waitForNewMapReady(timeoutMs = 4000) {
        return new Promise((resolve) => {
            const start = Date.now();
            const check = () => {
                const ctx = findMapContext();
                if (ctx && ctx.map) {
                    resolve(ctx);
                } else if (Date.now() - start < timeoutMs) {
                    setTimeout(check, 100);
                } else {
                    resolve(findMapContext());
                }
            };
            setTimeout(check, 250);
        });
    }

    function performAutoSyncAndMark(data, markAfterSync = false) {
        try {
            const targetMapId = data.map_id ? parseInt(data.map_id) : 2;
            const currentMapId = getCurrentWebMapId();

            if (data.has_world_coords && targetMapId !== currentMapId) {
                const mapName = data.map_name || `Map ${targetMapId}`;
                setLog(`🔄 Tự động chuyển sang bản đồ [${mapName}]...`, '#38bdf8');
                if (markAfterSync) playBeep();

                // Switch route via Vue Router or Hash
                try {
                    const vueApp = document.querySelector('#app')?.__vue__;
                    if (vueApp && vueApp.$router) {
                        vueApp.$router.push(`/map/${targetMapId}`);
                    }
                } catch (e) {}

                if (window.location.hash.includes('#/map/')) {
                    window.location.hash = window.location.hash.replace(/#\/map\/\d+/, `#/map/${targetMapId}`);
                } else {
                    window.location.hash = `#/map/${targetMapId}`;
                }
                window.dispatchEvent(new HashChangeEvent('hashchange'));

                // Fallback: If SPA router fails to load target map within 700ms, reload to force HoYoLAB to mount target map
                setTimeout(() => {
                    if (getCurrentWebMapId() !== targetMapId) {
                        const url = new URL(window.location.href);
                        url.hash = `#/map/${targetMapId}`;
                        window.location.href = url.toString();
                        window.location.reload();
                    }
                }, 700);

                waitForNewMapReady(4000).then((newCtx) => {
                    setLog(`✅ Đã mở bản đồ [${mapName}]! Đang trượt tới vị trí...`, '#22c55e');
                    executeSyncAndMarkOnContext(newCtx, data, markAfterSync);
                });
                return;
            }

            const ctx = findMapContext();
            executeSyncAndMarkOnContext(ctx, data, markAfterSync);
        } catch (e) {
            console.error('[Genshin Helper] Error in performAutoSyncAndMark:', e);
        }
    }

    function executeSyncAndMarkOnContext(ctx, data, markAfterSync = false) {
        try {
            if (!ctx || !ctx.map) {
                setLog('❌ Chưa tìm thấy Leaflet Map! Hãy chắc chắn map đã tải xong.', '#ef4444');
                return;
            }

            // Step 1: Check if game sent real World Coordinates (lat, lng)
            const hasWorld = (data && data.has_world_coords && data.lat !== undefined && data.lng !== undefined);
            const targetLat = hasWorld ? data.lat : null;
            const targetLng = hasWorld ? data.lng : null;
            const targetZoom = computeTargetLeafletZoom(ctx.map, data);

            if (hasWorld) {
                const mapName = data.map_name ? ` (${data.map_name})` : '';
                if (markAfterSync) {
                    setLog(`Đang trượt bản đồ web tới vị trí game${mapName}: [${targetLat.toFixed(1)}, ${targetLng.toFixed(1)}]...`, '#38bdf8');
                }

                // Auto switch region/area on Map 2 (commits areaInfo for both Surface & Caves so sidebar & markers match)
                const areaSwitchRes = (!data.map_id || parseInt(data.map_id) === 2)
                    ? ensureAreaSwitch(ctx, data.area_id, data.area_name, Boolean(data.is_underground))
                    : 'none';
                const areaChanged = (areaSwitchRes === 'changed');

                const gisVmNow = resolveGisVm(ctx);
                const elevatorNow = document.querySelector('#underground-elevator, .underground-elevator');
                const inLayerNow = Boolean(
                    (gisVmNow && gisVmNow.$store && gisVmNow.$store.state && gisVmNow.$store.state.layeredExplorationInfo) ||
                    (gisVmNow && gisVmNow.canvasLayeredImageLayer) ||
                    (elevatorNow && elevatorNow.offsetWidth > 0)
                );
                const needsLayerTransition = Boolean(autoSwitchLayerEnabled && (data.is_underground || inLayerNow));

                // Unconditionally clear stale cave maxBounds when landing on surface or transitioning layers
                if (!data.is_underground || needsLayerTransition) {
                    if (typeof ctx.map.setMaxBounds === 'function') {
                        ctx.map.setMaxBounds(null);
                    }
                    if (gisVmNow && gisVmNow.setMaxBoundsFn && typeof ctx.map.off === 'function') {
                        ctx.map.off('zoomend', gisVmNow.setMaxBoundsFn);
                    }
                }

                // Instant snap on teleports / zoom jumps / layer transitions; smooth short pan only for nearby moves
                if (autoPanEnabled) {
                    const curCenter = (typeof ctx.map.getCenter === 'function') ? ctx.map.getCenter() : null;
                    const distFromCur = curCenter ? Math.hypot(curCenter.lat - targetLat, curCenter.lng - targetLng) : Infinity;
                    const currentZoom = ctx.map.getZoom();
                    if (needsLayerTransition || distFromCur > 250 || Math.abs(currentZoom - targetZoom) > 0.25) {
                        ctx.map.setView([targetLat, targetLng], targetZoom, { animate: false });
                    } else {
                        ctx.map.panTo([targetLat, targetLng], { animate: true, duration: 0.22 });
                    }
                }
                updatePlayerMarker(ctx, data);

                // Handle automatic cave & multi-floor exploration (or returning to surface)
                if (autoSwitchLayerEnabled) {
                    handleLayeredExploration(ctx, data, targetZoom, areaChanged);
                }
            } else {
                setLog('⚠️ Không đủ mốc định vị thế giới! (Hãy thu nhỏ hoặc kéo bản đồ thấy rõ mốc dịch chuyển)', '#f59e0b');
                if (autoSwitchLayerEnabled) {
                    handleLayeredExploration(ctx, data, targetZoom);
                }
            }

            // Auto-sync on Map (M) only updates position/marker/layer — never opens popups or clicks "Ghim"
            if (!markAfterSync) {
                closeOpenPopup();
                if (hasWorld) {
                    const floorDesc = data.is_underground
                        ? ` [${data.hoyolab_floor_name || data.floor_short || data.floor_name || 'Phân tầng'}]`
                        : ' [Mặt đất]';
                    setLog(`✅ Đã đồng bộ vị trí${floorDesc}!`, '#22c55e');
                }
                return;
            }

            // Step 2 (manual hotkey SYNC_AND_MARK): Wait for map animation to settle before querying collectibles
            const waitTime = hasWorld ? 650 : 50;
            setTimeout(() => {
                let closest = null;
                let minDistance = Infinity;

                if (hasWorld) {
                    const radius = 60;
                    const tree = ctx.ciLayer && ctx.ciLayer._latlngMarkers;
                    if (tree && typeof tree.search === 'function') {
                        // Leaflet RBush tree stores minX/maxX as Lng, minY/maxY as Lat
                        const treeHits = tree.search({
                            minX: targetLng - radius,
                            minY: targetLat - radius,
                            maxX: targetLng + radius,
                            maxY: targetLat + radius
                        });
                        function isNavLandmark(it, markerObj) {
                            if (!it && !markerObj) return false;
                            const name = ((it && it.name) || (it && it.rawItem && it.rawItem.name) || (markerObj && markerObj.options && markerObj.options.title) || '').toLowerCase();
                            if (name.includes('dịch chuyển') || name.includes('dich chuyen') || name.includes('teleport') || name.includes('thần tượng') || name.includes('than tuong') || name.includes('statue')) return true;
                            const lid = (it && (it.label_id || (it.rawItem && it.rawItem.label_id))) || (markerObj && markerObj.label_id);
                            if (lid === 2 || lid === 3 || lid === 154) return true;
                            return false;
                        }

                        for (const item of treeHits) {
                            const m = item.data || item;
                            if (isNavLandmark(item, m)) continue;
                            const latlng = (m && typeof m.getLatLng === 'function') ? m.getLatLng() : { lat: (item.minY + item.maxY) / 2, lng: (item.minX + item.maxX) / 2 };
                            const d = Math.hypot(latlng.lat - targetLat, latlng.lng - targetLng);
                            if (d < minDistance) {
                                minDistance = d;
                                const pt = (ctx.map && typeof ctx.map.latLngToContainerPoint === 'function') ? ctx.map.latLngToContainerPoint(latlng) : { x: (item.minX + item.maxX) / 2, y: (item.minY + item.maxY) / 2 };
                                closest = { marker: m, latlng, point: pt, rawItem: item };
                            }
                        }
                    }

                    // Direct fast bounding-box fallback on markerList
                    if (!closest && Array.isArray(ctx.markerList) && ctx.markerList.length > 0) {
                        for (let i = 0; i < ctx.markerList.length; i++) {
                            const item = ctx.markerList[i];
                            const m = item.marker;
                            const name = (item.name || (m && m.options && m.options.title) || '').toLowerCase();
                            const lid = item.label_id || item.labelId;
                            if (lid === 2 || lid === 3 || lid === 154 || name.includes('dịch chuyển') || name.includes('teleport') || name.includes('thần tượng')) continue;

                            const y = (item.y_pos !== undefined) ? item.y_pos : (item.marker && item.marker.getLatLng ? item.marker.getLatLng().lat : null);
                            const x = (item.x_pos !== undefined) ? item.x_pos : (item.marker && item.marker.getLatLng ? item.marker.getLatLng().lng : null);
                            if (y === null || x === null) continue;
                            const dy = y - targetLat;
                            if (dy > radius || dy < -radius) continue;
                            const dx = x - targetLng;
                            if (dx > radius || dx < -radius) continue;
                            const d = Math.hypot(dx, dy);
                            if (d < minDistance) {
                                minDistance = d;
                                const latlng = { lat: y, lng: x };
                                const pt = (ctx.map && typeof ctx.map.latLngToContainerPoint === 'function') ? ctx.map.latLngToContainerPoint(latlng) : null;
                                closest = { marker: item.marker, latlng, point: pt, rawItem: item };
                            }
                        }
                    }

                    // Proximity check: Only open popup if player is close (< 60m)
                    if (!closest || minDistance > radius) {
                        closeOpenPopup();
                        setLog(`✅ Đã căn vị trí & tầng thám hiểm! (Không có điểm gần < 60m)`, '#22c55e');
                        return;
                    }

                    setLog(`Điểm gần nhất cách người chơi ${minDistance.toFixed(1)}m. Đang mở...`, '#10b981');
                } else {
                    // Screen center fallback
                    const markers = getAllMarkers(ctx);
                    if (markers.length === 0) {
                        setLog('❌ Chưa thấy điểm nào! Hãy chọn bật loại Rương hoặc Thần Đồng ở menu!', '#ef4444');
                        return;
                    }
                    const mapEl = document.querySelector('.leaflet-container') || document.body;
                    const rect = mapEl.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    for (const m of markers) {
                        const dist = Math.hypot(m.point.x - centerX, m.point.y - centerY);
                        if (dist < minDistance) {
                            minDistance = dist;
                            closest = m;
                        }
                    }
                }

                if (!closest) {
                    setLog('❌ Không tìm thấy điểm phù hợp.', '#ef4444');
                    return;
                }

                // Open Marker Popup
                if (closest.marker) {
                    if (typeof closest.marker.openPopup === 'function') closest.marker.openPopup();
                    if (typeof closest.marker.fire === 'function') closest.marker.fire('click');
                }
                if (ctx.ciLayer && typeof ctx.ciLayer._executeListeners === 'function') {
                    ctx.ciLayer._executeListeners({ type: 'click', containerPoint: closest.point });
                }
                if (closest.domElement) {
                    closest.domElement.click();
                }

                // Wait for popup and click "Ghim"
                let attempts = 0;
                let layerActivated = false;
                const checkInterval = setInterval(() => {
                    attempts++;
                    // If underground exploration button is inside popup, only click if elevator is not already open
                    if (autoSwitchLayerEnabled && data.is_underground && !layerActivated) {
                        const elevator = document.querySelector('#underground-elevator, .underground-elevator');
                        const isElevatorOpen = !!elevator && elevator.offsetWidth > 0;
                        if (!isElevatorOpen) {
                            const layerBtn = findLayerButton();
                            if (layerBtn) {
                                layerActivated = true;
                                setLog('🗺️ Đang kích hoạt [Thám Hiểm Phân Tầng]...', '#38bdf8');
                                layerBtn.click();
                                ensureFloorSwitch(data.active_floor_idx, data.total_floors, data.floor_name);
                            }
                        } else {
                            layerActivated = true;
                            ensureFloorSwitch(data.active_floor_idx, data.total_floors, data.floor_name);
                        }
                    }

                    const ghimBtn = findGhimButton();
                    if (ghimBtn) {
                        clearInterval(checkInterval);
                        clickGhimAndNotify(ghimBtn);
                        return;
                    }

                    const alreadyMarked = findAlreadyMarkedButton();
                    if (alreadyMarked) {
                        clearInterval(checkInterval);
                        playBeep();
                        setLog('ℹ️ Điểm này ĐÃ ĐƯỢC GHIM từ trước!', '#fbbf24');
                        return;
                    }

                    if (attempts >= 20) {
                        clearInterval(checkInterval);
                    }
                }, 100);

            }, waitTime);

        } catch (err) {
            console.error('[Genshin Helper Fatal Error]', err);
            setLog(`❌ Lỗi: ${err.message}`, '#ef4444');
        }
    }

    // =========================================================================
    // 6. Popup Helpers
    // =========================================================================
    function getText(el) {
        if (!el) return '';
        try { return (el.innerText || el.textContent || '').trim(); } catch (e) { return ''; }
    }

    function findPopupContainer() {
        const pop = document.querySelector('.expand-popup, .map-popup, .leaflet-popup-content, .leaflet-popup:not(.leaflet-popup-pane), [class*="map-popup"]');
        if (pop && pop.offsetParent !== null) return pop;
        return document.querySelector('.el-dialog');
    }

    function closeOpenPopup() {
        try {
            const explicitBtns = document.querySelectorAll('.leaflet-popup-close-button, .map-popup__close, .map-popup-custom__close, .map-entrance-popup__close');
            explicitBtns.forEach(el => {
                if (el && el.offsetWidth > 0 && el.offsetHeight > 0 && typeof el.click === 'function') {
                    el.click();
                }
            });
            const ctx = findMapContext();
            if (ctx && ctx.map && typeof ctx.map.closePopup === 'function') {
                ctx.map.closePopup();
            }
        } catch(e) {}
    }

    function findGhimButton() {
        try {
            const popup = findPopupContainer() || document;
            const candidates = Array.from(popup.querySelectorAll('.map-popup__btn, button, [role="button"]'));
            return candidates.find(el => {
                if (el.offsetWidth === 0 || el.offsetHeight === 0) return false;
                const txt = getText(el);
                return (txt === 'Ghim' || txt === 'Mark') && !txt.includes('Nhiều') && !txt.includes('trong game') && !txt.includes('mới') && !txt.includes('Đã');
            });
        } catch (e) { return null; }
    }

    function findLayerButton() {
        try {
            const candidates = Array.from(document.querySelectorAll('.map-popup__entrance, [class*="map-popup__entrance"], .map-popup__special-btns > div, .map-popup__special-btns span'));
            return candidates.find(el => {
                if (el.offsetWidth === 0 || el.offsetHeight === 0) return false;
                const txt = getText(el);
                return (txt === 'Thám Hiểm Phân Tầng' || txt.includes('Thám Hiểm Phân Tầng') || txt.includes('Phân Tầng') || txt.includes('Underground')) && !txt.includes('Lối Vào');
            });
        } catch (e) { return null; }
    }

    function findAlreadyMarkedButton() {
        try {
            const popup = findPopupContainer() || document;
            const candidates = Array.from(popup.querySelectorAll('.map-popup__btn, button, [role="button"]'));
            return candidates.find(el => {
                if (el.offsetWidth === 0 || el.offsetHeight === 0) return false;
                const txt = getText(el);
                return txt === 'Bỏ ghim' || txt === 'Đã ghim' || txt === 'Unmark';
            });
        } catch (e) { return null; }
    }

    function clickGhimAndNotify(btn) {
        let itemTitle = 'Điểm đánh dấu';
        const popup = findPopupContainer();
        if (popup) {
            const titleEl = popup.querySelector('.leaflet-popup-content h3, [class*="title"], h3, h4');
            if (titleEl) {
                const txt = getText(titleEl);
                if (txt) itemTitle = txt;
            }
        }
        btn.click();
        playBeep();
        setLog(`✅ Đã kéo map & GHIM: [${itemTitle}]!`, '#22c55e');
    }

    function playBeep() {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(659.25, ctx.currentTime);
            osc.frequency.setValueAtTime(880, ctx.currentTime + 0.08);
            gain.gain.setValueAtTime(0.12, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.25);
        } catch (e) {}
    }

    // Launch UI and polling
    fetchInitialStatus();
    createUI();
    pollServer();
    }

    bootGenshinHelper();

})();
