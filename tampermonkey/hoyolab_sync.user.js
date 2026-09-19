// ==UserScript==
// @name         HoYoLAB Genshin Helper (Auto Sync)
// @namespace    https://github.com/genshin-helper
// @version      3.1.0
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

    function showHotkeyModal() {
        const existing = document.getElementById('gh-hotkey-modal');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = 'gh-hotkey-modal';
        overlay.style.cssText = `
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.7); backdrop-filter: blur(8px);
            z-index: 2147483647; display: flex; align-items: center; justify-content: center;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
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

                <!-- Custom Input -->
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    <label style="font-size: 11px; color: #cbd5e1; font-weight: 600;">Hoặc tự nhập mã phím bất kỳ:</label>
                    <input id="gh-modal-input" type="text" placeholder="Ví dụ: mouse4, mouse5, middle, alt+f11, ctrl+q..." style="background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 8px; padding: 8px 12px; color: #fff; font-size: 13px; font-family: monospace; outline: none;" />
                    <span id="gh-modal-error" style="color: #ef4444; font-size: 11px; min-height: 14px;"></span>
                </div>

                <!-- Actions -->
                <div style="display: flex; justify-content: flex-end; gap: 8px; border-top: 1px solid rgba(51, 65, 85, 0.6); padding-top: 12px;">
                    <button id="gh-modal-cancel" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); color: #94a3b8; border-radius: 8px; padding: 7px 16px; cursor: pointer; font-size: 12px;">Hủy</button>
                    <button id="gh-modal-save" style="background: #0284c7; border: none; color: #fff; font-weight: 600; border-radius: 8px; padding: 7px 18px; cursor: pointer; font-size: 12px; box-shadow: 0 2px 10px rgba(2,132,199,0.4); transition: all 0.15s;">Lưu phím tắt</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const input = document.getElementById('gh-modal-input');
        const closeBtn = document.getElementById('gh-modal-close');
        const cancelBtn = document.getElementById('gh-modal-cancel');
        const saveBtn = document.getElementById('gh-modal-save');
        const errEl = document.getElementById('gh-modal-error');

        input.value = currentHotkeyDisplay || 'mouse4';
        input.focus();
        input.select();

        const closeModal = () => overlay.remove();
        closeBtn.addEventListener('click', closeModal);
        cancelBtn.addEventListener('click', closeModal);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });

        overlay.querySelectorAll('.gh-preset-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                input.value = btn.getAttribute('data-key');
                input.focus();
            });
        });

        const submitKey = () => {
            const val = input.value.trim().toLowerCase();
            if (!val) {
                errEl.textContent = 'Vui lòng nhập hoặc chọn một phím tắt!';
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
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') submitKey();
            if (e.key === 'Escape') closeModal();
        });
    }

    function createUI() {
        const old = document.getElementById('genshin-helper-panel');
        if (old) old.remove();

        let isSavedCollapsed = false;
        try { isSavedCollapsed = (localStorage.getItem('gh_widget_collapsed') === '1'); } catch(e) {}

        const panel = document.createElement('div');
        panel.id = 'genshin-helper-panel';
        panel.innerHTML = `
            <div style="position: fixed; bottom: 20px; right: 20px; z-index: 2147483647; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; user-select: none;">
                <!-- Collapsed Floating Pill -->
                <div id="gh-pill" style="display: ${isSavedCollapsed ? 'flex' : 'none'}; align-items: center; gap: 8px; background: rgba(15, 23, 42, 0.90); backdrop-filter: blur(14px); border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 24px; padding: 7px 14px; color: #fff; cursor: pointer; box-shadow: 0 4px 20px rgba(0,0,0,0.5); transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);">
                    <span id="gh-pill-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #16a34a; box-shadow: 0 0 8px #16a34a;"></span>
                    <strong style="color: #38bdf8; font-size: 12px; font-weight: 600; letter-spacing: 0.2px;">Genshin Sync</strong>
                    <span id="gh-pill-hotkey" title="Click để đổi phím tắt" style="font-size: 11px; color: #38bdf8; font-family: monospace; background: rgba(255,255,255,0.08); padding: 1px 6px; border-radius: 4px;">${currentHotkeyDisplay}</span>
                    <span title="Mở rộng widget" style="font-size: 11px; color: #38bdf8; margin-left: 2px;">▲</span>
                </div>

                <!-- Expanded Card -->
                <div id="gh-card" style="display: ${isSavedCollapsed ? 'none' : 'flex'}; flex-direction: column; gap: 8px; min-width: 320px; max-width: 360px; background: rgba(15, 23, 42, 0.94); backdrop-filter: blur(16px); border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 12px; padding: 12px 16px; color: #fff; box-shadow: 0 10px 30px rgba(0,0,0,0.7); transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);">
                    <!-- Header -->
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 7px;">
                        <div style="display: flex; align-items: center; gap: 7px;">
                            <span id="gh-status-dot" style="width: 8px; height: 8px; border-radius: 50%; background: #16a34a; box-shadow: 0 0 8px #16a34a;"></span>
                            <strong style="color: #38bdf8; font-size: 13px; font-weight: 600;">Genshin Helper v3.0</strong>
                        </div>
                        <div style="display: flex; align-items: center; gap: 6px;">
                            <span id="gh-status" style="font-size: 10px; padding: 2px 7px; border-radius: 10px; background: #16a34a; font-weight: 600;">Connected</span>
                            <button id="gh-min-btn" title="Thu nhỏ / Ẩn widget" style="background: rgba(255,255,255,0.08); border: none; color: #94a3b8; width: 22px; height: 22px; border-radius: 6px; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 13px; line-height: 1; transition: all 0.15s ease;">−</button>
                        </div>
                    </div>

                    <!-- Coords & Hotkey Row -->
                    <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px;">
                        <div style="display: flex; align-items: center; gap: 4px;">
                            <span style="color: #94a3b8;">Tọa độ game:</span>
                            <span id="gh-coords" style="color: #38bdf8; font-family: monospace; font-weight: 600; font-size: 12px;">Sẵn sàng</span>
                        </div>
                        <div id="gh-hotkey-btn" title="Click để đổi phím tắt (Bàn phím hoặc Chuột)" style="display: flex; align-items: center; gap: 5px; background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 5px; padding: 2px 7px; font-size: 10px; font-family: monospace; font-weight: bold; cursor: pointer; transition: all 0.15s ease;">
                            <span id="gh-hotkey-text">${currentHotkeyDisplay}</span>
                            <span style="font-size: 9px; opacity: 0.8;">✏️</span>
                        </div>
                    </div>

                    <!-- Live Log Box -->
                    <div id="gh-log" style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 11px; background: rgba(0,0,0,0.45); border: 1px solid rgba(51, 65, 85, 0.6); border-radius: 6px; padding: 6px 10px; min-height: 20px; color: #94a3b8; line-height: 1.4; word-break: break-word;">
                        Mở Map (M) trong game rồi kích hoạt phím tắt để tự đồng bộ.
                    </div>

                    <!-- Compact Toggles -->
                    <div style="display: flex; justify-content: space-between; align-items: center; font-size: 11px; padding-top: 1px; color: #cbd5e1;">
                        <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                            <input type="checkbox" id="gh-autopan" checked style="cursor: pointer; accent-color: #0284c7;" /> Tự kéo map
                        </label>
                        <label style="display: flex; align-items: center; gap: 5px; cursor: pointer; color: #a5f3fc;">
                            <input type="checkbox" id="gh-autolayer" checked style="cursor: pointer; accent-color: #0284c7;" /> Mở tầng ngầm
                        </label>
                    </div>
                </div>
            </div>
        `;

        (document.body || document.documentElement).appendChild(panel);
        console.log('[Genshin Helper v3.0] UI Panel attached to DOM!');

        // Minimize / Expand interactions
        const minBtn = document.getElementById('gh-min-btn');
        const pill = document.getElementById('gh-pill');
        const hotkeyBtn = document.getElementById('gh-hotkey-btn');
        const pillHotkey = document.getElementById('gh-pill-hotkey');

        if (minBtn) {
            minBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleWidget(true);
            });
            minBtn.addEventListener('mouseenter', () => {
                minBtn.style.background = 'rgba(255,255,255,0.2)';
                minBtn.style.color = '#fff';
            });
            minBtn.addEventListener('mouseleave', () => {
                minBtn.style.background = 'rgba(255,255,255,0.08)';
                minBtn.style.color = '#94a3b8';
            });
        }

        if (pill) {
            pill.addEventListener('click', () => {
                toggleWidget(false);
            });
            pill.addEventListener('mouseenter', () => {
                pill.style.borderColor = '#38bdf8';
                pill.style.boxShadow = '0 6px 24px rgba(56,189,248,0.25)';
            });
            pill.addEventListener('mouseleave', () => {
                pill.style.borderColor = 'rgba(56, 189, 248, 0.4)';
                pill.style.boxShadow = '0 4px 20px rgba(0,0,0,0.5)';
            });
        }

        if (hotkeyBtn) {
            hotkeyBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                showHotkeyModal();
            });
            hotkeyBtn.addEventListener('mouseenter', () => {
                hotkeyBtn.style.background = 'rgba(56, 189, 248, 0.3)';
                hotkeyBtn.style.borderColor = '#38bdf8';
                hotkeyBtn.style.boxShadow = '0 0 10px rgba(56, 189, 248, 0.4)';
            });
            hotkeyBtn.addEventListener('mouseleave', () => {
                hotkeyBtn.style.background = 'rgba(56, 189, 248, 0.15)';
                hotkeyBtn.style.borderColor = 'rgba(56, 189, 248, 0.4)';
                hotkeyBtn.style.boxShadow = 'none';
            });
        }

        if (pillHotkey) {
            pillHotkey.addEventListener('click', (e) => {
                e.stopPropagation();
                showHotkeyModal();
            });
        }

        document.getElementById('gh-autopan').addEventListener('change', (e) => {
            autoPanEnabled = e.target.checked;
        });

        document.getElementById('gh-autolayer').addEventListener('change', (e) => {
            autoSwitchLayerEnabled = e.target.checked;
        });
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
