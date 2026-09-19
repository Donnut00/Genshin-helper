// ==UserScript==
// @name         HoYoLAB Genshin Helper (Auto Sync)
// @namespace    https://github.com/genshin-helper
// @version      3.2.1
// @description  Live Sync & Auto-Mark for HoYoLAB Interactive Map (World Coordinates + Underground Layer + Native Cave Exploration)
// @match        *://act.hoyolab.com/*
// @match        https://act.hoyolab.com/*
// @include      *act.hoyolab.com*
// @updateURL    http://127.0.0.1:8765/hoyolab_sync.user.js
// @downloadURL  http://127.0.0.1:8765/hoyolab_sync.user.js
// @grant        none
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    if (!window.location.hostname.includes('hoyolab.com')) {
        return;
    }

    // Auto Hot-Reload from local server during development
    if (!window.__gh_hotloaded && window.location.hostname.includes('hoyolab.com')) {
        fetch('http://127.0.0.1:8765/hoyolab_sync.user.js?v=' + Date.now())
            .then(r => r.text())
            .then(code => {
                window.__gh_hotloaded = true;
                eval(code);
            })
            .catch(() => {
                // Fallback to local script if server not running
                window.__gh_hotloaded = true;
            });
        return;
    }

    const API_POLL_URL = 'http://127.0.0.1:8765/poll';
    const API_REPORT_URL = 'http://127.0.0.1:8765/report_state';
    let autoPanEnabled = true;
    let autoSwitchLayerEnabled = true;
    let isConnected = false;
    let currentHotkeyDisplay = 'Alt+F11';
    try {
        const savedKey = localStorage.getItem('gh_hotkey_display');
        if (savedKey) currentHotkeyDisplay = savedKey;
    } catch(e) {}

    console.log('%c[Genshin Helper v3.0.0] Loaded (World Coordinates + Underground + Unified Hotkey)', 'color: #38bdf8; font-size: 16px; font-weight: bold;');

    // =========================================================================
    // 1. UI Panel & On-Screen Log
    // =========================================================================
    function setLog(msg, color) {
        console.log('[Genshin Helper v3.0]', msg);
        const logEl = document.getElementById('gh-log');
        if (logEl) {
            logEl.innerHTML = `<span style="color: ${color || '#38bdf8'}">${msg}</span>`;
        }
    }

    function updateHotkeyDisplay(displayName) {
        if (!displayName) return;
        currentHotkeyDisplay = displayName;
        const badgeText = document.getElementById('gh-hotkey-text');
        const pillHotkey = document.getElementById('gh-pill-hotkey');
        if (badgeText) badgeText.textContent = displayName;
        if (pillHotkey) pillHotkey.textContent = displayName;
        try { localStorage.setItem('gh_hotkey_display', displayName); } catch(e) {}
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
        `;
        document.head.appendChild(style);
    }

    function showHotkeyModal() {
        injectThemeStyles();
        const existing = document.getElementById('gh-hotkey-modal');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = 'gh-hotkey-modal';
        overlay.className = 'gh-root';
        overlay.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.72); backdrop-filter: blur(10px);
            z-index: 2147483647; display: flex; align-items: center; justify-content: center;
            user-select: none;
        `;

        overlay.innerHTML = `
            <div style="background: rgba(15, 23, 42, 0.96); border: 1px solid rgba(56, 189, 248, 0.5); border-radius: 16px; padding: 22px 26px; max-width: 450px; width: 92%; color: #fff; box-shadow: 0 20px 50px rgba(0,0,0,0.8); display: flex; flex-direction: column; gap: 14px;">
                <!-- Header -->
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 10px;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="font-size: 18px;">⚙️</span>
                        <strong style="color: #38bdf8; font-size: 15px;">Tùy Chỉnh Phím Tắt Sync</strong>
                    </div>
                    <button id="gh-modal-close" style="background: transparent; border: none; color: #94a3b8; font-size: 18px; cursor: pointer; line-height: 1;">✕</button>
                </div>

                <!-- Subtitle -->
                <div style="font-size: 12px; color: #94a3b8; line-height: 1.5;">
                    Kích hoạt khi mở Map (M) trong game để tự đồng bộ & ghim. Hỗ trợ <strong style="color: #38bdf8;">toàn bộ nút chuột</strong> và <strong style="color: #38bdf8;">bàn phím</strong>:
                </div>

                <!-- Quick Presets -->
                <div>
                    <div style="font-size: 11px; color: #cbd5e1; font-weight: 600; margin-bottom: 6px;">Chọn nhanh nút chuột / bàn phím:</div>
                    <div style="display: flex; flex-wrap: wrap; gap: 6px;">
                        <button class="gh-preset-btn" data-key="mouse4" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">🖱️ Mouse 4 (Chuột hông)</button>
                        <button class="gh-preset-btn" data-key="mouse5" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">🖱️ Mouse 5 (Chuột hông)</button>
                        <button class="gh-preset-btn" data-key="middle" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">🖱️ Chuột giữa (Con lăn)</button>
                        <button class="gh-preset-btn" data-key="alt+mouse4" style="background: rgba(168, 85, 247, 0.15); border: 1px solid rgba(168, 85, 247, 0.4); color: #c084fc; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">🖱️ Alt + Mouse 4</button>
                        <button class="gh-preset-btn" data-key="alt+f11" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">⌨️ Alt + F11</button>
                        <button class="gh-preset-btn" data-key="f10" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">⌨️ F10</button>
                        <button class="gh-preset-btn" data-key="f12" style="background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.2); color: #e2e8f0; border-radius: 6px; padding: 4px 9px; font-size: 11px; cursor: pointer; transition: all 0.15s;">⌨️ F12</button>
                    </div>
                </div>

                <!-- Interactive Key & Mouse Recorder -->
                <div style="display: flex; flex-direction: column; gap: 6px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <label style="font-size: 11.5px; color: #cbd5e1; font-weight: 600;">Phím tắt được gán:</label>
                        <span id="gh-recorder-hint" style="color: #38bdf8; font-size: 10.5px; font-weight: 500;">Click vào ô để bắt phím mới</span>
                    </div>

                    <div id="gh-key-recorder" tabindex="0" title="Click vào đây rồi nhấn bất kỳ phím bàn phím hoặc nút chuột nào" style="background: rgba(2, 6, 23, 0.65); border: 2px dashed rgba(56, 189, 248, 0.45); border-radius: 10px; padding: 12px 16px; color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: space-between; transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); outline: none;">
                        <div style="display: flex; align-items: center; gap: 12px;">
                            <span id="gh-recorder-icon" style="font-size: 20px; line-height: 1;">⌨️</span>
                            <div style="display: flex; flex-direction: column; gap: 2px;">
                                <div id="gh-recorder-display" style="font-size: 15px; font-weight: 700; color: #38bdf8; letter-spacing: -0.01em;">${currentHotkeyDisplay}</div>
                                <div id="gh-recorder-subtext" style="font-size: 11px; color: #64748b;">Click vào đây rồi nhấn phím hoặc click chuột</div>
                            </div>
                        </div>
                        <div id="gh-recorder-badge" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #7dd3fc; font-weight: 600; transition: all 0.2s ease;">
                            Đổi phím
                        </div>
                    </div>
                    <span id="gh-modal-error" style="color: #ef4444; font-size: 11px; min-height: 14px;"></span>
                </div>

                <!-- Actions -->
                <div style="display: flex; justify-content: flex-end; gap: 8px; border-top: 1px solid rgba(51, 65, 85, 0.6); padding-top: 12px;">
                    <button id="gh-modal-cancel" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); color: #94a3b8; border-radius: 8px; padding: 7px 16px; cursor: pointer; font-size: 12px; font-weight: 500;">Hủy</button>
                    <button id="gh-modal-save" style="background: #0284c7; border: none; color: #fff; font-weight: 600; border-radius: 8px; padding: 7px 18px; cursor: pointer; font-size: 12px; box-shadow: 0 2px 10px rgba(2,132,199,0.4); transition: all 0.15s;">Lưu phím tắt</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const recorderBox = document.getElementById('gh-key-recorder');
        const recorderDisplay = document.getElementById('gh-recorder-display');
        const recorderSubtext = document.getElementById('gh-recorder-subtext');
        const recorderIcon = document.getElementById('gh-recorder-icon');
        const recorderBadge = document.getElementById('gh-recorder-badge');
        const closeBtn = document.getElementById('gh-modal-close');
        const cancelBtn = document.getElementById('gh-modal-cancel');
        const saveBtn = document.getElementById('gh-modal-save');
        const errEl = document.getElementById('gh-modal-error');

        let recordedKeyVal = currentHotkeyDisplay.toLowerCase().replace(/\s+/g, '');
        let recordedKeyDisplay = currentHotkeyDisplay;
        let isRecording = false;

        function stopRecording(newVal, newDisplay) {
            if (!isRecording) return;
            isRecording = false;

            window.removeEventListener('keydown', onKeyDown, { capture: true });
            window.removeEventListener('mousedown', onMouseDown, { capture: true });
            window.removeEventListener('pointerdown', onPointerDown, { capture: true });
            setTimeout(() => {
                window.removeEventListener('mouseup', preventDefaultAction, { capture: true });
                window.removeEventListener('contextmenu', preventDefaultAction, { capture: true });
            }, 300);

            recorderBox.style.borderColor = 'rgba(56, 189, 248, 0.45)';
            recorderBox.style.borderStyle = 'dashed';
            recorderBox.style.background = 'rgba(2, 6, 23, 0.65)';
            recorderBox.style.boxShadow = 'none';

            if (newVal && newDisplay) {
                recordedKeyVal = newVal;
                recordedKeyDisplay = newDisplay;
                recorderDisplay.textContent = newDisplay;
                recorderDisplay.style.color = '#38bdf8';
                recorderSubtext.textContent = 'Đã nhận diện! Nhấn [Lưu phím tắt] để áp dụng.';
                recorderIcon.textContent = '✅';
                recorderBadge.textContent = 'Đã chọn';
                recorderBadge.style.background = 'rgba(34, 197, 94, 0.15)';
                recorderBadge.style.borderColor = 'rgba(34, 197, 94, 0.4)';
                recorderBadge.style.color = '#4ade80';
            } else {
                recorderDisplay.textContent = recordedKeyDisplay;
                recorderDisplay.style.color = '#38bdf8';
                recorderSubtext.textContent = 'Click vào đây rồi nhấn phím hoặc click chuột';
                recorderIcon.textContent = '⌨️';
                recorderBadge.textContent = 'Đổi phím';
                recorderBadge.style.background = 'rgba(56, 189, 248, 0.15)';
                recorderBadge.style.borderColor = 'rgba(56, 189, 248, 0.35)';
                recorderBadge.style.color = '#7dd3fc';
            }
        }

        function preventDefaultAction(e) {
            if (isRecording) {
                e.preventDefault();
                e.stopPropagation();
            }
        }

        function onMouseDown(e) {
            if (!isRecording) return;

            // If clicked cancel or close buttons, abort recording
            if (e.target && e.target.closest('#gh-modal-close, #gh-modal-cancel')) {
                stopRecording(null, null);
                return;
            }

            e.preventDefault();
            e.stopPropagation();

            const buttonMap = {
                1: { val: 'middle', name: 'Chuột Giữa (Middle)' },
                2: { val: 'right', name: 'Chuột Phải' },
                3: { val: 'mouse4', name: 'Mouse 4 (Chuột hông)' },
                4: { val: 'mouse5', name: 'Mouse 5 (Chuột hông)' }
            };

            const btn = buttonMap[e.button];
            if (!btn) {
                // Left click alone: do not capture (needed for UI interaction)
                if (e.button === 0 && (e.altKey || e.ctrlKey || e.shiftKey)) {
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

            const modsVal = [];
            const modsDisplay = [];
            if (e.ctrlKey) { modsVal.push('ctrl'); modsDisplay.push('Ctrl'); }
            if (e.altKey) { modsVal.push('alt'); modsDisplay.push('Alt'); }
            if (e.shiftKey) { modsVal.push('shift'); modsDisplay.push('Shift'); }

            modsVal.push(btn.val);
            modsDisplay.push(btn.name);

            stopRecording(modsVal.join('+'), modsDisplay.join(' + '));
        }

        function onPointerDown(e) {
            if (!isRecording) return;
            if (e.button === 3 || e.button === 4 || e.button === 1 || e.button === 2) {
                onMouseDown(e);
            }
        }

        function onKeyDown(e) {
            if (!isRecording) return;
            e.preventDefault();
            e.stopPropagation();

            if (e.key === 'Escape') {
                stopRecording(null, null);
                return;
            }

            // Lone modifier preview
            if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) {
                const activeMods = [];
                if (e.ctrlKey) activeMods.push('Ctrl');
                if (e.altKey) activeMods.push('Alt');
                if (e.shiftKey) activeMods.push('Shift');
                recorderDisplay.textContent = activeMods.join(' + ') + ' + ...';
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

        function startRecording() {
            if (isRecording) return;
            isRecording = true;
            recorderBox.style.borderColor = '#38bdf8';
            recorderBox.style.borderStyle = 'solid';
            recorderBox.style.background = 'rgba(56, 189, 248, 0.12)';
            recorderBox.style.boxShadow = '0 0 20px rgba(56, 189, 248, 0.35)';
            recorderDisplay.textContent = 'Đang nghe phím...';
            recorderDisplay.style.color = '#e2e8f0';
            recorderSubtext.textContent = 'Nhấn phím bàn phím hoặc click nút chuột bất kỳ';
            recorderIcon.textContent = '🔴';
            recorderBadge.textContent = 'Đang nghe...';
            recorderBadge.style.background = 'rgba(239, 68, 68, 0.2)';
            recorderBadge.style.borderColor = 'rgba(239, 68, 68, 0.5)';
            recorderBadge.style.color = '#f87171';
            recorderBox.focus();

            window.addEventListener('keydown', onKeyDown, { capture: true });
            window.addEventListener('mousedown', onMouseDown, { capture: true });
            window.addEventListener('pointerdown', onPointerDown, { capture: true });
            window.addEventListener('mouseup', preventDefaultAction, { capture: true });
            window.addEventListener('contextmenu', preventDefaultAction, { capture: true });
        }

        recorderBox.addEventListener('click', () => {
            if (!isRecording) startRecording();
        });

        // Quick Preset buttons
        overlay.querySelectorAll('.gh-preset-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const key = btn.getAttribute('data-key');
                const label = btn.innerText.replace(/[🖱️⌨️]/g, '').trim();
                stopRecording(key, label);
            });
        });

        const closeModal = () => {
            stopRecording(null, null);
            overlay.remove();
        };

        closeBtn.addEventListener('click', closeModal);
        cancelBtn.addEventListener('click', closeModal);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });

        const submitKey = () => {
            const val = recordedKeyVal.trim().toLowerCase();
            if (!val) {
                errEl.textContent = 'Vui lòng nhấn một phím tắt để gán!';
                return;
            }
            saveBtn.disabled = true;
            saveBtn.textContent = 'Đang lưu...';
            errEl.textContent = '';

            fetch('http://127.0.0.1:8765/set_hotkey', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hotkey: val })
            })
            .then(r => r.json())
            .then(res => {
                if (res && res.ok) {
                    updateHotkeyDisplay(res.display_name);
                    setLog(`✅ Đã đổi phím tắt thành: [${res.display_name}]!`, '#22c55e');
                    playBeep();
                    closeModal();
                } else {
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Lưu phím tắt';
                    errEl.textContent = 'Không thể gán phím tắt này. Thử phím khác!';
                }
            })
            .catch(() => {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Lưu phím tắt';
                errEl.textContent = 'Lỗi kết nối tới Server (hãy chắc chắn tool đang chạy)!';
            });
        };

        saveBtn.addEventListener('click', submitKey);
    }

    function createUI() {
        const old = document.getElementById('genshin-helper-panel');
        if (old) old.remove();

        injectThemeStyles();

        // Clean Vector SVG Icons
        const SVG_PIN = `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>`;
        const SVG_UNPIN = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>`;
        const SVG_MIN = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"></line></svg>`;
        const SVG_EDIT = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"></path><path d="m15 5 4 4"></path></svg>`;

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
                    <span id="gh-pill-hotkey" class="gh-mono" title="Click để đổi phím tắt" style="font-size: 11px; color: #7dd3fc; background: rgba(56, 189, 248, 0.14); border: 1px solid rgba(56, 189, 248, 0.3); padding: 1px 7px; border-radius: 5px; font-weight: 600; cursor: pointer;">${currentHotkeyDisplay}</span>
                    <span title="Mở rộng widget" style="font-size: 11px; color: #38bdf8; margin-left: 2px;">▲</span>
                </div>

                <!-- Expanded Card -->
                <div id="gh-card" style="display: ${isSavedCollapsed ? 'none' : 'flex'}; flex-direction: column; gap: 9px; min-width: 320px; max-width: 360px; background: rgba(15, 23, 42, 0.94); backdrop-filter: blur(20px); border: 1px solid rgba(56, 189, 248, 0.35); border-radius: 14px; padding: 12px 16px; color: #fff; box-shadow: 0 16px 36px -8px rgba(0, 0, 0, 0.75), 0 0 0 1px rgba(255, 255, 255, 0.05); transition: box-shadow 0.2s ease;">
                    <!-- Draggable Header -->
                    <div id="gh-card-header" title="${isPinned ? 'Vị trí đã ghim cố định' : 'Click giữ để kéo thả • Click đúp để reset vị trí'}" style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 8px; cursor: ${isPinned ? 'default' : 'grab'};">
                        <div style="display: flex; align-items: center; gap: 7px;">
                            <span id="gh-status-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #16a34a; box-shadow: 0 0 8px #16a34a;"></span>
                            <strong style="color: #38bdf8; font-size: 13px; font-weight: 700; letter-spacing: -0.2px;">Genshin Helper</strong>
                            <span style="font-size: 10px; background: rgba(56,189,248,0.12); color: #7dd3fc; border: 1px solid rgba(56,189,248,0.25); border-radius: 4px; padding: 1px 5px; font-weight: 600; line-height: 1.2; display: inline-flex; align-items: center;">v3.2</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span id="gh-status" style="font-size: 10px; padding: 2px 7px; border-radius: 10px; background: #16a34a; font-weight: 600; line-height: 1.2; display: inline-flex; align-items: center;">Connected</span>
                            <button id="gh-pin-btn" class="gh-btn-icon ${isPinned ? 'gh-btn-pinned' : ''}" title="${isPinned ? 'Vị trí đã GHIM (Click để mở khóa kéo thả)' : 'Chưa ghim (Kéo giữ thanh tiêu đề để di chuyển, click để ghim)'}">
                                ${isPinned ? SVG_PIN : SVG_UNPIN}
                            </button>
                            <button id="gh-min-btn" class="gh-btn-icon" title="Thu nhỏ widget">${SVG_MIN}</button>
                        </div>
                    </div>

                    <!-- Coords & Hotkey Row -->
                    <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; gap: 8px;">
                        <div style="display: flex; flex-direction: column; gap: 2px; overflow: hidden;">
                            <span style="color: #64748b; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;">TỌA ĐỘ GAME</span>
                            <span id="gh-coords" style="color: #38bdf8; font-weight: 600; font-size: 12px; font-variant-numeric: tabular-nums lining-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">Sẵn sàng</span>
                        </div>
                        <div id="gh-hotkey-btn" title="Click để đổi phím tắt (Bàn phím hoặc Chuột)" style="display: flex; align-items: center; gap: 6px; background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.15s ease; flex-shrink: 0;">
                            <span id="gh-hotkey-text" class="gh-mono">${currentHotkeyDisplay}</span>
                            <span style="display: flex; align-items: center; opacity: 0.85;">${SVG_EDIT}</span>
                        </div>
                    </div>

                    <!-- Live Log Box -->
                    <div id="gh-log" style="font-size: 11.5px; background: rgba(2, 6, 23, 0.6); border: 1px solid rgba(51, 65, 85, 0.5); border-radius: 8px; padding: 7px 11px; min-height: 22px; color: #94a3b8; line-height: 1.45; word-break: break-word;">
                        Mở Map (M) trong game rồi kích hoạt phím tắt để tự đồng bộ.
                    </div>
                </div>
            </div>
        `;

        (document.body || document.documentElement).appendChild(panel);
        console.log('[Genshin Helper v3.1] UI Panel attached with Pro Font & Draggable support!');

        const container = document.getElementById('gh-container');
        const cardHeader = document.getElementById('gh-card-header');
        const pinBtn = document.getElementById('gh-pin-btn');
        const minBtn = document.getElementById('gh-min-btn');
        const pill = document.getElementById('gh-pill');
        const hotkeyBtn = document.getElementById('gh-hotkey-btn');
        const pillHotkey = document.getElementById('gh-pill-hotkey');

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
                if (e.target.closest('#gh-pill-hotkey')) return;
                toggleWidget(false);
            });
        }

        if (hotkeyBtn) {
            hotkeyBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                showHotkeyModal();
            });
        }

        if (pillHotkey) {
            pillHotkey.addEventListener('click', (e) => {
                e.stopPropagation();
                showHotkeyModal();
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
    // 3. Universal Layered Exploration Manager (Caves & High-Altitude Floors)
    // =========================================================================
    function findMatchingPointGroup(ctx, targetLat, targetLng) {
        if (!ctx || targetLat === null || targetLng === null) return null;
        const vm = ctx.vm;
        const list = (vm && Array.isArray(vm.pointGroupList) && vm.pointGroupList.length > 0)
            ? vm.pointGroupList
            : (vm && vm.$store && Array.isArray(vm.$store.state.pointGroupList) ? vm.$store.state.pointGroupList : []);

        if (!list || list.length === 0) return null;

        let bestInside = null;
        let minInsideArea = Infinity;
        let bestNearby = null;
        let minNearbyDist = Infinity;

        for (const g of list) {
            if (!g.floors || g.floors.length === 0) continue;
            let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity;
            let cLat = 0, cLng = 0, count = 0;

            for (const f of g.floors) {
                if (f.overlay) {
                    const ov = f.overlay;
                    const fMinLat = Math.min(ov.l_y, ov.r_y);
                    const fMaxLat = Math.max(ov.l_y, ov.r_y);
                    const fMinLng = Math.min(ov.l_x, ov.r_x);
                    const fMaxLng = Math.max(ov.l_x, ov.r_x);

                    minLat = Math.min(minLat, fMinLat);
                    maxLat = Math.max(maxLat, fMaxLat);
                    minLng = Math.min(minLng, fMinLng);
                    maxLng = Math.max(maxLng, fMaxLng);

                    cLat += (fMinLat + fMaxLat) / 2;
                    cLng += (fMinLng + fMaxLng) / 2;
                    count++;
                }
            }

            if (count > 0) {
                cLat /= count;
                cLng /= count;
                const isInside = (targetLat >= minLat && targetLat <= maxLat && targetLng >= minLng && targetLng <= maxLng);
                const area = (maxLat - minLat) * (maxLng - minLng);
                const dist = Math.hypot(cLat - targetLat, cLng - targetLng);

                if (isInside) {
                    if (area < minInsideArea) {
                        minInsideArea = area;
                        bestInside = g;
                    }
                } else if (dist < minNearbyDist && dist < 600) {
                    minNearbyDist = dist;
                    bestNearby = g;
                }
            }
        }

        return bestInside || bestNearby;
    }

    function handleLayeredExploration(ctx, data) {
        try {
            if (!ctx || !ctx.vm) return;
            const vm = ctx.vm;
            const isUnderground = !!(data && (data.is_underground || data.has_layers));
            const isCurrentlyInExploration = !!(vm.$store && vm.$store.state.layeredExplorationInfo);

            // Case 1: Player is on Surface (not underground/multilayer)
            if (!isUnderground) {
                if (isCurrentlyInExploration && typeof vm.onQuitExploreAPointGroup === 'function') {
                    setLog('🌤️ Đang quay về bản đồ bề mặt...', '#38bdf8');
                    vm.onQuitExploreAPointGroup();
                }
                return;
            }

            // Case 2: Player is Underground or in Multilayer Structure
            const hasWorld = (data.lat !== undefined && data.lng !== undefined);
            if (!hasWorld) return;

            const group = findMatchingPointGroup(ctx, data.lat, data.lng);
            if (!group) {
                console.log('[Genshin Helper] No matching pointGroup found for coords:', data.lat, data.lng);
                return;
            }

            // Determine target floor index
            let targetFloorIdx = 0;
            if (data.floor_name) {
                const clean = data.floor_name.toUpperCase().trim();
                if (clean.startsWith('B')) {
                    const num = parseInt(clean.replace(/\D/g, '')) || 1;
                    targetFloorIdx = Math.max(0, num - 1);
                } else if (clean.endsWith('F') || clean.startsWith('F')) {
                    const num = parseInt(clean.replace(/\D/g, '')) || 1;
                    targetFloorIdx = Math.max(0, num - 1);
                } else if (group.floors && group.floors.length > 0) {
                    const matchIdx = group.floors.findIndex(f => (f.floor_name || '').toUpperCase().includes(clean));
                    if (matchIdx !== -1) targetFloorIdx = matchIdx;
                }
            } else if (data.active_floor_idx !== undefined && data.surface_idx !== undefined) {
                if (data.active_floor_idx > data.surface_idx) {
                    targetFloorIdx = data.active_floor_idx - data.surface_idx - 1;
                } else {
                    targetFloorIdx = data.active_floor_idx;
                }
            }

            if (group.floors && group.floors.length > 0) {
                targetFloorIdx = Math.min(Math.max(0, targetFloorIdx), group.floors.length - 1);
            }

            const currentLayered = vm.$store ? vm.$store.state.layeredExplorationInfo : null;
            const caveTitle = (group.floors && group.floors[targetFloorIdx]) ? group.floors[targetFloorIdx].floor_name : (group.name || `Hang ${group.id}`);

            if (currentLayered && currentLayered.id === group.id) {
                // Already in this cave; check if we need to switch floor
                if (currentLayered.activeIndex !== targetFloorIdx && typeof vm.onSwitchExploreFloor === 'function' && group.floors[targetFloorIdx]) {
                    setLog(`🔄 Đang chuyển sang tầng [${caveTitle}]...`, '#38bdf8');
                    vm.onSwitchExploreFloor({ id: group.floors[targetFloorIdx].id, name: group.floors[targetFloorIdx].floor_name });
                }
            } else if (typeof vm.onEnterExploreAPointGroup === 'function') {
                setLog(`🗺️ Đang vào [Thám Hiểm Phân Tầng]: ${caveTitle}...`, '#38bdf8');
                vm.onEnterExploreAPointGroup(group, targetFloorIdx);
                // HoYoLAB fitBounds to the cave, so re-center to user's exact coordinates
                setTimeout(() => {
                    if (ctx.map && typeof ctx.map.panTo === 'function') {
                        ctx.map.panTo([data.lat, data.lng], { animate: true, duration: 0.5 });
                    }
                }, 180);
            }

            // Sync with physical elevator UI as secondary assurance
            setTimeout(() => {
                switchUndergroundFloor(data.active_floor_idx, data.total_floors, data.floor_name);
            }, 350);

        } catch (e) {
            console.error('[Genshin Helper] Error in handleLayeredExploration:', e);
        }
    }

    // =========================================================================
    // 3.1 Universal Underground Floor Switcher (Index 1:1 Matching)
    // =========================================================================
    function switchUndergroundFloor(floorIdx, totalFloors, floorName) {
        try {
            // Priority 1: Direct 1:1 Index Match via HoYoLAB's .underground-elevator
            const elevator = document.querySelector('.underground-elevator, [class*="underground-elevator"]');
            if (elevator) {
                const floors = Array.from(elevator.querySelectorAll('.underground-elevator__floor'));
                if (floors.length > 0) {
                    let target = null;
                    // Try exact floorIdx if valid
                    if (floorIdx !== undefined && floorIdx >= 0 && floorIdx < floors.length) {
                        target = floors[floorIdx];
                    }
                    // Try text match if floorName provided
                    if (!target && floorName) {
                        target = floors.find(f => (f.innerText || '').trim().toUpperCase() === floorName.toUpperCase());
                    }

                    if (target) {
                        const txt = (target.innerText || target.textContent || '').trim();
                        if (!target.classList.contains('underground-elevator__floor--active')) {
                            target.click();
                            setLog(`Đã chuyển tầng bản đồ web sang: [${txt || ('Tầng ' + floorIdx)}]!`, '#38bdf8');
                            return true;
                        } else {
                            setLog(`ℹ️ Bản đồ web đang ở đúng tầng: [${txt || ('Tầng ' + floorIdx)}]`, '#38bdf8');
                            return true;
                        }
                    }
                }
            }

            // Priority 2: DOM Search by exact floorName without bugs
            if (floorName) {
                const candidates = Array.from(document.querySelectorAll('button, div, span, a'));
                const target = candidates.find(el => {
                    if (el.offsetParent === null) return false;
                    const txt = (el.innerText || el.textContent || '').trim().toUpperCase();
                    return txt === floorName.toUpperCase();
                });
                if (target) {
                    target.click();
                    setLog(`Đã chuyển tầng bản đồ web sang: [${floorName}]!`, '#38bdf8');
                    return true;
                }
            }
        } catch (e) {
            console.error('[Genshin Helper] Error in switchUndergroundFloor:', e);
        }
        return false;
    }

    // =========================================================================
    // 4. Server Polling & Live State Sync
    // =========================================================================
    function pollServer() {
        fetch(API_POLL_URL, { cache: 'no-store' })
            .then(res => res.json())
            .then(data => {
                setStatus(true);
                if (data && data.type) {
                    if (data.type === 'EVAL' && data.code) {
                        try { eval(data.code); } catch(err) { console.error('Eval error:', err); }
                    } else if (data.type !== 'HEARTBEAT') {
                        handleServerMessage(data);
                    }
                }
                setTimeout(pollServer, 100);
            })
            .catch(() => {
                setStatus(false);
                setTimeout(pollServer, 1500);
            });
    }

    function reportState() {
        try {
            const ctx = findMapContext();
            if (!ctx.map) return;
            const center = ctx.map.getCenter();
            const zoom = ctx.map.getZoom();
            const markers = getAllMarkers(ctx);
            fetch(API_REPORT_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    center: [center.lat, center.lng],
                    zoom: zoom,
                    markersCount: markers.length
                })
            }).catch(() => {});
        } catch (e) {}
    }

    setInterval(reportState, 5000);

    function fetchInitialStatus() {
        fetch('http://127.0.0.1:8765/status', { cache: 'no-store' })
            .then(res => res.json())
            .then(data => {
                if (data && data.display_name) {
                    updateHotkeyDisplay(data.display_name);
                }
            })
            .catch(() => {});
    }

    function handleServerMessage(data) {
        if (data.type === 'HOTKEY_UPDATED') {
            if (data.display_name) {
                updateHotkeyDisplay(data.display_name);
                setLog(`⚙️ Phím tắt đã đổi thành: [${data.display_name}]`, '#38bdf8');
            }
            return;
        }

        if (data.type === 'SYNC_AND_MARK') {
            const coordsEl = document.getElementById('gh-coords');
            if (coordsEl) {
                if (data.lat !== undefined && data.lng !== undefined) {
                    const mapTag = data.map_name ? `[${data.map_name}] ` : (data.map_id ? `[Map ${data.map_id}] ` : '');
                    coordsEl.textContent = `${mapTag}(${data.lat.toFixed(0)}, ${data.lng.toFixed(0)})`;
                } else if (data.pixel_x) {
                    coordsEl.textContent = `Pixel: (${data.pixel_x.toFixed(0)}, ${data.pixel_y.toFixed(0)})`;
                }
            }
            performAutoSyncAndMark(data);
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

    function performAutoSyncAndMark(data) {
        try {
            const targetMapId = data.map_id ? parseInt(data.map_id) : 2;
            const currentMapId = getCurrentWebMapId();

            if (data.has_world_coords && targetMapId !== currentMapId) {
                const mapName = data.map_name || `Map ${targetMapId}`;
                setLog(`🔄 Tự động chuyển sang bản đồ [${mapName}]...`, '#38bdf8');
                playBeep();

                // Switch route via Vue Router or Hash
                try {
                    const vueApp = document.querySelector('#app')?.__vue__;
                    if (vueApp && vueApp.$router) {
                        vueApp.$router.push(`/map/${targetMapId}`);
                    } else if (window.location.hash.includes('#/map/')) {
                        window.location.hash = window.location.hash.replace(/#\/map\/\d+/, `#/map/${targetMapId}`);
                    } else {
                        window.location.hash = `#/map/${targetMapId}`;
                    }
                    window.dispatchEvent(new HashChangeEvent('hashchange'));
                } catch (e) {
                    window.location.hash = `#/map/${targetMapId}`;
                }

                waitForNewMapReady(4000).then((newCtx) => {
                    setLog(`✅ Đã mở bản đồ [${mapName}]! Đang trượt tới vị trí...`, '#22c55e');
                    executeSyncAndMarkOnContext(newCtx, data);
                });
                return;
            }

            const ctx = findMapContext();
            executeSyncAndMarkOnContext(ctx, data);
        } catch (e) {
            console.error('[Genshin Helper] Error in performAutoSyncAndMark:', e);
        }
    }

    function executeSyncAndMarkOnContext(ctx, data) {
        try {
            if (!ctx || !ctx.map) {
                setLog('❌ Chưa tìm thấy Leaflet Map! Hãy chắc chắn map đã tải xong.', '#ef4444');
                return;
            }

            // Step 1: Check if game sent real World Coordinates (lat, lng)
            const hasWorld = (data && data.has_world_coords && data.lat !== undefined && data.lng !== undefined);
            const targetLat = hasWorld ? data.lat : null;
            const targetLng = hasWorld ? data.lng : null;

            if (hasWorld) {
                const mapName = data.map_name ? ` (${data.map_name})` : '';
                setLog(`Đang trượt bản đồ web tới vị trí game${mapName}: [${targetLat.toFixed(1)}, ${targetLng.toFixed(1)}]...`, '#38bdf8');
                // Pan / setView directly to the player's in-game position
                if (autoPanEnabled) {
                    const currentZoom = ctx.map.getZoom();
                    const targetZoom = (currentZoom < 0.8 || currentZoom > 2.5) ? 1.2 : currentZoom;
                    if (Math.abs(currentZoom - targetZoom) > 0.3) {
                        ctx.map.setView([targetLat, targetLng], targetZoom, { animate: true, duration: 0.6 });
                    } else {
                        ctx.map.panTo([targetLat, targetLng], { animate: true, duration: 0.6 });
                    }
                }

                // Handle automatic cave & multi-floor exploration (or returning to surface)
                if (autoSwitchLayerEnabled) {
                    handleLayeredExploration(ctx, data);
                }
            } else {
                if (autoSwitchLayerEnabled) {
                    handleLayeredExploration(ctx, data);
                }
            }

            // Step 2: Wait for map animation to settle before querying markers
            const waitTime = hasWorld ? 650 : 50;
            setTimeout(() => {
                const markers = getAllMarkers(ctx);
                setLog(`Đã quét thấy ${markers.length} điểm đánh dấu trên vùng bản đồ.`, '#38bdf8');

                if (markers.length === 0) {
                    setLog('❌ Chưa thấy điểm nào! Hãy chọn bật loại Rương hoặc Thần Đồng ở menu!', '#ef4444');
                    return;
                }

                // Find closest marker to target coordinate or screen center
                let closest = null;
                let minDistance = Infinity;

                if (hasWorld) {
                    // Distance in World LatLng
                    for (const m of markers) {
                        if (m.latlng) {
                            const d = Math.hypot(m.latlng.lat - targetLat, m.latlng.lng - targetLng);
                            if (d < minDistance) {
                                minDistance = d;
                                closest = m;
                            }
                        }
                    }

                    // Proximity check: Only open popup if player is close (< 60m)
                    if (minDistance > 60) {
                        closeOpenPopup();
                        setLog(`✅ Đã căn vị trí & tầng thám hiểm! (Không có điểm gần < 60m)`, '#22c55e');
                        return;
                    }

                    setLog(`Điểm gần nhất cách người chơi ${minDistance.toFixed(1)}m. Đang mở...`, '#10b981');
                } else {
                    // Screen center fallback
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
                const checkInterval = setInterval(() => {
                    attempts++;
                    // If underground exploration button is inside popup, click it
                    if (autoSwitchLayerEnabled && (data.is_underground || data.has_layers)) {
                        const layerBtn = findLayerButton();
                        if (layerBtn) {
                            setLog('Đang mở [Thám Hiểm Phân Tầng]...', '#38bdf8');
                            layerBtn.click();
                            setTimeout(() => {
                                const floorIdx = (data.active_floor_idx !== undefined) ? data.active_floor_idx : data.floor_idx;
                                switchUndergroundFloor(floorIdx, data.total_floors, data.floor_name);
                            }, 250);
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

                    if (attempts >= 15) {
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
        return document.querySelector('.leaflet-popup, .leaflet-popup-pane, [class*="popup"], [class*="marker-pop"], .popup-ys, .el-dialog');
    }

    function closeOpenPopup() {
        try {
            const popup = findPopupContainer();
            if (popup) {
                const candidates = Array.from(popup.querySelectorAll('*'));
                const xBtn = candidates.find(el => {
                    const t = getText(el);
                    const c = el.className || '';
                    return t === '✕' || t === '×' || (typeof c === 'string' && c.includes('close'));
                });
                if (xBtn && typeof xBtn.click === 'function') xBtn.click();
            }
            const all = document.querySelectorAll('.leaflet-popup-close-button, [class*="close"], [class*="cancel"]');
            all.forEach(el => {
                const t = (el.innerText || el.textContent || '').trim();
                const c = String(el.className || '');
                if (t === '✕' || t === '×' || c.includes('close')) {
                    if (el.offsetWidth > 0 && el.offsetHeight > 0 && typeof el.click === 'function') {
                        el.click();
                    }
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
            const popup = findPopupContainer();
            if (!popup || popup.offsetParent === null) return null;
            const candidates = Array.from(popup.querySelectorAll('button, div, span, a'));
            return candidates.find(el => {
                if (el.offsetParent === null) return false;
                const txt = getText(el);
                return txt === 'Ghim' || txt === 'Mark';
            });
        } catch (e) { return null; }
    }

    function findLayerButton() {
        try {
            const popup = findPopupContainer();
            if (!popup) return null;
            const candidates = Array.from(popup.querySelectorAll('button, div, span, a'));
            return candidates.find(el => {
                if (el.offsetParent === null) return false;
                const txt = getText(el);
                return txt.includes('Thám Hiểm Phân Tầng') || txt.includes('Phân Tầng') || txt.includes('Underground');
            });
        } catch (e) { return null; }
    }

    function findAlreadyMarkedButton() {
        try {
            const popup = findPopupContainer();
            if (!popup || popup.offsetParent === null) return null;
            const candidates = Array.from(popup.querySelectorAll('button, div, span, a'));
            return candidates.find(el => {
                if (el.offsetParent === null) return false;
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

})();
