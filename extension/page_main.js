(function () {
    'use strict';

    window.__gh_runtime_mode = 'extension';

    if (!window.location.hostname.includes('hoyolab.com') || !window.location.pathname.includes('/interactive-map/')) {
        return;
    }

    const SCRIPT_VERSION = 'v5.5.0';
    const SCRIPT_BUILD = '5.5.0-r1';

    function bootGenshinHelper() {
    if (window.__gh_booted_build === SCRIPT_BUILD) return;
    if (typeof window.__gh_teardown === 'function') {
        try { window.__gh_teardown(); } catch (e) {}
    }
    _cleanupOrphanPlayerMarkers(null);
    window.__gh_booted_version = SCRIPT_BUILD;
    window.__gh_booted_build = SCRIPT_BUILD;

    const pendingExtRequests = new Map();
    let extReqCounter = 0;
    let initialStatusSynced = false;
    let warnedVersionMismatch = false;

    function extRequest(path, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const body = options.body !== undefined ? options.body : undefined;
        const defaultTimeout = path.startsWith('/trigger_diagnostic') ? 25000 : 10000;
        const timeoutMs = typeof options.timeoutMs === 'number' ? options.timeoutMs : defaultTimeout;
        const reqId = `gh_ext_${Date.now()}_${++extReqCounter}`;

        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                pendingExtRequests.delete(reqId);
                reject(new Error(`Extension bridge timeout (${path})`));
            }, timeoutMs);

            pendingExtRequests.set(reqId, { resolve, reject, timer });

            window.postMessage({
                __gh_ext: true,
                dir: 'to-bg',
                kind: 'API_REQUEST',
                reqId,
                path,
                method,
                body
            }, window.location.origin);
        });
    }

    const apiRequest = extRequest;

    let autoPanEnabled = true;
    let autoSwitchLayerEnabled = true;
    let autoSyncMapMEnabled = true;
    let singleMonitorMode = false;
    let compactUiEnabled = false;
    let monitorCount = 0;
    let pendingVisibleRefresh = false;
    let lastSyncedView = null;
    let isConnected = false;
    let engineActive = true;
    let miniToastTimer = null;
    let currentHotkeySyncDisplay = 'Alt+F11';
    let currentHotkeyDebugDisplay = 'Alt+F12';
    let currentHotkeySwitchDisplay = 'Alt + Q';
    let currentHotkeySyncRaw = 'alt+f11';
    let currentHotkeyDebugRaw = 'alt+f12';
    let currentHotkeySwitchRaw = 'alt+q';
    let playerMarkerLayer = null;
    let playerMarkerMapRef = null;
    let playerMarkerWrapEl = null;
    let playerDomMarkerEl = null;
    let playerDomWrapEl = null;
    let playerDomMapRef = null;
    let playerLastState = null;
    let playerUnwrappedDir = null;
    let playerUnwrappedRot = null;
    let dragBoundContainerEl = null;
    let dragListenersCleanup = null;
    let layerTimers = [];
    let lastNoteModalFolder = '';
    let lastNoteModalTs = 0;
    let lastEventSeq = -1;
    let lastNearbyPushTs = 0;
    let lastNearbyPushLat = null;
    let lastNearbyPushLng = null;
    let lastNearbyPushMapId = null;
    let lastNearbyPushFloorId = null;
    let activeTspRoute = null;
    let tspSvgEl = null;
    let tspMapRef = null;
    let tspRedrawRaf = null;
    let cachedMarkerIndexSrc = null;
    let cachedMarkerIndexLen = -1;
    let cachedMarkerByMarker = null;
    let cachedMarkerById = null;
    let cachedFloorGroupListSrc = null;
    let cachedFloorGroupListLen = -1;
    let cachedFloorLabelMap = null;
    let exportInProgress = false;
    const cachedMarkedIdsByMap = new Map();
    const cachedMarkedFetchedAt = new Map();
    const markedFetchInFlight = new Map();
    // HoYoLAB label (category) names per map+lang: point items carry only label_id, so the
    // in-game Mini-HUD would otherwise show a generic "Map Pin" for every target.
    const labelNamesByKey = new Map();
    const labelParentByKey = new Map();
    const labelFetchInFlight = new Map();
    const hookedMarkStores = new WeakSet();
    const hookedPopupMaps = new WeakSet();
    let navMarkChain = Promise.resolve();
    // Nearby feed geometry: the controller needs the searched radius to tell "out of window"
    // from "collected on web" (prevents the in-game arrow from flipping between pins).
    const NEARBY_QUERY_RADIUS = 260;
    const NEARBY_QUERY_LIMIT = 25;
    try {
        const savedSync = localStorage.getItem('gh_hotkey_sync_display') || localStorage.getItem('gh_hotkey_display');
        if (savedSync) currentHotkeySyncDisplay = savedSync;
        const savedDebug = localStorage.getItem('gh_hotkey_debug_display');
        if (savedDebug) currentHotkeyDebugDisplay = savedDebug;
        const savedSwitch = localStorage.getItem('gh_hotkey_switch_display');
        if (savedSwitch) currentHotkeySwitchDisplay = savedSwitch;
        const savedSyncRaw = localStorage.getItem('gh_hotkey_sync_raw');
        if (savedSyncRaw) currentHotkeySyncRaw = savedSyncRaw;
        const savedDebugRaw = localStorage.getItem('gh_hotkey_debug_raw');
        if (savedDebugRaw) currentHotkeyDebugRaw = savedDebugRaw;
        const savedSwitchRaw = localStorage.getItem('gh_hotkey_switch_raw');
        if (savedSwitchRaw) currentHotkeySwitchRaw = savedSwitchRaw;
        const savedAutoM = localStorage.getItem('gh_auto_sync_m');
        if (savedAutoM === '0' || savedAutoM === 'false') autoSyncMapMEnabled = false;
        const savedSingleMon = localStorage.getItem('gh_single_monitor_mode');
        if (savedSingleMon === '1' || savedSingleMon === 'true') singleMonitorMode = true;
        const savedCompact = localStorage.getItem('gh_compact_ui');
        if (savedCompact === '1' || savedCompact === 'true') compactUiEnabled = true;
    } catch(e) {}

    function tr(key, params) {
        if (window.GHI18n && typeof window.GHI18n.t === 'function') {
            return window.GHI18n.t(key, params);
        }
        return key;
    }

    console.log('%c[Genshin Helper v5.5.0] Loaded (In-Place Dual Marker + Drag Auto-Unpin + Safe Compact UI + i18n)', 'color: #38bdf8; font-size: 16px; font-weight: bold;');

    function setAutoSyncUI(enabled) {
        autoSyncMapMEnabled = !!enabled;
        try { localStorage.setItem('gh_auto_sync_m', autoSyncMapMEnabled ? '1' : '0'); } catch(e) {}
    }

    function setSingleMonitorUI(enabled, monCount = undefined) {
        singleMonitorMode = !!enabled;
        if (typeof monCount === 'number' && !isNaN(monCount)) {
            monitorCount = monCount;
        }
        try { localStorage.setItem('gh_single_monitor_mode', singleMonitorMode ? '1' : '0'); } catch(e) {}
    }

    let compactTagTimer = null;

    function isProtectedSpaNode(el) {
        if (!el || el.nodeType !== 1) return true;
        if (el.matches && el.matches('.gt-modal, .gt-popup-root, .map-loading-mask, .leaflet-container, #gh-mini-toast, #gh-recenter-btn, #gh-diag-modal-overlay')) {
            return true;
        }
        if (el.closest && el.closest('.gt-modal, .gt-popup-root, .map-loading-mask')) {
            return true;
        }
        if (el.querySelector && el.querySelector('.leaflet-container')) {
            return true;
        }
        return false;
    }

    function clearCompactDomTags() {
        try {
            document.querySelectorAll('.gh-compact-sidebar').forEach((el) => el.classList.remove('gh-compact-sidebar'));
            document.querySelectorAll('.gh-compact-hide').forEach((el) => el.classList.remove('gh-compact-hide'));
        } catch (e) {}
    }

    function applyCompactDomTags() {
        clearCompactDomTags();
        if (!compactUiEnabled || !document.body) return;
        try {
            const vw = window.innerWidth || 1280;
            const vh = window.innerHeight || 720;

            // 1. Identify left filter sidebar container regardless of HoYoLAB class hashes
            const explicitSidebars = document.querySelectorAll(
                '.filter-panel, [class*="filter-panel"], [class*="map-filter"], [class*="sidebar"]'
            );
            let taggedSidebar = false;
            for (const el of explicitSidebars) {
                if (isProtectedSpaNode(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width >= 200 && r.width <= 460 && r.height >= vh * 0.45 && r.left <= 80) {
                    el.classList.add('gh-compact-sidebar');
                    taggedSidebar = true;
                    break;
                }
            }
            if (!taggedSidebar) {
                const candidates = document.querySelectorAll('aside, section, div');
                for (const el of candidates) {
                    if (isProtectedSpaNode(el)) continue;
                    const style = window.getComputedStyle(el);
                    if (style.position !== 'fixed' && style.position !== 'absolute') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width >= 240 && r.width <= 440 && r.height >= vh * 0.55 && r.left >= -20 && r.left <= 60 && r.top <= 120) {
                        const txt = (el.innerText || '').slice(0, 600);
                        if (/Điểm Dịch Chuyển|Thần Đồng|Rương|Thám Hiểm|Bộ lọc|Teleport|Oculi|Chest|Pins|Filters|Markers/i.test(txt)) {
                            el.classList.add('gh-compact-sidebar');
                            break;
                        }
                    }
                }
            }

            // 2. Hide bottom-right HoYoLAB App QR promo box & floating download cards
            const allBoxes = document.querySelectorAll('div, section, a');
            for (const el of allBoxes) {
                if (isProtectedSpaNode(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width >= 60 && r.width <= 260 && r.height >= 60 && r.height <= 260 && r.right >= vw - 48 && r.bottom >= vh - 48) {
                    const txt = (el.innerText || '').trim();
                    if (/HoYoLAB\s*App|Quét mã|Scan\s*QR|Download/i.test(txt)) {
                        el.classList.add('gh-compact-hide');
                    }
                }
            }
        } catch (e) {}
    }

    function setCompactUI(enabled, opts = {}) {
        const next = Boolean(enabled);
        const changed = compactUiEnabled !== next;
        compactUiEnabled = next;
        try { localStorage.setItem('gh_compact_ui', compactUiEnabled ? '1' : '0'); } catch(e) {}
        injectThemeStyles();
        if (document.body) {
            document.body.classList.toggle('gh-compact-ui', compactUiEnabled);
        }
        if (compactTagTimer) {
            clearTimeout(compactTagTimer);
            compactTagTimer = null;
        }
        if (compactUiEnabled) {
            applyCompactDomTags();
            compactTagTimer = setTimeout(applyCompactDomTags, 600);
        } else {
            clearCompactDomTags();
        }
        if (opts.syncStorage !== false) {
            try {
                window.postMessage({
                    __gh_ext: true,
                    dir: 'to-isolated',
                    kind: 'COMPACT_UI_SET',
                    enabled: compactUiEnabled
                }, window.location.origin);
            } catch (e) {}
        }
        if (changed && !opts.silent) {
            setLog(tr(compactUiEnabled ? 'page.compact.enabled' : 'page.compact.disabled'), '#a855f7');
        }
        return compactUiEnabled;
    }

    function updateRecenterButtonUI() {
        const existing = document.getElementById('gh-recenter-btn');
        if (autoPanEnabled) {
            if (existing) existing.classList.remove('gh-recenter-visible');
            return;
        }
        injectThemeStyles();
        let btn = existing;
        if (!btn) {
            btn = document.createElement('button');
            btn.id = 'gh-recenter-btn';
            btn.type = 'button';
            btn.className = 'gh-root';
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                recenterToPlayer();
            });
            (document.body || document.documentElement).appendChild(btn);
        }
        btn.textContent = tr('page.unpin.recenter_btn');
        btn.title = tr('page.unpin.recenter_title');
        btn.classList.add('gh-recenter-visible');
    }

    function setAutoPanPinned(pinned, opts = {}) {
        const next = Boolean(pinned);
        const changed = autoPanEnabled !== next;
        autoPanEnabled = next;
        updateRecenterButtonUI();
        if (changed && !next && !opts.silent) {
            showMiniToast(tr('page.unpin.toast'), '#fbbf24');
        } else if (changed && next && opts.notifyResumed) {
            showMiniToast(tr('page.unpin.resumed_toast'), '#38bdf8');
        }
        return autoPanEnabled;
    }

    function recenterToPlayer() {
        setAutoPanPinned(true, { notifyResumed: true });
        try {
            const ctx = findMapContext();
            if (ctx && ctx.map && playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                const allowAnim = !document.hidden && !singleMonitorMode;
                ctx.map.panTo([playerLastState.lat, playerLastState.lng], {
                    animate: allowAnim,
                    duration: allowAnim ? 0.28 : 0
                });
            }
        } catch (e) {}
    }

    function bindDragUnpinListeners(ctx) {
        try {
            const container = (ctx && ctx.map && typeof ctx.map.getContainer === 'function')
                ? ctx.map.getContainer()
                : document.querySelector('.leaflet-container');
            if (!container || container === dragBoundContainerEl) return;

            if (typeof dragListenersCleanup === 'function') {
                dragListenersCleanup();
                dragListenersCleanup = null;
            }

            let pointerActive = false;
            let dragStartTs = 0;
            let dragStartX = 0;
            let dragStartY = 0;

            const readCoords = (evt) => {
                if (evt.touches && evt.touches.length > 0) {
                    return { x: evt.touches[0].clientX, y: evt.touches[0].clientY };
                }
                return { x: evt.clientX, y: evt.clientY };
            };

            const onPointerDown = (evt) => {
                if (evt.type === 'mousedown' && evt.button !== 0) return;
                const pos = readCoords(evt);
                pointerActive = true;
                dragStartTs = Date.now();
                dragStartX = pos.x;
                dragStartY = pos.y;
            };

            const onPointerMove = (evt) => {
                if (!pointerActive || !autoPanEnabled) return;
                const pos = readCoords(evt);
                const heldMs = Date.now() - dragStartTs;
                const distPx = Math.hypot(pos.x - dragStartX, pos.y - dragStartY);
                if (heldMs > 100 && distPx > 20) {
                    pointerActive = false;
                    setAutoPanPinned(false);
                }
            };

            const onPointerEnd = () => {
                pointerActive = false;
            };

            container.addEventListener('mousedown', onPointerDown, { passive: true });
            container.addEventListener('mousemove', onPointerMove, { passive: true });
            container.addEventListener('touchstart', onPointerDown, { passive: true });
            container.addEventListener('touchmove', onPointerMove, { passive: true });
            window.addEventListener('mouseup', onPointerEnd, { passive: true });
            window.addEventListener('touchend', onPointerEnd, { passive: true });
            window.addEventListener('touchcancel', onPointerEnd, { passive: true });

            dragBoundContainerEl = container;
            dragListenersCleanup = () => {
                container.removeEventListener('mousedown', onPointerDown);
                container.removeEventListener('mousemove', onPointerMove);
                container.removeEventListener('touchstart', onPointerDown);
                container.removeEventListener('touchmove', onPointerMove);
                window.removeEventListener('mouseup', onPointerEnd);
                window.removeEventListener('touchend', onPointerEnd);
                window.removeEventListener('touchcancel', onPointerEnd);
            };
        } catch (e) {}
    }

    if (window.GHI18n && typeof window.GHI18n.onChange === 'function') {
        window.GHI18n.onChange(() => {
            if (!autoPanEnabled) {
                updateRecenterButtonUI();
            }
        });
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
    // 1. Lightweight Auto-Dismiss Mini Toast & Theme Styles
    // =========================================================================
    function showMiniToast(msg, color) {
        try {
            if (!msg) return;
            injectThemeStyles();

            let toast = document.getElementById('gh-mini-toast');
            if (!toast) {
                toast = document.createElement('div');
                toast.id = 'gh-mini-toast';
                toast.className = 'gh-root';
                toast.setAttribute('aria-live', 'polite');

                const dot = document.createElement('span');
                dot.id = 'gh-mini-toast-dot';
                const text = document.createElement('span');
                text.id = 'gh-mini-toast-text';

                toast.appendChild(dot);
                toast.appendChild(text);
                (document.body || document.documentElement).appendChild(toast);
            }

            const dotEl = toast.querySelector('#gh-mini-toast-dot');
            const textEl = toast.querySelector('#gh-mini-toast-text');
            const dotColor = color || '#38bdf8';

            if (dotEl) {
                dotEl.style.background = dotColor;
                dotEl.style.boxShadow = `0 0 8px ${dotColor}`;
            }
            if (textEl) {
                textEl.textContent = String(msg);
            }

            toast.classList.add('gh-toast-visible');
            if (miniToastTimer) {
                clearTimeout(miniToastTimer);
            }
            miniToastTimer = setTimeout(() => {
                miniToastTimer = null;
                const activeToast = document.getElementById('gh-mini-toast');
                if (activeToast) {
                    activeToast.classList.remove('gh-toast-visible');
                }
            }, 2400);
        } catch (e) {}
    }

    function setLog(msg, color) {
        console.log('[Genshin Helper v5.5.0]', msg);
        if (singleMonitorMode && document.hidden) {
            return;
        }
        showMiniToast(msg, color);
    }

    function updateHotkeyDisplay(displayName, action = 'sync', rawKey = null) {
        if (!displayName) return;
        const act = (action || 'sync').toLowerCase();
        if (act === 'debug') {
            currentHotkeyDebugDisplay = displayName;
            if (rawKey) currentHotkeyDebugRaw = rawKey;
            try {
                localStorage.setItem('gh_hotkey_debug_display', displayName);
                if (rawKey) localStorage.setItem('gh_hotkey_debug_raw', rawKey);
            } catch(e) {}
        } else if (act === 'switch') {
            currentHotkeySwitchDisplay = displayName;
            if (rawKey) currentHotkeySwitchRaw = rawKey;
            try {
                localStorage.setItem('gh_hotkey_switch_display', displayName);
                if (rawKey) localStorage.setItem('gh_hotkey_switch_raw', rawKey);
            } catch(e) {}
        } else {
            currentHotkeySyncDisplay = displayName;
            if (rawKey) currentHotkeySyncRaw = rawKey;
            try {
                localStorage.setItem('gh_hotkey_sync_display', displayName);
                localStorage.setItem('gh_hotkey_display', displayName);
                if (rawKey) localStorage.setItem('gh_hotkey_sync_raw', rawKey);
            } catch(e) {}
        }
    }

    function injectThemeStyles() {
        if (document.getElementById('gh-theme-styles')) return;

        const style = document.createElement('style');
        style.id = 'gh-theme-styles';
        style.textContent = `
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

            /* --- Lightweight Auto-Dismiss Toast --- */
            #gh-mini-toast {
                position: fixed;
                top: 16px;
                left: 50%;
                transform: translateX(-50%) translateY(-8px);
                z-index: 2147483646;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                max-width: min(480px, calc(100vw - 32px));
                padding: 7px 14px;
                border-radius: 999px;
                background: rgba(15, 23, 42, 0.90);
                backdrop-filter: blur(12px);
                border: 1px solid rgba(56, 189, 248, 0.35);
                box-shadow: 0 10px 26px rgba(2, 6, 23, 0.55);
                color: #f8fafc;
                font-size: 12px;
                font-weight: 500;
                line-height: 1.3;
                pointer-events: none;
                opacity: 0;
                transition: opacity 0.22s ease, transform 0.22s ease;
            }
            #gh-mini-toast.gh-toast-visible {
                opacity: 1;
                transform: translateX(-50%) translateY(0);
            }
            #gh-mini-toast-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                flex-shrink: 0;
                background: #38bdf8;
            }
            #gh-mini-toast-text {
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            /* --- Floating Re-center Button (when user drags map to unpin) --- */
            #gh-recenter-btn {
                position: fixed;
                bottom: 22px;
                left: 50%;
                transform: translateX(-50%) translateY(10px);
                z-index: 2147483645;
                display: inline-flex;
                align-items: center;
                gap: 6px;
                padding: 7px 14px;
                border-radius: 999px;
                background: rgba(15, 23, 42, 0.92);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(56, 189, 248, 0.55);
                box-shadow: 0 8px 22px rgba(2, 6, 23, 0.55);
                color: #e0f2fe;
                font-size: 12px;
                font-weight: 600;
                cursor: pointer;
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.2s ease, transform 0.2s ease, background 0.15s ease;
            }
            #gh-recenter-btn.gh-recenter-visible {
                opacity: 1;
                pointer-events: auto;
                transform: translateX(-50%) translateY(0);
            }
            #gh-recenter-btn:hover {
                background: rgba(14, 165, 233, 0.32);
                border-color: #38bdf8;
                color: #ffffff;
            }

            /* --- Live In-Place Dual-Rotation Player Marker (--gh-dir & --gh-rot) --- */
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
                --gh-dir: 0deg;
                --gh-rot: 0deg;
            }
            /* High-contrast base disc: keeps the arrow readable on top of busy, colourful pin icons */
            .gh-player-marker-wrap::before {
                content: '';
                position: absolute;
                left: 50%;
                top: 50%;
                width: 28px;
                height: 28px;
                transform: translate(-50%, -50%);
                border-radius: 50%;
                background: rgba(15, 23, 42, 0.82);
                border: 2px solid #ffffff;
                box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.55), 0 2px 10px rgba(0, 0, 0, 0.55);
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
            .gh-player-fov-svg {
                position: absolute;
                left: 50%;
                top: 50%;
                width: 72px;
                height: 72px;
                transform-origin: 50% 50%;
                transform: translate(-50%, -50%) rotate(var(--gh-rot, 0deg));
                opacity: 0;
                transition: transform 90ms linear, opacity 160ms ease;
                will-change: transform;
                pointer-events: none;
            }
            .gh-player-marker-wrap.gh-has-fov .gh-player-fov-svg {
                opacity: 1;
            }
            .gh-player-arrow-svg {
                position: absolute;
                left: 50%;
                top: 50%;
                width: 30px;
                height: 30px;
                transform-origin: 50% 50%;
                transform: translate(-50%, -50%) rotate(var(--gh-dir, 0deg));
                transition: transform 90ms linear;
                will-change: transform;
                filter: drop-shadow(0 2px 6px rgba(2, 132, 199, 0.85));
                pointer-events: none;
            }

            /* --- TSP Smart Route SVG Overlay (overlayPane) --- */
            .gh-tsp-route-svg {
                position: absolute;
                left: 0;
                top: 0;
                pointer-events: none;
                overflow: visible;
                z-index: 450;
            }
            .gh-tsp-chevron-text {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                font-size: 11px;
                font-weight: 700;
                fill: #e0f2fe;
                stroke: #0f172a;
                stroke-width: 2.2px;
                paint-order: stroke;
                letter-spacing: 2px;
            }
            .gh-tsp-stop-label {
                font-family: 'JetBrains Mono', 'SF Mono', Consolas, Monaco, monospace;
                font-size: 10px;
                font-weight: 700;
                fill: #f8fafc;
            }

            /* --- Safe Compact HoYoLAB Web UI (opt-in via body.gh-compact-ui) --- */
            body.gh-compact-ui .gh-compact-hide,
            body.gh-compact-ui .mhy-header,
            body.gh-compact-ui .hoyolab-header,
            body.gh-compact-ui .mhy-hoyolab-app-header,
            body.gh-compact-ui [class*="mhy-header"],
            body.gh-compact-ui [class*="hoyolab-header"],
            body.gh-compact-ui [class*="bbs-header"],
            body.gh-compact-ui .map-notice-bar,
            body.gh-compact-ui [class*="notice-bar"],
            body.gh-compact-ui .bbs-qr,
            body.gh-compact-ui [class*="bbs-qr"],
            body.gh-compact-ui [class*="qr-code"],
            body.gh-compact-ui [class*="qrcode"],
            body.gh-compact-ui [class*="download-qr"],
            body.gh-compact-ui .mihoyo-cookie-tips,
            body.gh-compact-ui .strong-tip-closable,
            body.gh-compact-ui .driver-popover {
                display: none !important;
            }
            body.gh-compact-ui .filter-panel,
            body.gh-compact-ui .gh-compact-sidebar {
                transform: translateX(calc(-100% + 26px)) !important;
                opacity: 0.35 !important;
                transition: transform 0.22s ease, opacity 0.22s ease !important;
            }
            body.gh-compact-ui .filter-panel:hover,
            body.gh-compact-ui .filter-panel:focus-within,
            body.gh-compact-ui .gh-compact-sidebar:hover,
            body.gh-compact-ui .gh-compact-sidebar:focus-within {
                transform: translateX(0) !important;
                opacity: 1 !important;
            }

            /* --- CRITICAL SPA SAFETY GUARD (never hide/remove .gt-modal, .gt-popup-root, or .map-loading-mask) --- */
            .gt-modal[data-modal-visible-count="0"] {
                pointer-events: none !important;
            }
            .map-loading-mask {
                pointer-events: none !important;
            }
        `;
        (document.head || document.documentElement).appendChild(style);
    }

    function setStatus(connected, active = engineActive) {
        isConnected = Boolean(connected);
        if (typeof active === 'boolean') {
            engineActive = active;
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
        if (found && found.vm && found.vm.$store) return found.vm;
        const appVm = document.querySelector('#app')?.__vue__;
        if (appVm && appVm.$store) return appVm;
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
                    setLog(tr('page.area.switched', { name: targetArea.name }), '#38bdf8');
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
                setLog(tr('page.area.switched', { name: areaName || tr('page.area.fallback', { id: areaId }) }), '#38bdf8');
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

    function unwrapAngle(prev, next) {
        if (typeof next !== 'number' || !Number.isFinite(next)) {
            return (typeof prev === 'number' && Number.isFinite(prev)) ? prev : 0;
        }
        if (typeof prev !== 'number' || !Number.isFinite(prev)) {
            return ((next % 360) + 360) % 360;
        }
        const delta = ((next - prev) % 360 + 540) % 360 - 180;
        return prev + delta;
    }

    function _buildPlayerMarkerMarkup() {
        return (
            '<div class="gh-player-marker-wrap">' +
                '<div class="gh-player-pulse-ring"></div>' +
                '<svg class="gh-player-fov-svg" viewBox="0 0 64 64" aria-hidden="true">' +
                    '<defs>' +
                        '<radialGradient id="gh-fov-grad" cx="50%" cy="50%" r="50%">' +
                            '<stop offset="0%" stop-color="#38bdf8" stop-opacity="0.46"/>' +
                            '<stop offset="68%" stop-color="#38bdf8" stop-opacity="0.16"/>' +
                            '<stop offset="100%" stop-color="#38bdf8" stop-opacity="0"/>' +
                        '</radialGradient>' +
                    '</defs>' +
                    '<path d="M32 32 L15.94 9.06 A28 28 0 0 1 48.06 9.06 Z" fill="url(#gh-fov-grad)"/>' +
                '</svg>' +
                '<svg class="gh-player-arrow-svg" viewBox="0 0 32 32" aria-hidden="true">' +
                    '<path d="M16 4.5 L25 24 L16 19.5 L7 24 Z" fill="#38bdf8" stroke="#ffffff" stroke-width="2" stroke-linejoin="round"/>' +
                '</svg>' +
            '</div>'
        );
    }

    function _createPlayerMarkerWrapNode() {
        const SVG_NS = 'http://www.w3.org/2000/svg';
        const wrap = document.createElement('div');
        wrap.className = 'gh-player-marker-wrap';

        const pulse = document.createElement('div');
        pulse.className = 'gh-player-pulse-ring';
        wrap.appendChild(pulse);

        const fovSvg = document.createElementNS(SVG_NS, 'svg');
        fovSvg.setAttribute('class', 'gh-player-fov-svg');
        fovSvg.setAttribute('viewBox', '0 0 64 64');
        fovSvg.setAttribute('aria-hidden', 'true');

        const defs = document.createElementNS(SVG_NS, 'defs');
        const grad = document.createElementNS(SVG_NS, 'radialGradient');
        grad.setAttribute('id', 'gh-fov-grad-dom');
        grad.setAttribute('cx', '50%');
        grad.setAttribute('cy', '50%');
        grad.setAttribute('r', '50%');

        const stops = [
            ['0%', '0.46'],
            ['68%', '0.16'],
            ['100%', '0']
        ];
        for (const [offset, opacity] of stops) {
            const stop = document.createElementNS(SVG_NS, 'stop');
            stop.setAttribute('offset', offset);
            stop.setAttribute('stop-color', '#38bdf8');
            stop.setAttribute('stop-opacity', opacity);
            grad.appendChild(stop);
        }
        defs.appendChild(grad);
        fovSvg.appendChild(defs);

        const fovPath = document.createElementNS(SVG_NS, 'path');
        fovPath.setAttribute('d', 'M32 32 L15.94 9.06 A28 28 0 0 1 48.06 9.06 Z');
        fovPath.setAttribute('fill', 'url(#gh-fov-grad-dom)');
        fovSvg.appendChild(fovPath);
        wrap.appendChild(fovSvg);

        const arrowSvg = document.createElementNS(SVG_NS, 'svg');
        arrowSvg.setAttribute('class', 'gh-player-arrow-svg');
        arrowSvg.setAttribute('viewBox', '0 0 32 32');
        arrowSvg.setAttribute('aria-hidden', 'true');

        const arrowPath = document.createElementNS(SVG_NS, 'path');
        arrowPath.setAttribute('d', 'M16 4.5 L25 24 L16 19.5 L7 24 Z');
        arrowPath.setAttribute('fill', '#38bdf8');
        arrowPath.setAttribute('stroke', '#ffffff');
        arrowPath.setAttribute('stroke-width', '2');
        arrowPath.setAttribute('stroke-linejoin', 'round');
        arrowSvg.appendChild(arrowPath);
        wrap.appendChild(arrowSvg);

        return wrap;
    }

    function _applyPlayerMarkerProps(wrapEl, data, unwrappedDir, hasCameraHeading, unwrappedRot) {
        if (!wrapEl) return;
        wrapEl.style.setProperty('--gh-dir', `${unwrappedDir.toFixed(2)}deg`);
        if (hasCameraHeading) {
            wrapEl.style.setProperty('--gh-rot', `${unwrappedRot.toFixed(2)}deg`);
            wrapEl.classList.add('gh-has-fov');
        } else {
            wrapEl.classList.remove('gh-has-fov');
        }
        const latStr = (data && typeof data.lat === 'number') ? data.lat.toFixed(1) : '?';
        const lngStr = (data && typeof data.lng === 'number') ? data.lng.toFixed(1) : '?';
        const floorTag = (data && data.is_underground && (data.floor_short || data.floor_name))
            ? ` • ${data.floor_short || data.floor_name}`
            : '';
        wrapEl.setAttribute('title', tr('page.marker.title', { lat: latStr, lng: lngStr, floor: floorTag }));
    }

    function _repositionDomPlayerMarker() {
        try {
            if (!playerDomMarkerEl || !playerDomMapRef || !playerLastState) return;
            if (typeof playerDomMapRef.latLngToLayerPoint === 'function') {
                const pt = playerDomMapRef.latLngToLayerPoint([playerLastState.lat, playerLastState.lng]);
                playerDomMarkerEl.style.transform = `translate3d(${(pt.x - 22).toFixed(2)}px, ${(pt.y - 22).toFixed(2)}px, 0)`;
            }
        } catch(e) {}
    }

    // HoYoLAB draws every pin into a single <canvas class="leaflet-canvas-icon-layer"> inside
    // markerPane with `z-index: 9999`, while Leaflet gives our marker `z-index = y + offset`
    // (≈ 5000) in the same pane — so the player arrow always sank under the pin icons. A
    // dedicated pane above markerPane (600) and tooltipPane (650) but below popupPane (700)
    // keeps the arrow on top of every pin without covering pin popups.
    const PLAYER_PANE_NAME = 'ghPlayerPane';
    const PLAYER_PANE_Z = 690;

    function _ensurePlayerPane(map) {
        try {
            if (!map || typeof map.getPane !== 'function') return null;
            let pane = map.getPane(PLAYER_PANE_NAME);
            if (!pane && typeof map.createPane === 'function') {
                pane = map.createPane(PLAYER_PANE_NAME);
            }
            if (pane) {
                pane.style.zIndex = String(PLAYER_PANE_Z);
                pane.style.pointerEvents = 'none';
            }
            return pane || null;
        } catch (e) {
            return null;
        }
    }

    function _cleanupOrphanPlayerMarkers(keepEl = null) {
        try {
            const nodes = document.querySelectorAll('#gh-player-marker-dom, .gh-player-leaflet-icon, .gh-player-marker-wrap');
            for (let i = 0; i < nodes.length; i++) {
                const node = nodes[i];
                if (!node || !node.isConnected) continue;
                let host = node;
                if (node.classList && node.classList.contains('gh-player-marker-wrap')) {
                    host = node.closest('#gh-player-marker-dom, .gh-player-leaflet-icon') || node;
                }
                if (keepEl && (host === keepEl || (typeof keepEl.contains === 'function' && keepEl.contains(host)))) {
                    continue;
                }
                try { host.remove(); } catch (e) {}
            }
        } catch (e) {}
    }

    function updatePlayerMarker(ctx, data) {
        try {
            if (!ctx || !ctx.map || !data || typeof data.lat !== 'number' || typeof data.lng !== 'number') return;
            injectThemeStyles();
            bindDragUnpinListeners(ctx);

            const rawDir = (typeof data.heading === 'number' && Number.isFinite(data.heading)) ? data.heading : null;
            if (rawDir !== null) {
                playerUnwrappedDir = unwrapAngle(playerUnwrappedDir, rawDir);
            } else if (playerUnwrappedDir === null) {
                playerUnwrappedDir = 0;
            }

            const rawCam = (typeof data.camera_heading === 'number' && Number.isFinite(data.camera_heading))
                ? data.camera_heading
                : null;
            const hasCameraHeading = rawCam !== null;
            if (hasCameraHeading) {
                playerUnwrappedRot = unwrapAngle(playerUnwrappedRot, rawCam);
            }

            playerLastState = {
                lat: data.lat,
                lng: data.lng,
                heading: rawDir !== null ? rawDir : (playerLastState ? playerLastState.heading : 0),
                camera_heading: hasCameraHeading ? rawCam : null,
                is_underground: Boolean(data.is_underground),
                floor_id: data.floor_id !== undefined ? data.floor_id : (playerLastState ? playerLastState.floor_id : null),
                floor_short: data.floor_short || data.floor_name || null,
                map_id: data.map_id ? parseInt(data.map_id, 10) : getCurrentWebMapId()
            };

            // Primary path: native Leaflet L.marker created ONCE, updated in-place via setLatLng + CSS custom properties
            if (window.L && typeof window.L.marker === 'function' && typeof window.L.divIcon === 'function') {
                if (playerDomMarkerEl) {
                    if (playerDomMapRef && typeof playerDomMapRef.off === 'function') {
                        playerDomMapRef.off('move zoom viewreset zoomend', _repositionDomPlayerMarker);
                    }
                    playerDomMarkerEl.remove();
                    playerDomMarkerEl = null;
                    playerDomWrapEl = null;
                    playerDomMapRef = null;
                }
                const playerPane = _ensurePlayerPane(ctx.map);
                if (playerMarkerLayer && (playerMarkerMapRef !== ctx.map || (playerPane && playerMarkerLayer.options.pane !== PLAYER_PANE_NAME))) {
                    try { playerMarkerLayer.remove(); } catch(e) {}
                    playerMarkerLayer = null;
                    playerMarkerWrapEl = null;
                }
                const createdLayer = !playerMarkerLayer;
                if (!playerMarkerLayer) {
                    const icon = window.L.divIcon({
                        className: 'gh-player-leaflet-icon',
                        html: _buildPlayerMarkerMarkup(),
                        iconSize: [44, 44],
                        iconAnchor: [22, 22]
                    });
                    const markerOpts = {
                        icon: icon,
                        interactive: false,
                        zIndexOffset: 9000
                    };
                    if (playerPane) markerOpts.pane = PLAYER_PANE_NAME;
                    playerMarkerLayer = window.L.marker([data.lat, data.lng], markerOpts).addTo(ctx.map);
                    playerMarkerMapRef = ctx.map;
                    const el = typeof playerMarkerLayer.getElement === 'function' ? playerMarkerLayer.getElement() : null;
                    playerMarkerWrapEl = el ? el.querySelector('.gh-player-marker-wrap') : null;
                } else {
                    playerMarkerLayer.setLatLng([data.lat, data.lng]);
                    if (!playerMarkerWrapEl || !playerMarkerWrapEl.isConnected) {
                        const el = typeof playerMarkerLayer.getElement === 'function' ? playerMarkerLayer.getElement() : null;
                        playerMarkerWrapEl = el ? el.querySelector('.gh-player-marker-wrap') : null;
                    }
                }
                const activeEl = typeof playerMarkerLayer.getElement === 'function' ? playerMarkerLayer.getElement() : null;
                if (createdLayer || document.getElementsByClassName('gh-player-marker-wrap').length > 1) {
                    _cleanupOrphanPlayerMarkers(activeEl);
                }
                _applyPlayerMarkerProps(
                    playerMarkerWrapEl,
                    data,
                    playerUnwrappedDir,
                    hasCameraHeading,
                    playerUnwrappedRot || 0
                );
                return;
            }

            // Fallback path for bundled HoYoLAB Leaflet (created ONCE in the top player pane, updated in-place via translate3d + CSS vars)
            if (playerMarkerLayer) {
                try { playerMarkerLayer.remove(); } catch (e) {}
                playerMarkerLayer = null;
                playerMarkerMapRef = null;
                playerMarkerWrapEl = null;
            }
            const pane = _ensurePlayerPane(ctx.map)
                || ((typeof ctx.map.getPanes === 'function' && ctx.map.getPanes() && ctx.map.getPanes().markerPane)
                    ? ctx.map.getPanes().markerPane
                    : (typeof ctx.map.getContainer === 'function' ? ctx.map.getContainer() : null));
            if (!pane) return;

            const createdDom = (!playerDomMarkerEl || !playerDomMarkerEl.isConnected || playerDomMapRef !== ctx.map || playerDomMarkerEl.parentNode !== pane);
            if (createdDom) {
                if (playerDomMapRef && playerDomMapRef !== ctx.map && typeof playerDomMapRef.off === 'function') {
                    playerDomMapRef.off('move zoom viewreset zoomend', _repositionDomPlayerMarker);
                }
                if (playerDomMarkerEl) playerDomMarkerEl.remove();
                playerDomMarkerEl = document.createElement('div');
                playerDomMarkerEl.id = 'gh-player-marker-dom';
                playerDomMarkerEl.style.cssText = 'position:absolute;left:0;top:0;width:44px;height:44px;z-index:9000;pointer-events:none;will-change:transform;';
                playerDomWrapEl = _createPlayerMarkerWrapNode();
                playerDomMarkerEl.appendChild(playerDomWrapEl);
                pane.appendChild(playerDomMarkerEl);
                if (playerDomMapRef !== ctx.map && typeof ctx.map.on === 'function') {
                    playerDomMapRef = ctx.map;
                    ctx.map.on('move zoom viewreset zoomend', _repositionDomPlayerMarker);
                } else {
                    playerDomMapRef = ctx.map;
                }
            }
            if (createdDom || document.getElementsByClassName('gh-player-marker-wrap').length > 1) {
                _cleanupOrphanPlayerMarkers(playerDomMarkerEl);
            }
            _applyPlayerMarkerProps(
                playerDomWrapEl,
                data,
                playerUnwrappedDir,
                hasCameraHeading,
                playerUnwrappedRot || 0
            );
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
                        const allowAnim = Boolean(animate) && !document.hidden && !singleMonitorMode;
                        ctx.map.setView([data.lat, data.lng], finalZoom, {
                            animate: allowAnim,
                            duration: allowAnim ? 0.25 : 0
                        });
                        lastSyncedView = {
                            lat: data.lat,
                            lng: data.lng,
                            zoom: finalZoom,
                            mapId: data.map_id ? parseInt(data.map_id, 10) : 2
                        };
                        if (document.hidden) {
                            pendingVisibleRefresh = true;
                        }
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
                        setLog(tr('page.layer.exit_surface'), '#38bdf8');
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
                const label = resolvedFloor.floor_name || resolvedFloor.floor_name_short || group.name || tr('page.layer.default_underground');

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
                            setLog(tr('page.layer.switch_floor', { label }), '#22c55e');
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
                            setLog(tr('page.layer.switch_floor', { label }), '#22c55e');
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
                        setLog(tr('page.layer.enter_explore', { label }), '#22c55e');
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
                        const floorDisplay = txt || tr('page.layer.floor_fallback', { idx: floorIdx });
                        if (forceKickstart && floorG && floorG !== target) {
                            dispatchFullClick(floorG);
                            setTimeout(() => {
                                dispatchFullClick(target);
                                setLog(tr('page.layer.activated_floor', { label: floorDisplay }), '#22c55e');
                            }, 130);
                        } else {
                            dispatchFullClick(target);
                            setLog(tr('page.layer.selected_floor', { label: floorDisplay }), '#22c55e');
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
                        const allowAnim = !document.hidden && !singleMonitorMode;
                        ctx.map.setView([targetLat, targetLng], finalZoom, { animate: allowAnim, duration: allowAnim ? 0.5 : 0 });
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
                setLog(tr('page.layer.exit_surface'), '#38bdf8');
                return true;
            }
            const exitBtn = document.querySelector('.underground-elevator__exit, [class*="underground-elevator__exit"], .underground-elevator [class*="exit"], #underground-elevator [class*="exit"]');
            if (exitBtn) {
                exitBtn.click();
                setLog(tr('page.layer.exit_surface'), '#38bdf8');
                return true;
            }
        } catch (e) {}
        return false;
    }

    // =========================================================================
    // 4. Extension Bridge Events & Live State Sync
    // =========================================================================
    function warnServerVersionMismatch(data) {
        if (data && data.script_version && data.script_version !== SCRIPT_VERSION && !warnedVersionMismatch) {
            warnedVersionMismatch = true;
            console.warn(`[Genshin Helper] Phiên bản Desktop (${data.script_version}) khác Extension (${SCRIPT_VERSION}).`);
        }
    }

    const onWindowMessage = (e) => {
        if (window.__gh_booted_build !== SCRIPT_BUILD) return;
        if (e.source !== window || !e.data || e.data.__gh_ext !== true || e.data.dir !== 'to-page') {
            return;
        }
        const msg = e.data;

        if (msg.reqId && (msg.kind === 'API_RESPONSE' || Object.prototype.hasOwnProperty.call(msg, 'response'))) {
            const entry = pendingExtRequests.get(msg.reqId);
            if (!entry) return;
            pendingExtRequests.delete(msg.reqId);
            clearTimeout(entry.timer);
            const resp = msg.response;
            if (resp && resp.ok) {
                entry.resolve(resp.data);
            } else {
                const err = new Error((resp && resp.error) || `HTTP ${(resp && resp.status) || 0}`);
                err.status = (resp && resp.status) || 0;
                err.data = (resp && resp.data) || null;
                entry.reject(err);
            }
            return;
        }

        if (msg.kind === 'CONNECTION_STATUS') {
            const wasConnected = isConnected;
            const nextEngine = typeof msg.engineActive === 'boolean' ? msg.engineActive : engineActive;
            setStatus(Boolean(msg.connected), nextEngine);
            if (msg.connected && (!wasConnected || !initialStatusSynced)) {
                fetchInitialStatus();
            }
            return;
        }

        if (msg.kind === 'BRIDGE_READY') {
            if (window.GHI18n && typeof window.GHI18n.isReady === 'function' && !window.GHI18n.isReady()) {
                try {
                    window.postMessage({ __gh_ext: true, dir: 'to-isolated', kind: 'I18N_REQUEST' }, window.location.origin);
                } catch (err) {}
            }
            if (!initialStatusSynced) {
                fetchInitialStatus();
            }
            return;
        }

        if (msg.kind === 'COMPACT_UI_SYNC' && typeof msg.enabled === 'boolean') {
            setCompactUI(msg.enabled, { syncStorage: false });
            return;
        }

        const directCmd = msg.kind || msg.type;
        if (directCmd === 'GH_CMD_PLAN_TSP' || directCmd === 'GH_CMD_CLEAR_TSP' || directCmd === 'GH_CMD_EXPORT_PINS') {
            handleLocalCommand({ ...msg, type: directCmd });
            return;
        }

        if (msg.kind === 'SERVER_EVENT' && msg.data) {
            const data = msg.data;
            if (data._local_cmd === true || data.type === 'GH_CMD_PLAN_TSP' || data.type === 'GH_CMD_CLEAR_TSP' || data.type === 'GH_CMD_EXPORT_PINS') {
                handleLocalCommand(data);
                return;
            }
            setStatus(true);
            warnServerVersionMismatch(data);
            if (typeof data._seq === 'number') {
                lastEventSeq = data._seq;
            }
            if (data.type && data.type !== 'HEARTBEAT') {
                try {
                    handleServerMessage(data);
                } catch (err) {
                    console.error('[Genshin Helper] handleServerMessage error:', err);
                }
            }
        }
    };
    window.addEventListener('message', onWindowMessage);

    function sendActionFeedback(eventType, status, title = '', detail = '', seq = 0) {
        apiRequest('/action_feedback', {
            method: 'POST',
            body: {
                event_type: eventType,
                status: status,
                title: title || '',
                detail: detail || '',
                seq: typeof seq === 'number' ? seq : 0
            }
        }).catch(() => {});
    }

    const onVisibilityChange = () => {
        if (document.hidden || !pendingVisibleRefresh) return;
        pendingVisibleRefresh = false;
        try {
            const ctx = findMapContext();
            if (ctx && ctx.map) {
                if (typeof ctx.map.invalidateSize === 'function') {
                    ctx.map.invalidateSize({ animate: false });
                }
                if (lastSyncedView && getCurrentWebMapId() === Number(lastSyncedView.mapId || 2)) {
                    ctx.map.setView([lastSyncedView.lat, lastSyncedView.lng], lastSyncedView.zoom, { animate: false });
                }
                _repositionDomPlayerMarker();
                _scheduleTspRouteRedraw();
                forceRefreshCaveCanvas(resolveGisVm(ctx));
            }
        } catch (e) {}
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    function reportState() {
        try {
            const ctx = findMapContext();
            if (!ctx.map) return;
            _ensureMarkRedrawHooks(ctx);
            _reconcileMarkedVisuals(ctx, { skipSelectedPopup: true });
            const center = ctx.map.getCenter();
            const zoom = ctx.map.getZoom();
            const markersCount = (ctx.markerList && ctx.markerList.length) || (ctx.ciLayer && ctx.ciLayer._latlngMarkers && ctx.ciLayer._latlngMarkers.all ? ctx.ciLayer._latlngMarkers.all().length : 0);
            const gisVm = resolveGisVm(ctx);
            const st = gisVm && gisVm.$store && gisVm.$store.state ? gisVm.$store.state : {};
            const areaInfo = st.areaInfo || {};
            const layerInfo = st.layeredExplorationInfo || null;
            apiRequest('/report_state', {
                method: 'POST',
                body: {
                    center: [center.lat, center.lng],
                    zoom: zoom,
                    markersCount: markersCount,
                    activeAreaId: areaInfo.id !== undefined ? areaInfo.id : null,
                    activeAreaName: areaInfo.name || null,
                    layeredGroupId: layerInfo && layerInfo.id !== undefined ? layerInfo.id : null,
                    layeredFloorId: layerInfo && layerInfo.activeFloorId !== undefined ? layerInfo.activeFloorId : null,
                    scriptVersion: SCRIPT_VERSION,
                    scriptBuild: SCRIPT_BUILD,
                    runtimeMode: 'extension'
                }
            }).catch(() => {});
        } catch (e) {}
    }

    const reportStateTimer = setInterval(reportState, 5000);

    function fetchInitialStatus() {
        apiRequest('/status', { method: 'GET' })
            .then(data => {
                if (data) {
                    initialStatusSynced = true;
                    const activeState = typeof data.engine_active === 'boolean' ? data.engine_active : engineActive;
                    setStatus(true, activeState);
                    warnServerVersionMismatch(data);
                    if (typeof data.latest_seq === 'number' && (lastEventSeq < 0 || data.latest_seq < lastEventSeq)) {
                        lastEventSeq = data.latest_seq;
                    }
                    if (data.display_name_sync) updateHotkeyDisplay(data.display_name_sync, 'sync', data.hotkey_sync || null);
                    else if (data.display_name) updateHotkeyDisplay(data.display_name, 'sync', data.hotkey || null);
                    if (data.display_name_debug) updateHotkeyDisplay(data.display_name_debug, 'debug', data.hotkey_debug || null);
                    if (data.display_name_switch) updateHotkeyDisplay(data.display_name_switch, 'switch', data.hotkey_switch || null);
                    if (typeof data.auto_sync_m === 'boolean') setAutoSyncUI(data.auto_sync_m);
                    const monCnt = typeof data.monitor_count === 'number' ? data.monitor_count : undefined;
                    if (typeof data.single_monitor_mode === 'boolean') {
                        setSingleMonitorUI(data.single_monitor_mode, monCnt);
                    } else if (monCnt !== undefined) {
                        setSingleMonitorUI(singleMonitorMode, monCnt);
                    }
                    if (data.language && window.GHI18n && !window.GHI18n.hasExplicitPreference()) {
                        window.GHI18n.setLang(data.language, { notifyIsolated: false });
                    }
                }
            })
            .catch(() => {
                setStatus(false);
            });
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

            overlay.innerHTML = `
                <div style="background: rgba(15, 23, 42, 0.98); border: 1px solid rgba(245, 158, 11, 0.55); border-radius: 16px; padding: 20px 24px; max-width: 500px; width: 92%; color: #fff; box-shadow: 0 24px 60px rgba(0,0,0,0.85); display: flex; flex-direction: column; gap: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; border-bottom: 1px solid rgba(51, 65, 85, 0.6); padding-bottom: 10px;">
                        <div>
                            <strong id="gh-note-title" style="color: #fbbf24; font-size: 15px; font-weight: 700;"></strong>
                            <div id="gh-note-folder" class="gh-mono" style="font-size: 11.5px; color: #94a3b8; margin-top: 2px;"></div>
                        </div>
                        <span id="gh-note-badge" style="background: ${badgeBg}; border: 1px solid ${badgeBorder}; color: ${badgeColor}; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px; white-space: nowrap;"></span>
                    </div>
                    <div id="gh-note-reason" style="font-size: 11.5px; color: #cbd5e1; display: none;"></div>
                    <div id="gh-note-tags" style="display: flex; flex-wrap: wrap; gap: 6px;"></div>
                    <textarea id="gh-diag-note-input" rows="4" style="width: 100%; box-sizing: border-box; background: rgba(2, 6, 23, 0.8); border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 10px; padding: 10px 12px; color: #f8fafc; font-size: 12.5px; line-height: 1.45; resize: vertical; outline: none; font-family: inherit;"></textarea>
                    <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px;">
                        <span id="gh-note-shortcut" style="font-size: 11px; color: #64748b;"></span>
                        <div style="display: flex; gap: 8px;">
                            <button id="gh-note-skip" style="background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.16); color: #94a3b8; border-radius: 8px; padding: 7px 14px; font-size: 12px; cursor: pointer;"></button>
                            <button id="gh-note-save" style="background: #d97706; border: none; color: #fff; font-weight: 600; border-radius: 8px; padding: 7px 16px; font-size: 12px; cursor: pointer;"></button>
                        </div>
                    </div>
                    <div id="gh-note-error" style="color: #ef4444; font-size: 11.5px; min-height: 14px;"></div>
                </div>
            `;

            document.body.appendChild(overlay);
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

            const renderModalLabels = () => {
                const badgeEl = overlay.querySelector('#gh-note-badge');
                const titleEl = overlay.querySelector('#gh-note-title');
                const folderEl = overlay.querySelector('#gh-note-folder');
                const shortcutEl = overlay.querySelector('#gh-note-shortcut');
                if (titleEl) titleEl.textContent = tr('page.diag.modal.title');
                if (badgeEl) badgeEl.textContent = hasWorldCoords ? tr('page.diag.modal.badge_ok') : tr('page.diag.modal.badge_warn');
                if (folderEl) folderEl.textContent = tr('page.diag.modal.bundle_prefix', { folder: fname });
                if (shortcutEl) shortcutEl.textContent = tr('page.diag.modal.shortcut_hint');
                textarea.placeholder = tr('page.diag.modal.placeholder');
                skipBtn.textContent = tr('page.diag.modal.skip');
                if (!saveBtn.disabled) {
                    saveBtn.textContent = tr('page.diag.modal.save');
                }
                const tagKeys = [
                    'page.diag.tag.wrong_pos',
                    'page.diag.tag.wrong_floor',
                    'page.diag.tag.no_cave_img',
                    'page.diag.tag.panning_no_arrow',
                    'page.diag.tag.other',
                ];
                tagsWrap.innerHTML = '';
                tagKeys.forEach((tKey) => {
                    const tag = tr(tKey);
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
            };

            renderModalLabels();
            let unsubI18n = null;
            if (window.GHI18n && typeof window.GHI18n.onChange === 'function') {
                unsubI18n = window.GHI18n.onChange(() => {
                    if (document.body.contains(overlay)) {
                        renderModalLabels();
                    }
                });
                if (typeof window.GHI18n.isReady === 'function' && !window.GHI18n.isReady()) {
                    try {
                        window.postMessage({ __gh_ext: true, dir: 'to-isolated', kind: 'I18N_REQUEST' }, window.location.origin);
                    } catch (err) {}
                }
            }

            const closeNoteModal = () => {
                document.removeEventListener('keydown', onModalKeyDown, true);
                if (typeof unsubI18n === 'function') {
                    try { unsubI18n(); } catch (err) {}
                    unsubI18n = null;
                }
                overlay.remove();
            };

            const submitNote = () => {
                const noteText = textarea.value.trim();
                saveBtn.disabled = true;
                saveBtn.textContent = tr('page.diag.modal.saving');
                errEl.textContent = '';

                apiRequest('/save_diagnostic_note', {
                    method: 'POST',
                    body: { folder_name: fname, note: noteText }
                })
                .then(res => {
                    if (res && res.ok) {
                        closeNoteModal();
                        setLog(tr('page.diag.modal.saved_toast', { folder: fname }), '#22c55e');
                    } else {
                        saveBtn.disabled = false;
                        saveBtn.textContent = tr('page.diag.modal.save');
                        errEl.textContent = (res && res.error) ? res.error : tr('page.diag.modal.save_error');
                    }
                })
                .catch(err => {
                    saveBtn.disabled = false;
                    saveBtn.textContent = tr('page.diag.modal.save');
                    errEl.textContent = (err && err.data && err.data.error)
                        ? err.data.error
                        : tr('page.diag.modal.offline_error');
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
                updateHotkeyDisplay(data.display_name, act, data.hotkey || null);
                setLog(tr('page.hotkey.updated', { action: act.toUpperCase(), name: data.display_name }), '#38bdf8');
            }
            return;
        }

        if (data.type === 'LANGUAGE_UPDATED') {
            if (data.language && window.GHI18n && typeof window.GHI18n.setLang === 'function') {
                window.GHI18n.setLang(data.language);
            }
            return;
        }

        if (data.type === 'AUTO_SYNC_UPDATED') {
            if (typeof data.auto_sync_m === 'boolean') {
                setAutoSyncUI(data.auto_sync_m);
                setLog(tr(data.auto_sync_m ? 'page.autosync.on' : 'page.autosync.off'), '#38bdf8');
            }
            return;
        }

        if (data.type === 'SINGLE_MONITOR_UPDATED') {
            if (typeof data.single_monitor_mode === 'boolean') {
                setSingleMonitorUI(data.single_monitor_mode);
                setLog(tr(data.single_monitor_mode ? 'page.single_monitor.on' : 'page.single_monitor.off'), '#10b981');
            }
            return;
        }

        if (data.type === 'ENGINE_STATE_UPDATED') {
            if (typeof data.engine_active === 'boolean') {
                setStatus(true, data.engine_active);
                setLog(
                    tr(data.engine_active ? 'page.engine.resumed' : 'page.engine.standby'),
                    data.engine_active ? '#22c55e' : '#fbbf24'
                );
            }
            return;
        }

        if (data.type === 'DIAGNOSTIC_CAPTURED') {
            playBeep();
            const folderName = data.folder_name || 'diagnostics';
            if (data.has_world_coords) {
                setLog(tr('page.diag.captured_ok', { folder: folderName }), '#22c55e');
            } else {
                setLog(tr('page.diag.captured_warn', { folder: folderName, reason: data.reason || '' }), '#fbbf24');
            }
            showDiagnosticNoteModal(folderName, Boolean(data.has_world_coords), data.reason || '');
            return;
        }

        if (data.type === 'TSP_ROUTE_UPDATED') {
            const route = Object.prototype.hasOwnProperty.call(data, 'route') ? data.route : data;
            const hasRoute = Boolean(
                route &&
                typeof route === 'object' &&
                !data.cleared &&
                ((Array.isArray(route.legs) && route.legs.length > 0) ||
                 (Array.isArray(route.ordered_pins) && route.ordered_pins.length > 0))
            );
            if (hasRoute) {
                renderTspRouteOnMap(route);
            } else {
                clearTspRouteFromMap();
            }
            return;
        }

        if (data.type === 'MINIMAP_POSITION' || data.type === 'LIVE_POSITION') {
            handleContinuousMinimapUpdate(data);
            return;
        }

        if (data.type === 'NAV_MARK_PIN') {
            handleNavMarkPin(data);
            return;
        }

        if (data.type === 'SYNC_POSITION_ONLY' || data.type === 'SYNC_AND_MARK') {
            const markAfterSync = (data.type === 'SYNC_AND_MARK');
            const eventSeq = typeof data._seq === 'number' ? data._seq : 0;
            if (data.detected === false) {
                if (markAfterSync) {
                    setLog(tr('page.sync.not_detected'), '#f59e0b');
                    sendActionFeedback(
                        'SYNC_AND_MARK',
                        'error',
                        tr('page.sync.not_detected_title'),
                        tr('page.sync.not_detected_detail'),
                        eventSeq
                    );
                }
                return;
            }
            if (!markAfterSync && (data.source === 'minimap' || data.continuous === true || data.silent === true)) {
                handleContinuousMinimapUpdate(data);
                return;
            }
            // Explicit Map (M) sync or Sync & Mark always auto-re-pins autoPanEnabled
            setAutoPanPinned(true, { silent: true });
            performAutoSyncAndMark(data, markAfterSync, eventSeq);
        }
    }

    function handleContinuousMinimapUpdate(data) {
        try {
            if (!data || typeof data.lat !== 'number' || typeof data.lng !== 'number') return;
            const ctx = findMapContext();
            if (!ctx || !ctx.map) return;
            _ensureMarkRedrawHooks(ctx);
            bindDragUnpinListeners(ctx);
            const targetMapId = data.map_id ? parseInt(data.map_id, 10) : 2;
            if (targetMapId !== getCurrentWebMapId()) return;

            updatePlayerMarker(ctx, data);
            pushNearbyPinsToServer(
                data.lat,
                data.lng,
                targetMapId,
                data.floor_id !== undefined ? data.floor_id : (data.floor_short || null)
            );
            if (autoPanEnabled) {
                const curCenter = (typeof ctx.map.getCenter === 'function') ? ctx.map.getCenter() : null;
                const distFromCur = curCenter ? Math.hypot(curCenter.lat - data.lat, curCenter.lng - data.lng) : Infinity;
                if (distFromCur > 250 || document.hidden || singleMonitorMode) {
                    ctx.map.setView([data.lat, data.lng], ctx.map.getZoom(), { animate: false });
                } else if (distFromCur > 4) {
                    ctx.map.panTo([data.lat, data.lng], { animate: true, duration: 0.16 });
                }
                lastSyncedView = {
                    lat: data.lat,
                    lng: data.lng,
                    zoom: ctx.map.getZoom(),
                    mapId: targetMapId
                };
                if (document.hidden) {
                    pendingVisibleRefresh = true;
                }
            }
        } catch (e) {}
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

    function performAutoSyncAndMark(data, markAfterSync = false, eventSeq = 0) {
        try {
            const targetMapId = data.map_id ? parseInt(data.map_id) : 2;
            const currentMapId = getCurrentWebMapId();

            if (data.has_world_coords && targetMapId !== currentMapId) {
                const mapName = data.map_name || `Map ${targetMapId}`;
                setLog(tr('page.sync.switching_map', { name: mapName }), '#38bdf8');
                if (markAfterSync && !singleMonitorMode) playBeep();

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
                    setLog(tr('page.sync.map_opened', { name: mapName }), '#22c55e');
                    executeSyncAndMarkOnContext(newCtx, data, markAfterSync, eventSeq);
                });
                return;
            }

            const ctx = findMapContext();
            executeSyncAndMarkOnContext(ctx, data, markAfterSync, eventSeq);
        } catch (e) {
            console.error('[Genshin Helper] Error in performAutoSyncAndMark:', e);
        }
    }

    function executeSyncAndMarkOnContext(ctx, data, markAfterSync = false, eventSeq = 0) {
        try {
            if (!ctx || !ctx.map) {
                setLog(tr('page.sync.map_not_found'), '#ef4444');
                if (markAfterSync) {
                    sendActionFeedback(
                        'SYNC_AND_MARK',
                        'error',
                        tr('page.sync.map_not_ready_title'),
                        tr('page.sync.map_not_ready_detail'),
                        eventSeq
                    );
                }
                return;
            }

            _ensureMarkRedrawHooks(ctx);
            bindDragUnpinListeners(ctx);

            // Step 1: Check if game sent real World Coordinates (lat, lng)
            const hasWorld = (data && data.has_world_coords && data.lat !== undefined && data.lng !== undefined);
            const targetLat = hasWorld ? data.lat : null;
            const targetLng = hasWorld ? data.lng : null;
            const targetZoom = computeTargetLeafletZoom(ctx.map, data);

            if (hasWorld) {
                const mapName = data.map_name ? ` (${data.map_name})` : '';
                if (markAfterSync) {
                    setLog(
                        tr('page.sync.panning_to', {
                            map: mapName,
                            lat: targetLat.toFixed(1),
                            lng: targetLng.toFixed(1)
                        }),
                        '#38bdf8'
                    );
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

                // Instant snap on teleports / zoom jumps / layer transitions / hidden tab / 1-monitor mode; smooth short pan only for visible nearby moves
                if (autoPanEnabled) {
                    const curCenter = (typeof ctx.map.getCenter === 'function') ? ctx.map.getCenter() : null;
                    const distFromCur = curCenter ? Math.hypot(curCenter.lat - targetLat, curCenter.lng - targetLng) : Infinity;
                    const currentZoom = ctx.map.getZoom();
                    const mustSnap = document.hidden || singleMonitorMode || needsLayerTransition || distFromCur > 250 || Math.abs(currentZoom - targetZoom) > 0.25;
                    if (mustSnap) {
                        ctx.map.setView([targetLat, targetLng], targetZoom, { animate: false });
                    } else {
                        ctx.map.panTo([targetLat, targetLng], { animate: true, duration: 0.22 });
                    }
                    lastSyncedView = {
                        lat: targetLat,
                        lng: targetLng,
                        zoom: targetZoom,
                        mapId: data.map_id ? parseInt(data.map_id, 10) : 2
                    };
                    if (document.hidden) {
                        pendingVisibleRefresh = true;
                    }
                }
                updatePlayerMarker(ctx, data);
                const resolvedSyncMapId = data.map_id ? parseInt(data.map_id, 10) : getCurrentWebMapId();
                const resolvedSyncFloorId = data.floor_id !== undefined ? data.floor_id : (data.floor_short || null);
                pushNearbyPinsToServer(targetLat, targetLng, resolvedSyncMapId, resolvedSyncFloorId, { force: true });

                // Handle automatic cave & multi-floor exploration (or returning to surface)
                if (autoSwitchLayerEnabled) {
                    handleLayeredExploration(ctx, data, targetZoom, areaChanged);
                }
                if (areaChanged || needsLayerTransition) {
                    setTimeout(() => {
                        pushNearbyPinsToServer(targetLat, targetLng, resolvedSyncMapId, resolvedSyncFloorId, { force: true });
                    }, 750);
                }
            } else {
                setLog(tr('page.sync.low_landmarks'), '#f59e0b');
                if (autoSwitchLayerEnabled) {
                    handleLayeredExploration(ctx, data, targetZoom);
                }
            }

            // Auto-sync on Map (M) only updates position/marker/layer — never opens popups or clicks "Ghim"
            if (!markAfterSync) {
                closeOpenPopup();
                if (hasWorld) {
                    const floorDesc = data.is_underground
                        ? ` [${data.hoyolab_floor_name || data.floor_short || data.floor_name || tr('page.layer.layered_label')}]`
                        : ` [${tr('page.layer.surface_label')}]`;
                    setLog(tr('page.sync.synced_pos', { floor: floorDesc }), '#22c55e');
                    const floorLabel = data.is_underground
                        ? (data.hoyolab_floor_name || data.floor_short || data.floor_name || tr('page.layer.layered_label'))
                        : (data.map_name || tr('page.layer.surface_label'));
                    const coordsDetail = (data.area_name ? `${data.area_name} • ` : '') + `(${targetLat.toFixed(0)}, ${targetLng.toFixed(0)})`;
                    sendActionFeedback('SYNC_POSITION_ONLY', 'synced', floorLabel, coordsDetail, eventSeq);
                }
                return;
            }

            // Step 2 (manual hotkey SYNC_AND_MARK): Wait for map animation to settle before querying collectibles
            const waitTime = hasWorld ? ((document.hidden || singleMonitorMode) ? 140 : 650) : 50;
            setTimeout(() => {
                let closest = null;
                let minDistance = Infinity;

                if (hasWorld) {
                    const radius = 60;
                    const indivMarkers = _collectIndividualMarkers(ctx);
                    for (let i = 0; i < indivMarkers.length; i++) {
                        const m = indivMarkers[i];
                        if (!m || _isNavLandmarkPin(null, m)) continue;
                        const latlng = (typeof m.getLatLng === 'function') ? m.getLatLng() : null;
                        if (!latlng || typeof latlng.lat !== 'number' || typeof latlng.lng !== 'number') continue;
                        const d = Math.hypot(latlng.lat - targetLat, latlng.lng - targetLng);
                        if (d < minDistance && d <= radius) {
                            minDistance = d;
                            const pt = (ctx.map && typeof ctx.map.latLngToContainerPoint === 'function') ? ctx.map.latLngToContainerPoint(latlng) : null;
                            closest = { marker: m, latlng, point: pt, rawItem: null };
                        }
                    }

                    const tree = ctx.ciLayer && ctx.ciLayer._latlngMarkers;
                    if (!closest && tree && typeof tree.search === 'function') {
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
                        setLog(tr('page.sync.no_nearby_toast'), '#22c55e');
                        sendActionFeedback('SYNC_AND_MARK', 'no_nearby', tr('page.sync.no_nearby_title'), '', eventSeq);
                        return;
                    }

                    setLog(tr('page.sync.nearest_opening', { dist: minDistance.toFixed(1) }), '#10b981');
                } else {
                    // Screen center fallback
                    const markers = getAllMarkers(ctx);
                    if (markers.length === 0) {
                        setLog(tr('page.sync.no_markers'), '#ef4444');
                        sendActionFeedback(
                            'SYNC_AND_MARK',
                            'no_nearby',
                            tr('page.sync.low_landmarks_title'),
                            tr('page.sync.low_landmarks_detail'),
                            eventSeq
                        );
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
                    setLog(tr('page.sync.no_match'), '#ef4444');
                    sendActionFeedback(
                        'SYNC_AND_MARK',
                        'no_nearby',
                        tr('page.sync.low_landmarks_title'),
                        tr('page.sync.low_landmarks_detail'),
                        eventSeq
                    );
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

                // Wait for popup and click "Ghim" (wall-clock deadline immune to hidden-tab timer throttling)
                let layerActivated = false;
                let resolved = false;
                const deadline = Date.now() + 2400;
                let checkInterval = null;

                const pollPopupTick = () => {
                    if (resolved) {
                        if (checkInterval) clearInterval(checkInterval);
                        return;
                    }

                    // If underground exploration button is inside popup, only click if elevator is not already open
                    if (autoSwitchLayerEnabled && data.is_underground && !layerActivated) {
                        const elevator = document.querySelector('#underground-elevator, .underground-elevator');
                        const isElevatorOpen = !!elevator && elevator.offsetWidth > 0;
                        if (!isElevatorOpen) {
                            const layerBtn = findLayerButton();
                            if (layerBtn) {
                                layerActivated = true;
                                setLog(tr('page.layer.activating'), '#38bdf8');
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
                        resolved = true;
                        if (checkInterval) clearInterval(checkInterval);
                        clickGhimAndNotify(ghimBtn, eventSeq, hasWorld ? minDistance : null, ctx, closest && closest.marker);
                        return;
                    }

                    const alreadyMarked = findAlreadyMarkedButton();
                    if (alreadyMarked) {
                        resolved = true;
                        if (checkInterval) clearInterval(checkInterval);
                        const itemTitle = getPopupTitle();
                        if (!singleMonitorMode) playBeep();
                        setLog(tr('page.sync.already_marked_toast'), '#fbbf24');
                        sendActionFeedback('SYNC_AND_MARK', 'already_marked', tr('page.sync.already_marked_title'), itemTitle, eventSeq);
                        return;
                    }

                    if (Date.now() >= deadline) {
                        resolved = true;
                        if (checkInterval) clearInterval(checkInterval);
                        setLog(tr('page.sync.no_mark_btn'), '#f59e0b');
                        sendActionFeedback('SYNC_AND_MARK', 'no_nearby', tr('page.sync.no_mark_btn'), '', eventSeq);
                    }
                };

                checkInterval = setInterval(pollPopupTick, 100);
                setTimeout(pollPopupTick, 30);

            }, waitTime);

        } catch (err) {
            console.error('[Genshin Helper Fatal Error]', err);
            setLog(tr('page.sync.error_toast', { error: err.message }), '#ef4444');
            if (markAfterSync) {
                sendActionFeedback('SYNC_AND_MARK', 'error', tr('page.sync.error_title'), err.message || '', eventSeq);
            }
        }
    }

    // =========================================================================
    // 5.1 In-game Mark / Unmark of the exact Mini-HUD target (no Alt+Tab)
    // =========================================================================
    function _sendNavMarkFeedback(action, status, title, seq, pinId = null) {
        apiRequest('/action_feedback', {
            method: 'POST',
            body: {
                event_type: 'NAV_MARK',
                action: action,
                status: status,
                title: title || '',
                detail: '',
                pin_id: (pinId !== null && pinId !== undefined) ? String(pinId) : '',
                seq: typeof seq === 'number' ? seq : 0
            }
        }).catch(() => {});
    }

    function _redrawCiLayer(ciLayer) {
        if (!ciLayer) return;
        try {
            if (typeof ciLayer.redraw === 'function') {
                ciLayer.redraw();
            } else if (typeof ciLayer._redraw === 'function') {
                ciLayer._redraw(true);
            } else if (typeof ciLayer.throttleRedraw === 'function') {
                ciLayer.throttleRedraw();
            }
        } catch (e) {}
    }

    function _getMarkedRecordFromStore(ctx, pinId) {
        if (pinId === null || pinId === undefined || pinId === '') return null;
        const want = String(pinId);
        try {
            const gisVm = resolveGisVm(ctx);
            const list = gisVm && gisVm.$store && gisVm.$store.state && gisVm.$store.state.markPointList;
            if (!Array.isArray(list)) return null;
            for (let i = 0; i < list.length; i++) {
                const row = list[i];
                if (row && String(row.point_id) === want) {
                    return row.id || true;
                }
            }
        } catch (e) {}
        return null;
    }

    function _syncMarkerVisualState(ctx, marker, isMarked, recordId = true, resetSelected = false) {
        if (!marker) return;
        try {
            if (!marker._map && ctx && ctx.map) {
                marker._map = ctx.map;
            }
            const gisVm = resolveGisVm(ctx);
            const markedOp = (
                gisVm && gisVm.$store && gisVm.$store.getters &&
                typeof gisVm.$store.getters.markedMarkerOpacity === 'number'
            ) ? gisVm.$store.getters.markedMarkerOpacity : 0.3;
            const desiredOp = isMarked ? markedOp : 1;
            const applyOpacity = (op) => {
                if (typeof marker.setOpacity === 'function') {
                    marker.setOpacity(op);
                } else if (marker.options) {
                    marker.options.opacity = op;
                }
            };

            if (typeof marker.setMarkedState === 'function') {
                marker.setMarkedState(isMarked ? 'marked' : 'default', isMarked ? (recordId || true) : false, true);
            }
            marker.marked = isMarked ? (recordId || true) : false;
            if (!marker.options || marker.options.opacity !== desiredOp) {
                applyOpacity(desiredOp);
            }
            if (resetSelected && typeof marker.setState === 'function') {
                marker.setState('default', undefined, false);
                if (!marker.options || marker.options.opacity !== desiredOp) {
                    applyOpacity(desiredOp);
                }
            }
        } catch (e) {}
    }

    function _extractStoreMarkMap(state) {
        const out = new Map();
        const list = state && state.markPointList;
        if (Array.isArray(list)) {
            for (let i = 0; i < list.length; i++) {
                const row = list[i];
                if (row && row.point_id !== undefined && row.point_id !== null) {
                    out.set(String(row.point_id), row.id || true);
                }
            }
        }
        return out;
    }

    function _collectAllCandidateMarkers(ctx) {
        const out = [];
        const seen = new Set();
        const add = (m) => {
            if (!m || typeof m !== 'object' || seen.has(m)) return;
            seen.add(m);
            out.push(m);
        };
        const indiv = _collectIndividualMarkers(ctx);
        for (let i = 0; i < indiv.length; i++) add(indiv[i]);
        if (ctx && Array.isArray(ctx.markerList)) {
            for (let i = 0; i < ctx.markerList.length; i++) {
                const it = ctx.markerList[i];
                if (it && it.marker) add(it.marker);
            }
        }
        if (ctx && ctx.ciLayer && ctx.ciLayer.contextMenuMarker) {
            add(ctx.ciLayer.contextMenuMarker);
        }
        return out;
    }

    function _reconcileMarkedVisuals(ctx, opts = {}) {
        try {
            const c = ctx || findMapContext();
            if (!c || !c.ciLayer) return 0;
            const gisVm = resolveGisVm(c);
            const store = gisVm && gisVm.$store;
            const markedMap = _extractStoreMarkMap(store && store.state);
            const cachedIds = cachedMarkedIdsByMap.get(getCurrentWebMapId());
            if (cachedIds) {
                for (const cid of cachedIds) {
                    const key = String(cid);
                    if (!markedMap.has(key)) markedMap.set(key, true);
                }
            }
            const unmarkedIds = opts && opts.unmarkedIds instanceof Set ? opts.unmarkedIds : null;
            const skipSelectedPopup = opts.skipSelectedPopup !== false;
            const keepSelected = Boolean(opts.keepSelected);
            const popupOpen = Boolean(findPopupContainer());
            const selectedMarker = c.ciLayer.contextMenuMarker || null;
            const pool = _collectAllCandidateMarkers(c);
            let fixed = 0;

            for (let i = 0; i < pool.length; i++) {
                const m = pool[i];
                const pidRaw = m.pointId ?? m.id ?? (m.attrs && m.attrs.marker && m.attrs.marker.id);
                if (pidRaw === undefined || pidRaw === null) continue;
                const pid = String(pidRaw);
                if (markedMap.has(pid)) {
                    if (skipSelectedPopup && popupOpen && m === selectedMarker) continue;
                    const op = m.options && typeof m.options.opacity === 'number' ? m.options.opacity : 1;
                    if (!m.marked || op >= 0.95) {
                        _syncMarkerVisualState(c, m, true, markedMap.get(pid), !keepSelected);
                        fixed += 1;
                    }
                } else if (unmarkedIds && unmarkedIds.has(pid)) {
                    const op = m.options && typeof m.options.opacity === 'number' ? m.options.opacity : 1;
                    if (m.marked || op < 0.95) {
                        _syncMarkerVisualState(c, m, false, false, true);
                        fixed += 1;
                    }
                }
            }

            if (fixed > 0 || opts.forceRedraw) {
                _redrawCiLayer(c.ciLayer);
            }
            return fixed;
        } catch (e) {
            return 0;
        }
    }

    function _ensureMarkRedrawHooks(ctx) {
        try {
            const c = ctx || findMapContext();
            if (!c) return;
            const gisVm = resolveGisVm(c);
            const store = gisVm && gisVm.$store;
            if (store && typeof store.subscribe === 'function' && !hookedMarkStores.has(store)) {
                hookedMarkStores.add(store);
                let prevMap = _extractStoreMarkMap(store.state);
                store.subscribe((mutation, state) => {
                    if (!mutation || mutation.type !== 'updateMarkPointList') return;
                    const mid = getCurrentWebMapId();
                    const nextMap = _extractStoreMarkMap(state);
                    cachedMarkedIdsByMap.set(mid, new Set(nextMap.keys()));
                    cachedMarkedFetchedAt.set(mid, Date.now());

                    const changed = new Map();
                    const unmarkedSet = new Set();
                    for (const [pid, recId] of nextMap.entries()) {
                        if (!prevMap.has(pid)) changed.set(pid, { marked: true, recId });
                    }
                    for (const pid of prevMap.keys()) {
                        if (!nextMap.has(pid)) {
                            changed.set(pid, { marked: false, recId: false });
                            unmarkedSet.add(pid);
                        }
                    }
                    prevMap = nextMap;

                    if (changed.size >= 1 && changed.size <= 50) {
                        const curCtx = findMapContext();
                        const ci = curCtx && curCtx.ciLayer;
                        const pool = _collectAllCandidateMarkers(curCtx);
                        for (let i = 0; i < pool.length; i++) {
                            const m = pool[i];
                            const pid = m.pointId ?? m.id ?? (m.attrs && m.attrs.marker && m.attrs.marker.id);
                            if (pid === undefined || pid === null) continue;
                            const info = changed.get(String(pid));
                            if (info) {
                                _syncMarkerVisualState(curCtx, m, info.marked, info.recId, false);
                            }
                        }
                        _redrawCiLayer(ci);
                        // Outlast HoYoLAB async popupopen y.getInfo() overwrite and 500ms destroy cleanup
                        for (const delayMs of [150, 650, 1300]) {
                            setTimeout(() => {
                                _reconcileMarkedVisuals(findMapContext(), {
                                    skipSelectedPopup: true,
                                    keepSelected: true,
                                    unmarkedIds: unmarkedSet,
                                    forceRedraw: delayMs === 150
                                });
                            }, delayMs);
                        }
                        if (playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                            pushNearbyPinsToServer(
                                playerLastState.lat,
                                playerLastState.lng,
                                mid,
                                playerLastState.floor_id !== undefined ? playerLastState.floor_id : (playerLastState.floor_short || null),
                                { force: true }
                            );
                        }
                    }
                });
            }

            if (c.map && typeof c.map.on === 'function' && !hookedPopupMaps.has(c.map)) {
                hookedPopupMaps.add(c.map);
                c.map.on('popupclose', () => {
                    try {
                        const curCtx = findMapContext();
                        const ci = curCtx && curCtx.ciLayer;
                        if (!ci) return;
                        const cm = ci.contextMenuMarker;
                        if (cm) {
                            const pid = cm.pointId ?? cm.id ?? (cm.attrs && cm.attrs.marker && cm.attrs.marker.id);
                            const storeRec = _getMarkedRecordFromStore(curCtx, pid);
                            const isMarked = Boolean(cm.marked || storeRec);
                            _syncMarkerVisualState(curCtx, cm, isMarked, storeRec || cm.marked || true, true);
                            ci.contextMenuMarker = null;
                        }
                        _reconcileMarkedVisuals(curCtx, { skipSelectedPopup: false, forceRedraw: true });
                        for (const delayMs of [120, 650]) {
                            setTimeout(() => {
                                _reconcileMarkedVisuals(findMapContext(), {
                                    skipSelectedPopup: false,
                                    forceRedraw: delayMs === 120
                                });
                            }, delayMs);
                        }
                    } catch (e) {}
                });
            }
        } catch (e) {}
    }

    function _findMarkerForPin(ctx, pinId, lat, lng) {
        const lookup = _getMarkerMetaLookup(ctx);
        const wantId = (pinId !== null && pinId !== undefined) ? String(pinId) : null;
        const radius = 6;
        let best = null;
        let bestDist = Infinity;
        const consider = (item, m, ll) => {
            if (!ll || typeof ll.lat !== 'number' || typeof ll.lng !== 'number') return false;
            const meta = _resolveRichPinItem(item, m, lookup);
            const d = Math.hypot(ll.lat - lat, ll.lng - lng);
            if (wantId !== null && meta.id !== null && meta.id !== undefined && String(meta.id) === wantId) {
                best = { marker: m, latlng: ll, rawItem: item };
                bestDist = -1;
                return true;
            }
            if (bestDist >= 0 && d < bestDist && d <= 2.5) {
                bestDist = d;
                best = { marker: m, latlng: ll, rawItem: item };
            }
            return false;
        };
        // Same source as the nearby feed: individual markers, including pins hidden in a cluster
        for (const m of _collectIndividualMarkers(ctx)) {
            const ll = (m && typeof m.getLatLng === 'function') ? m.getLatLng() : null;
            if (!ll || Math.abs(ll.lat - lat) > radius || Math.abs(ll.lng - lng) > radius) continue;
            if (consider(null, m, ll)) break;
        }
        if (best && ctx.map && typeof ctx.map.latLngToContainerPoint === 'function') {
            best.point = ctx.map.latLngToContainerPoint(best.latlng);
        }
        return best;
    }

    function _waitUntil(cond, timeoutMs) {
        return new Promise((resolve) => {
            const end = Date.now() + timeoutMs;
            const step = () => {
                let ok = false;
                try { ok = !!cond(); } catch (e) { ok = false; }
                if (ok || Date.now() >= end) { resolve(ok); return; }
                setTimeout(step, 50);
            };
            step();
        });
    }

    async function _openPinPopup(ctx, found) {
        const map = ctx.map;
        const marker = found.marker;
        let restoreView = null;
        if (marker && !marker._map && typeof map.setView === 'function') {
            // Pin is inside a cluster at this zoom: bring it out (view is restored afterwards)
            restoreView = { center: map.getCenter(), zoom: map.getZoom() };
            const maxZ = typeof map.getMaxZoom === 'function' ? map.getMaxZoom() : 4;
            const zoom = Math.max(map.getZoom(), (Number.isFinite(maxZ) ? maxZ : 4) - 1);
            map.setView(found.latlng, zoom, { animate: false });
            await _waitUntil(() => !!marker._map, 1500);
        }
        const point = map.latLngToContainerPoint(found.latlng);
        const evt = {
            type: 'click',
            latlng: found.latlng,
            containerPoint: point,
            layerPoint: typeof map.containerPointToLayerPoint === 'function' ? map.containerPointToLayerPoint(point) : point,
            originalEvent: new MouseEvent('click'),
            target: map
        };
        let opened = false;
        if (marker && ctx.ciLayer && typeof ctx.ciLayer.onMarkerClick === 'function') {
            try { ctx.ciLayer.onMarkerClick(evt, [{ data: marker }]); opened = true; } catch (e) { opened = false; }
        }
        if (!opened && marker && typeof marker.openPopup === 'function') {
            try { marker.openPopup(); opened = true; } catch (e) { opened = false; }
        }
        if (!opened && ctx.ciLayer && typeof ctx.ciLayer._executeListeners === 'function') {
            try { ctx.ciLayer._executeListeners(evt); } catch (e) { /* fall through to the poll timeout */ }
        }
        return restoreView;
    }

    async function handleNavMarkPin(data) {
        const task = navMarkChain.then(async () => {
            const action = (data && data.action === 'unmark') ? 'unmark' : 'mark';
            const seq = typeof data._seq === 'number' ? data._seq : 0;
            const pinId = (data && data.pin_id !== undefined && data.pin_id !== null) ? String(data.pin_id) : '';
            const name = (data && data.name) || tr('page.sync.default_pin_title');
            let restoreView = null;
            let ctx = null;
            try {
                const lat = Number(data.lat);
                const lng = Number(data.lng);
                ctx = findMapContext();
                const targetMapId = data.map_id ? parseInt(data.map_id, 10) : 2;
                if (!ctx || !ctx.map || !Number.isFinite(lat) || !Number.isFinite(lng) || targetMapId !== getCurrentWebMapId()) {
                    _sendNavMarkFeedback(action, 'error', name, seq, pinId);
                    return;
                }
                _ensureMarkRedrawHooks(ctx);
                const found = _findMarkerForPin(ctx, data.pin_id, lat, lng);
                if (!found) {
                    setLog(tr('page.nav.pin_not_found', { name }), '#f59e0b');
                    _sendNavMarkFeedback(action, 'not_found', name, seq, pinId);
                    return;
                }
                const markerPid = found.marker && (found.marker.pointId ?? found.marker.id);
                const targetPid = pinId || (markerPid !== undefined && markerPid !== null ? String(markerPid) : '');
                const storeRec = _getMarkedRecordFromStore(ctx, targetPid);
                if (action === 'mark' && ((found.marker && found.marker.marked) || storeRec)) {
                    _syncMarkerVisualState(ctx, found.marker, true, storeRec || (found.marker && found.marker.marked) || true, true);
                    _redrawCiLayer(ctx.ciLayer);
                    _sendNavMarkFeedback(action, 'already_marked', name, seq, targetPid);
                    if (playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                        pushNearbyPinsToServer(
                            playerLastState.lat,
                            playerLastState.lng,
                            targetMapId,
                            playerLastState.floor_id !== undefined ? playerLastState.floor_id : (playerLastState.floor_short || null),
                            { force: true }
                        );
                    }
                    return;
                }

                closeOpenPopup();
                restoreView = await _openPinPopup(ctx, found);

                await _waitUntil(() => Boolean(findGhimButton() || findAlreadyMarkedButton()), 2600);
                const markBtn = findGhimButton();
                const unmarkBtn = findAlreadyMarkedButton();

                let status = null;
                let actionBtn = null;
                if (action === 'mark') {
                    if (markBtn) {
                        actionBtn = markBtn;
                    } else if (unmarkBtn) {
                        status = 'already_marked';
                    }
                } else {
                    if (unmarkBtn && /bỏ|unmark/i.test(getText(unmarkBtn))) {
                        actionBtn = unmarkBtn;
                    } else if (markBtn) {
                        status = 'already_unmarked';
                    }
                }

                if (actionBtn) {
                    actionBtn.click();
                    const confirmed = await _waitUntil(() => {
                        const storeHas = Boolean(_getMarkedRecordFromStore(ctx, targetPid));
                        const markerHas = Boolean(found.marker && found.marker.marked);
                        if (action === 'mark') {
                            return Boolean(findAlreadyMarkedButton()) || markerHas || storeHas;
                        }
                        return Boolean(findGhimButton()) || (!markerHas && !storeHas);
                    }, 3500);
                    if (confirmed) {
                        status = (action === 'mark') ? 'marked' : 'unmarked';
                    } else {
                        setLog(tr('page.sync.no_mark_btn'), '#f59e0b');
                        status = 'error';
                    }
                } else if (!status) {
                    setLog(tr('page.sync.no_mark_btn'), '#f59e0b');
                    status = 'error';
                }

                if (status === 'marked' || status === 'already_marked') {
                    const rec = _getMarkedRecordFromStore(ctx, targetPid) || (found.marker && found.marker.marked) || true;
                    _syncMarkerVisualState(ctx, found.marker, true, rec, true);
                    if (ctx.ciLayer && ctx.ciLayer.contextMenuMarker === found.marker) {
                        ctx.ciLayer.contextMenuMarker = null;
                    }
                    _redrawCiLayer(ctx.ciLayer);
                } else if (status === 'unmarked' || status === 'already_unmarked') {
                    _syncMarkerVisualState(ctx, found.marker, false, false, true);
                    if (ctx.ciLayer && ctx.ciLayer.contextMenuMarker === found.marker) {
                        ctx.ciLayer.contextMenuMarker = null;
                    }
                    _redrawCiLayer(ctx.ciLayer);
                }

                if (status === 'marked' || status === 'unmarked') {
                    setLog(tr(action === 'mark' ? 'page.nav.marked' : 'page.nav.unmarked', { name }), '#22c55e');
                }
                _sendNavMarkFeedback(action, status, name, seq, targetPid || pinId);

                closeOpenPopup();
                if (restoreView && ctx.map && typeof ctx.map.setView === 'function') {
                    try { ctx.map.setView(restoreView.center, restoreView.zoom, { animate: false }); } catch (e) { /* ignore */ }
                    restoreView = null;
                }
                _redrawCiLayer(ctx.ciLayer);
                const unmarkedSet = (action === 'unmark' && targetPid) ? new Set([targetPid]) : null;
                for (const delayMs of [150, 700]) {
                    setTimeout(() => {
                        const curCtx = findMapContext();
                        if (found && found.marker && (status === 'marked' || status === 'unmarked')) {
                            const isM = status === 'marked';
                            _syncMarkerVisualState(curCtx || ctx, found.marker, isM, (found.marker && found.marker.marked) || isM, true);
                        }
                        _reconcileMarkedVisuals(curCtx || ctx, {
                            skipSelectedPopup: false,
                            unmarkedIds: unmarkedSet,
                            forceRedraw: delayMs === 150
                        });
                    }, delayMs);
                }

                const mid = getCurrentWebMapId();
                _refreshMarkedPinIds(mid, true).finally(() => {
                    if (playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                        pushNearbyPinsToServer(
                            playerLastState.lat,
                            playerLastState.lng,
                            mid,
                            playerLastState.floor_id !== undefined ? playerLastState.floor_id : (playerLastState.floor_short || null),
                            { force: true }
                        );
                    }
                });
            } catch (e) {
                console.error('[Genshin Helper] handleNavMarkPin error:', e);
                if (restoreView && ctx && ctx.map && typeof ctx.map.setView === 'function') {
                    try { ctx.map.setView(restoreView.center, restoreView.zoom, { animate: false }); } catch (err) { /* ignore */ }
                }
                _sendNavMarkFeedback(action, 'error', name, seq, pinId);
            }
        });
        navMarkChain = task.catch(() => {});
        return task;
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

    function getPopupTitle() {
        let itemTitle = tr('page.sync.default_pin_title');
        const popup = findPopupContainer();
        if (popup) {
            const titleEl = popup.querySelector('.leaflet-popup-content h3, [class*="title"], h3, h4');
            if (titleEl) {
                const txt = getText(titleEl);
                if (txt) itemTitle = txt;
            }
        }
        return itemTitle;
    }

    function clickGhimAndNotify(btn, seq = 0, distM = null, ctx = null, marker = null) {
        const itemTitle = getPopupTitle();
        btn.click();
        if (!singleMonitorMode) playBeep();
        setLog(tr('page.sync.marked_toast', { title: itemTitle }), '#22c55e');
        const distStr = (typeof distM === 'number' && isFinite(distM))
            ? tr('page.sync.marked_dist', { dist: distM.toFixed(1) })
            : '';
        sendActionFeedback('SYNC_AND_MARK', 'marked', tr('page.sync.marked_title', { title: itemTitle }), distStr, seq);
        _waitUntil(() => Boolean(findAlreadyMarkedButton()) || Boolean(marker && marker.marked), 3000)
            .then((ok) => {
                const curCtx = ctx || findMapContext();
                const targetMarker = marker || (curCtx && curCtx.ciLayer && curCtx.ciLayer.contextMenuMarker) || null;
                if (ok && targetMarker) {
                    _syncMarkerVisualState(curCtx, targetMarker, true, targetMarker.marked || true, true);
                }
                _reconcileMarkedVisuals(curCtx, { skipSelectedPopup: false, forceRedraw: true });
                for (const delayMs of [150, 700]) {
                    setTimeout(() => {
                        const latestCtx = findMapContext();
                        if (targetMarker) {
                            _syncMarkerVisualState(latestCtx, targetMarker, true, targetMarker.marked || true, true);
                        }
                        _reconcileMarkedVisuals(latestCtx, { skipSelectedPopup: false, forceRedraw: delayMs === 150 });
                    }, delayMs);
                }
                const mid = (playerLastState && playerLastState.map_id) || getCurrentWebMapId();
                return _refreshMarkedPinIds(mid, true).finally(() => {
                    if (playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                        pushNearbyPinsToServer(
                            playerLastState.lat,
                            playerLastState.lng,
                            mid,
                            playerLastState.floor_id !== undefined ? playerLastState.floor_id : (playerLastState.floor_short || null),
                            { force: true }
                        );
                    }
                });
            })
            .catch(() => {});
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

    // =========================================================================
    // 7. Wave 4 Integration Bridge (window.__GH_BRIDGE__)
    // =========================================================================
    const NAV_EXCLUDED_PARENT_LABELS = new Set([1, 426]);

    function _markerLabelConfig(markerObj) {
        const cfg = markerObj && markerObj.attrs && markerObj.attrs.config;
        return (cfg && typeof cfg === 'object') ? cfg : null;
    }

    function _isNavLandmarkPin(item, markerObj) {
        if (!item && !markerObj) return false;
        const cfg = _markerLabelConfig(markerObj);
        const parentId = Number(
            (cfg && cfg.parent_id) ?? (markerObj && markerObj.parentLabelId) ?? NaN
        );
        if (Number.isFinite(parentId) && NAV_EXCLUDED_PARENT_LABELS.has(parentId)) {
            return true;
        }
        const name = (
            (item && item.name) ||
            (item && item.rawItem && item.rawItem.name) ||
            (markerObj && markerObj.options && markerObj.options.title) ||
            ''
        ).toLowerCase();
        if (
            name.includes('dịch chuyển') ||
            name.includes('dich chuyen') ||
            name.includes('teleport') ||
            name.includes('thần tượng') ||
            name.includes('than tuong') ||
            name.includes('statue')
        ) {
            return true;
        }
        const lid = Number(
            (item && (item.label_id || item.labelId || (item.rawItem && item.rawItem.label_id))) ||
            (markerObj && (markerObj.label_id || (markerObj.options && markerObj.options.label_id))) ||
            0
        );
        return lid === 2 || lid === 3 || lid === 154;
    }

    function _extractMarkedIdsFromEnvelope(env) {
        const ids = new Set();
        const addItem = (entry) => {
            if (entry === null || entry === undefined) return;
            if (typeof entry === 'number' || typeof entry === 'string') {
                const s = String(entry).trim();
                if (s) ids.add(s);
                return;
            }
            if (typeof entry === 'object') {
                const rawId = entry.point_id ?? entry.map_point_id ?? entry.id ?? entry.pointId;
                if (rawId !== undefined && rawId !== null && String(rawId).trim() !== '') {
                    ids.add(String(rawId).trim());
                }
            }
        };
        try {
            const root = (env && env.data && typeof env.data === 'object') ? env.data : env;
            if (!root || typeof root !== 'object') return ids;
            const lists = [
                root.list,
                root.map_point_list,
                root.marked_list,
                root.point_list,
                root.point_ids,
                root.marked_ids,
                root.ids,
                root.data
            ];
            for (const candidate of lists) {
                if (Array.isArray(candidate)) {
                    for (const item of candidate) addItem(item);
                } else if (candidate && typeof candidate === 'object') {
                    for (const [k, v] of Object.entries(candidate)) {
                        if (Array.isArray(v)) {
                            for (const item of v) addItem(item);
                        } else if (v === true || v === 1) {
                            addItem(k);
                        }
                    }
                }
            }
        } catch (e) {}
        return ids;
    }

    function _refreshMarkedPinIds(mapId, force = false) {
        const mid = (mapId !== null && mapId !== undefined && !isNaN(Number(mapId)))
            ? parseInt(mapId, 10)
            : getCurrentWebMapId();
        const now = Date.now();
        const lastTs = cachedMarkedFetchedAt.get(mid) || 0;
        if (!force && cachedMarkedIdsByMap.has(mid) && (now - lastTs) < 45000) {
            return Promise.resolve(cachedMarkedIdsByMap.get(mid));
        }
        if (markedFetchInFlight.has(mid)) {
            return markedFetchInFlight.get(mid);
        }
        const apiLang = (window.GHI18n && window.GHI18n.getLang() === 'vi') ? 'vi-vn' : 'en-us';
        const url = `https://sg-public-api.hoyolab.com/common/map_user/ys_obc/v1/map/mark_map_point_list?map_id=${encodeURIComponent(mid)}&app_sn=ys_obc&lang=${encodeURIComponent(apiLang)}`;
        const p = _fetchHoyolabOriginJson(url)
            .then((env) => {
                if (env && (env.retcode === 0 || env.retcode === undefined)) {
                    const prevSize = cachedMarkedIdsByMap.has(mid) ? cachedMarkedIdsByMap.get(mid).size : -1;
                    const ids = _extractMarkedIdsFromEnvelope(env);
                    cachedMarkedIdsByMap.set(mid, ids);
                    cachedMarkedFetchedAt.set(mid, Date.now());
                    if (ids.size !== prevSize && playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                        pushNearbyPinsToServer(
                            playerLastState.lat,
                            playerLastState.lng,
                            mid,
                            playerLastState.floor_id !== undefined ? playerLastState.floor_id : (playerLastState.floor_short || null),
                            { force: true }
                        );
                    }
                    return ids;
                }
                return cachedMarkedIdsByMap.get(mid) || new Set();
            })
            .catch(() => cachedMarkedIdsByMap.get(mid) || new Set())
            .finally(() => {
                markedFetchInFlight.delete(mid);
            });
        markedFetchInFlight.set(mid, p);
        return p;
    }

    function _collectMarkedPinIds(ctx) {
        const markedSet = new Set();
        const curMapId = getCurrentWebMapId();
        const cached = cachedMarkedIdsByMap.get(curMapId);
        if (cached) {
            for (const id of cached) markedSet.add(id);
        }
        const ingest = (src) => {
            if (!src) return;
            if (Array.isArray(src)) {
                for (const item of src) {
                    if (typeof item === 'string' || typeof item === 'number') {
                        markedSet.add(String(item));
                    } else if (item && typeof item === 'object') {
                        const id = item.point_id ?? item.map_point_id ?? item.id;
                        if (id !== undefined && id !== null) markedSet.add(String(id));
                    }
                }
            } else if (src instanceof Set) {
                for (const id of src) markedSet.add(String(id));
            } else if (src instanceof Map) {
                for (const [k, v] of src.entries()) {
                    if (v) markedSet.add(String(k));
                }
            } else if (typeof src === 'object') {
                for (const [k, v] of Object.entries(src)) {
                    if (v === true || v === 1 || (typeof v === 'object' && v !== null && (v.is_marked || v.marked || v.point_id))) {
                        markedSet.add(String(k));
                    }
                }
            }
        };
        try {
            const gisVm = resolveGisVm(ctx);
            const st = gisVm && gisVm.$store && gisVm.$store.state ? gisVm.$store.state : null;
            if (st && typeof st === 'object') {
                const scanObj = (obj, depth) => {
                    if (!obj || typeof obj !== 'object' || depth > 2) return;
                    for (const [k, v] of Object.entries(obj)) {
                        if (!v) continue;
                        if (/mark|sign|collect/i.test(k)) {
                            ingest(v);
                        } else if (depth < 2 && typeof v === 'object' && !Array.isArray(v) && !(v instanceof Set) && !(v instanceof Map)) {
                            scanObj(v, depth + 1);
                        }
                    }
                };
                scanObj(st, 0);
            }
        } catch (e) {}
        return markedSet;
    }

    function _isPinMarked(item, markerObj, markedSet) {
        const raw = (item && item.rawItem) || item || {};
        const opts = (markerObj && markerObj.options) || {};
        const iconOpts = (opts.icon && opts.icon.options) || {};
        if (
            Boolean(raw.is_marked) ||
            Boolean(raw.marked) ||
            Boolean(raw.is_sign) ||
            Boolean(raw.sign) ||
            Boolean(raw.collected) ||
            Boolean(raw.checked) ||
            Number(raw.mark_status) === 1 ||
            Number(raw.status) === 1 ||
            Boolean(opts.isMarked) ||
            Boolean(opts.is_marked) ||
            Boolean(opts.marked) ||
            Boolean(markerObj && (markerObj.marked || markerObj.isMarked || markerObj.is_marked))
        ) {
            return true;
        }
        // Visual dimming check: HoYoLAB sets reduced opacity (< 0.95) or marked class on collected markers
        if (typeof opts.opacity === 'number' && opts.opacity < 0.95) {
            return true;
        }
        if (typeof raw.opacity === 'number' && raw.opacity < 0.95) {
            return true;
        }
        const clsStr = String(iconOpts.className || (markerObj && markerObj._icon && markerObj._icon.className) || '');
        if (/marked|signed|checked|collected/i.test(clsStr)) {
            return true;
        }
        if (markerObj && markerObj._icon && markerObj._icon.style && markerObj._icon.style.opacity) {
            const domOp = parseFloat(markerObj._icon.style.opacity);
            if (Number.isFinite(domOp) && domOp < 0.95) {
                return true;
            }
        }
        const id = raw.id ?? raw.point_id ?? opts.id ?? opts.point_id ?? (markerObj && (markerObj.pointId ?? markerObj.id));
        if (id !== undefined && id !== null && markedSet.has(String(id))) {
            return true;
        }
        return false;
    }

    function _getMarkerMetaLookup(ctx) {
        const list = ctx && Array.isArray(ctx.markerList) ? ctx.markerList : null;
        if (!list || list.length === 0) {
            return { byMarker: null, byId: null };
        }
        if (cachedMarkerIndexSrc === list && cachedMarkerIndexLen === list.length && cachedMarkerByMarker && cachedMarkerById) {
            return { byMarker: cachedMarkerByMarker, byId: cachedMarkerById };
        }
        const byMarker = new WeakMap();
        const byId = new Map();
        for (let i = 0; i < list.length; i++) {
            const it = list[i];
            if (!it || typeof it !== 'object') continue;
            if (it.marker && typeof it.marker === 'object') {
                byMarker.set(it.marker, it);
            }
            const rawId = it.id ?? it.point_id;
            if (rawId !== undefined && rawId !== null) {
                byId.set(String(rawId), it);
            }
        }
        cachedMarkerIndexSrc = list;
        cachedMarkerIndexLen = list.length;
        cachedMarkerByMarker = byMarker;
        cachedMarkerById = byId;
        return { byMarker, byId };
    }

    function _resolveFloorNameFromId(floorId) {
        if (floorId === null || floorId === undefined) return '';
        const fidStr = String(floorId).trim();
        if (!fidStr || fidStr === '0') return '';
        const gisEl = document.querySelector('.mhy-game-gis');
        const vm = gisEl && gisEl.__vue__;
        const list = (vm && Array.isArray(vm.pointGroupList) && vm.pointGroupList.length > 0)
            ? vm.pointGroupList
            : (vm && vm.$store && vm.$store.state && Array.isArray(vm.$store.state.pointGroupList)
                ? vm.$store.state.pointGroupList
                : null);
        if (!list || list.length === 0) return '';
        if (cachedFloorGroupListSrc !== list || cachedFloorGroupListLen !== list.length || !cachedFloorLabelMap) {
            const fmap = new Map();
            for (const g of list) {
                const floors = g && Array.isArray(g.floors) ? g.floors : [];
                for (let idx = 0; idx < floors.length; idx++) {
                    const fl = floors[idx];
                    if (!fl || fl.id === undefined || fl.id === null) continue;
                    const lbl = String(fl.floor_name_short || fl.floor_short || fl.floor_name || `B${idx + 1}`).trim();
                    if (lbl) fmap.set(String(fl.id).trim(), lbl);
                }
            }
            cachedFloorGroupListSrc = list;
            cachedFloorGroupListLen = list.length;
            cachedFloorLabelMap = fmap;
        }
        return cachedFloorLabelMap.get(fidStr) || '';
    }

    function _labelCacheKey(mapId) {
        const lang = (window.GHI18n && window.GHI18n.getLang() === 'vi') ? 'vi-vn' : 'en-us';
        return `${mapId}|${lang}`;
    }

    function _ensureLabelNames(mapId) {
        const mid = (mapId !== null && mapId !== undefined && !isNaN(Number(mapId)))
            ? parseInt(mapId, 10)
            : getCurrentWebMapId();
        const key = _labelCacheKey(mid);
        if (labelNamesByKey.has(key)) return Promise.resolve(labelNamesByKey.get(key));
        if (labelFetchInFlight.has(key)) return labelFetchInFlight.get(key);
        const apiLang = key.split('|')[1];
        const url = `https://sg-public-api.hoyolab.com/common/map_user/ys_obc/v1/map/label/tree?map_id=${encodeURIComponent(mid)}&app_sn=ys_obc&lang=${encodeURIComponent(apiLang)}`;
        const p = _fetchHoyolabOriginJson(url)
            .then((env) => {
                const names = new Map();
                const parents = new Map();
                const walk = (arr, parentId = null) => {
                    if (!Array.isArray(arr)) return;
                    for (const l of arr) {
                        if (!l || typeof l !== 'object') continue;
                        if (l.id !== undefined && l.id !== null) {
                            if (l.name) {
                                names.set(String(l.id), String(l.name).trim());
                            }
                            if (parentId !== null && parentId !== undefined) {
                                parents.set(String(l.id), Number(parentId));
                            }
                        }
                        walk(l.children, (l.id !== undefined && l.id !== null) ? l.id : parentId);
                    }
                };
                walk(env && env.data && env.data.tree, null);
                if (names.size > 0) {
                    labelNamesByKey.set(key, names);
                    labelParentByKey.set(key, parents);
                    // Re-push so the in-game HUD swaps generic titles for real category names
                    if (playerLastState && typeof playerLastState.lat === 'number' && typeof playerLastState.lng === 'number') {
                        pushNearbyPinsToServer(
                            playerLastState.lat,
                            playerLastState.lng,
                            mid,
                            playerLastState.floor_id !== undefined ? playerLastState.floor_id : (playerLastState.floor_short || null),
                            { force: true }
                        );
                    }
                }
                return names;
            })
            .catch(() => new Map())
            .finally(() => { labelFetchInFlight.delete(key); });
        labelFetchInFlight.set(key, p);
        return p;
    }

    function _labelNameFor(labelId, mapId = null) {
        if (labelId === null || labelId === undefined) return '';
        const mid = (mapId !== null && mapId !== undefined && !isNaN(Number(mapId))) ? parseInt(mapId, 10) : getCurrentWebMapId();
        const names = labelNamesByKey.get(_labelCacheKey(mid));
        if (!names) {
            _ensureLabelNames(mid);
            return '';
        }
        return names.get(String(labelId)) || '';
    }

    function _labelParentFor(labelId, mapId = null) {
        if (labelId === null || labelId === undefined) return null;
        const mid = (mapId !== null && mapId !== undefined && !isNaN(Number(mapId))) ? parseInt(mapId, 10) : getCurrentWebMapId();
        const parents = labelParentByKey.get(_labelCacheKey(mid));
        if (!parents) {
            _ensureLabelNames(mid);
            return null;
        }
        const pid = parents.get(String(labelId));
        return (pid !== undefined && pid !== null && Number.isFinite(Number(pid))) ? Number(pid) : null;
    }

    function _resolveRichPinItem(item, markerObj, lookup) {
        const attrMarker = (markerObj && markerObj.attrs && markerObj.attrs.marker && typeof markerObj.attrs.marker === 'object')
            ? markerObj.attrs.marker
            : null;
        const baseRaw = (item && item.rawItem) || (item && Object.keys(item).length ? item : null) || attrMarker || {};
        const cfg = _markerLabelConfig(markerObj);
        const opts = (markerObj && markerObj.options) || {};
        let enriched = null;
        if (lookup) {
            if (lookup.byMarker && markerObj && typeof markerObj === 'object') {
                enriched = lookup.byMarker.get(markerObj) || null;
            }
            if (!enriched && lookup.byId) {
                const candId = baseRaw.id ?? baseRaw.point_id ?? opts.id ?? opts.point_id ?? (markerObj && (markerObj.pointId ?? markerObj.id));
                if (candId !== undefined && candId !== null) {
                    enriched = lookup.byId.get(String(candId)) || null;
                }
            }
        }
        const raw = enriched ? Object.assign({}, baseRaw, enriched) : baseRaw;
        const id = raw.id ?? raw.point_id ?? opts.id ?? opts.point_id ?? (markerObj && (markerObj.pointId ?? markerObj.id)) ?? null;
        const labelId = raw.label_id ?? raw.labelId ?? opts.label_id ?? opts.labelId ?? (markerObj && markerObj.label_id) ?? (cfg && cfg.id) ?? null;
        const pg = (raw.point_group && typeof raw.point_group === 'object') ? raw.point_group : null;
        const floorId = raw.floor_id ?? raw.floorId ?? (pg && pg.floor_id) ?? opts.floor_id ?? opts.floorId ?? (markerObj && markerObj.floor_id) ?? null;
        let floorName = String(raw.floor_name || raw.floor_name_short || opts.floor_name || '').trim();
        if (!floorName && floorId !== null && floorId !== undefined) {
            floorName = _resolveFloorNameFromId(floorId);
        }
        const isUnderground = (typeof raw.is_underground === 'boolean')
            ? raw.is_underground
            : ((typeof opts.is_underground === 'boolean')
                ? opts.is_underground
                : Boolean(
                    (floorId !== null && floorId !== undefined && String(floorId).trim() !== '' && String(floorId) !== '0') ||
                    (pg && (pg.group_id || pg.floor_id)) ||
                    Number(raw.z_level || 0) !== 0 ||
                    [1, 2, 3].includes(Number(raw.icon_sign || 0))
                ));
        const infoObj = (raw.info && typeof raw.info === 'object') ? raw.info : null;
        const imageUrl = String(
            raw.image_url || raw.imageUrl || raw.picture || raw.img ||
            (infoObj && (infoObj.img || infoObj.picture || infoObj.image_url)) ||
            (Array.isArray(raw.picture_list) && raw.picture_list[0]) ||
            opts.image_url || opts.picture || opts.img || ''
        ).trim();
        const description = String(
            raw.description || raw.content || raw.desc ||
            (infoObj && (infoObj.content || infoObj.description || infoObj.desc)) ||
            opts.description || opts.content || opts.desc || ''
        ).trim();
        const rawParentLid = raw.parent_label_id ?? raw.parentLabelId ?? (cfg && cfg.parent_id) ?? (markerObj && markerObj.parentLabelId) ?? _labelParentFor(labelId);
        const parentLabelId = (rawParentLid !== null && rawParentLid !== undefined && rawParentLid !== '' && Number.isFinite(Number(rawParentLid)))
            ? Number(rawParentLid)
            : null;
        const labelName = String(raw.label_name || raw.labelName || (cfg && cfg.name) || _labelNameFor(labelId) || '').trim();
        const name = String(raw.name || raw.title || opts.title || opts.name || labelName || tr('page.sync.default_pin_title')).trim();
        return {
            raw,
            id,
            name,
            labelName,
            labelId,
            parentLabelId,
            floorId,
            floorName,
            isUnderground,
            imageUrl,
            description
        };
    }

    // Individual pin markers of the categories currently enabled on the web map.
    // Cluster entries are expanded through their MarkerClusterGroup (`_topClusterLevel`), so the
    // result is identical at every zoom level. The always-on landmark list (`markerList`) is
    // intentionally NOT used: it only holds teleports / domains / NPC spots.
    function _collectIndividualMarkers(ctx) {
        const out = [];
        const tree = ctx && ctx.ciLayer && ctx.ciLayer._latlngMarkers;
        if (!tree || typeof tree.all !== 'function') return out;
        const entries = tree.all();
        const groups = new Set();
        const seen = new Set();
        const addMarker = (m) => {
            if (!m || seen.has(m)) return;
            seen.add(m);
            out.push(m);
        };
        for (const entry of entries) {
            const m = entry && (entry.data || entry);
            if (!m) continue;
            const g = m._group || (m.__parent && m.__parent._group) || null;
            if (g && g._topClusterLevel && typeof g._topClusterLevel.getAllChildMarkers === 'function') {
                groups.add(g);
                continue;
            }
            if (m._childCount !== undefined && typeof m.getAllChildMarkers === 'function') {
                for (const child of m.getAllChildMarkers()) addMarker(child);
            } else {
                addMarker(m);
            }
        }
        for (const g of groups) {
            for (const child of g._topClusterLevel.getAllChildMarkers()) addMarker(child);
        }
        return out;
    }

    function queryNearbyUncollectedPins(lat, lng, radius = 150, limit = 25) {
        try {
            if (typeof lat !== 'number' || !Number.isFinite(lat) || typeof lng !== 'number' || !Number.isFinite(lng)) {
                return [];
            }
            const r = (typeof radius === 'number' && radius > 0) ? radius : 150;
            const maxItems = (typeof limit === 'number' && limit > 0) ? Math.floor(limit) : 25;
            const ctx = findMapContext();
            if (!ctx) return [];

            const markedSet = _collectMarkedPinIds(ctx);
            const lookup = _getMarkerMetaLookup(ctx);
            const results = [];
            const seenKeys = new Set();

            const pushPin = (item, markerObj, pinLat, pinLng) => {
                if (typeof pinLat !== 'number' || typeof pinLng !== 'number') return;
                if (!Number.isFinite(pinLat) || !Number.isFinite(pinLng)) return;

                const meta = _resolveRichPinItem(item, markerObj, lookup);
                if (_isNavLandmarkPin(meta.raw, markerObj) || _isNavLandmarkPin(item, markerObj)) return;
                if (_isPinMarked(meta.raw, markerObj, markedSet) || _isPinMarked(item, markerObj, markedSet)) return;

                const dLat = pinLat - lat;
                const dLng = pinLng - lng;
                const dist = Math.hypot(dLat, dLng);
                if (dist > r) return;

                const dedupKey = meta.id !== null ? `id:${meta.id}` : `${pinLat.toFixed(2)},${pinLng.toFixed(2)}`;
                if (seenKeys.has(dedupKey)) return;
                seenKeys.add(dedupKey);

                // Compass bearing in degrees (HoYoLAB CRS: -lat = North, +lng = East; 0° = North clockwise)
                const bearingRad = Math.atan2(dLng, -dLat);
                const bearingDeg = ((bearingRad * 180 / Math.PI) + 360) % 360;

                results.push({
                    id: meta.id,
                    labelId: meta.labelId,
                    label_id: meta.labelId,
                    parentLabelId: meta.parentLabelId,
                    parent_label_id: meta.parentLabelId,
                    name: meta.name,
                    label_name: meta.labelName || '',
                    lat: pinLat,
                    lng: pinLng,
                    distance: Number(dist.toFixed(2)),
                    bearing: Number(bearingDeg.toFixed(1)),
                    floorId: meta.floorId,
                    floor_id: meta.floorId,
                    floorName: meta.floorName || null,
                    floor_name: meta.floorName,
                    is_underground: meta.isUnderground,
                    image_url: meta.imageUrl,
                    description: meta.description
                });
            };

            // HoYoLAB clusters pins (MarkerCluster): the icon-layer tree holds CLUSTERS at low zoom,
            // whose centroid is not a real pin. Read the individual markers from the cluster group.
            const individual = _collectIndividualMarkers(ctx);
            for (const m of individual) {
                const ll = (m && typeof m.getLatLng === 'function') ? m.getLatLng() : null;
                if (!ll) continue;
                if (Math.abs(ll.lat - lat) > r || Math.abs(ll.lng - lng) > r) continue;
                pushPin(null, m, ll.lat, ll.lng);
            }

            results.sort((a, b) => a.distance - b.distance);
            return results.slice(0, maxItems);
        } catch (e) {
            return [];
        }
    }

    function pushNearbyPinsToServer(lat, lng, mapId = null, floorId = null, opts = {}) {
        try {
            if (typeof lat !== 'number' || !Number.isFinite(lat) || typeof lng !== 'number' || !Number.isFinite(lng)) {
                return false;
            }
            const resolvedMapId = (mapId !== null && mapId !== undefined && !isNaN(Number(mapId)))
                ? parseInt(mapId, 10)
                : getCurrentWebMapId();
            const resolvedFloorId = (floorId !== undefined && floorId !== null && String(floorId).trim() !== '')
                ? String(floorId)
                : null;
            const force = Boolean(opts && opts.force);
            const now = Date.now();
            const elapsed = now - lastNearbyPushTs;
            const moved = (lastNearbyPushLat !== null && lastNearbyPushLng !== null)
                ? Math.hypot(lat - lastNearbyPushLat, lng - lastNearbyPushLng)
                : Infinity;
            const contextChanged = (resolvedMapId !== lastNearbyPushMapId) || (resolvedFloorId !== lastNearbyPushFloorId);

            if (!force) {
                if (elapsed < 300 && !contextChanged) {
                    return false;
                }
                if (elapsed < 1500 && moved <= 15 && !contextChanged) {
                    return false;
                }
            }

            lastNearbyPushTs = now;
            lastNearbyPushLat = lat;
            lastNearbyPushLng = lng;
            lastNearbyPushMapId = resolvedMapId;
            lastNearbyPushFloorId = resolvedFloorId;

            // Keep authoritative HoYoLAB mark_map_point_list cache fresh (auto-re-pushes if marked set updates)
            _refreshMarkedPinIds(resolvedMapId, false);

            _ensureLabelNames(resolvedMapId);
            const bridge = window.__GH_BRIDGE__;
            const nearbyRaw = (bridge && typeof bridge.queryNearbyUncollectedPins === 'function')
                ? bridge.queryNearbyUncollectedPins(lat, lng, NEARBY_QUERY_RADIUS)
                : queryNearbyUncollectedPins(lat, lng, NEARBY_QUERY_RADIUS, NEARBY_QUERY_LIMIT);

            const nearbyList = Array.isArray(nearbyRaw) ? nearbyRaw.slice(0, NEARBY_QUERY_LIMIT) : [];
            // Radius actually covered: a full list stops at its farthest pin, not at the query radius
            let coverageRadius = NEARBY_QUERY_RADIUS;
            if (nearbyList.length >= NEARBY_QUERY_LIMIT) {
                const last = nearbyList[nearbyList.length - 1];
                const lastDist = (last && typeof last.distance === 'number')
                    ? last.distance
                    : Math.hypot(Number(last.lat) - lat, Number(last.lng) - lng);
                if (Number.isFinite(lastDist)) coverageRadius = Math.min(NEARBY_QUERY_RADIUS, lastDist);
            }

            const pins = nearbyList.map((p) => ({
                // Position-based fallback id (no list index) so the id stays stable across pushes
                id: p.id !== null && p.id !== undefined ? p.id : `pin_${Number(p.lat).toFixed(1)}_${Number(p.lng).toFixed(1)}`,
                name: p.name || p.label_name || tr('page.sync.default_pin_title'),
                category: p.label_name || '',
                lat: p.lat,
                lng: p.lng,
                map_id: resolvedMapId,
                label_id: p.label_id ?? p.labelId ?? null,
                parent_label_id: p.parent_label_id ?? p.parentLabelId ?? null,
                floor_id: p.floor_id ?? p.floorId ?? null,
                floor_name: p.floor_name ?? p.floorName ?? '',
                is_underground: Boolean(p.is_underground),
                image_url: p.image_url || '',
                description: p.description || ''
            }));

            extRequest('/nearby_pins', {
                method: 'POST',
                body: {
                    lat,
                    lng,
                    map_id: resolvedMapId,
                    floor_id: resolvedFloorId,
                    coverage_radius: Number(coverageRadius.toFixed(2)),
                    pins
                }
            }).catch(() => {});
            return true;
        } catch (e) {
            return false;
        }
    }

    // =========================================================================
    // 7.1 TSP Smart Route SVG Overlay (<textPath> ► arrows) & Planner (F7)
    // =========================================================================
    function clearTspRouteFromMap(opts = {}) {
        try {
            if (tspRedrawRaf !== null) {
                cancelAnimationFrame(tspRedrawRaf);
                tspRedrawRaf = null;
            }
            if (tspMapRef && typeof tspMapRef.off === 'function') {
                tspMapRef.off('move zoom viewreset zoomend', _scheduleTspRouteRedraw);
            }
            if (tspSvgEl) {
                tspSvgEl.remove();
                tspSvgEl = null;
            }
            const stray = document.getElementById('gh-tsp-route-svg');
            if (stray) stray.remove();
            const hadRoute = Boolean(activeTspRoute);
            activeTspRoute = null;
            tspMapRef = null;
            if (opts && opts.notifyToast && hadRoute) {
                setLog(tr('page.tsp.cleared'), '#94a3b8');
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    function _scheduleTspRouteRedraw() {
        if (!activeTspRoute) return;
        if (tspRedrawRaf !== null) return;
        tspRedrawRaf = requestAnimationFrame(() => {
            tspRedrawRaf = null;
            _drawTspRouteSvg();
        });
    }

    function _extractLegCoords(leg) {
        if (!leg || typeof leg !== 'object') return null;
        let fLat = leg.from_lat;
        let fLng = leg.from_lng;
        let tLat = leg.to_lat;
        let tLng = leg.to_lng;
        if ((fLat === undefined || fLng === undefined) && Array.isArray(leg.points) && leg.points.length >= 2) {
            fLat = leg.points[0][0];
            fLng = leg.points[0][1];
            tLat = leg.points[1][0];
            tLng = leg.points[1][1];
        }
        if (
            typeof fLat !== 'number' || !Number.isFinite(fLat) ||
            typeof fLng !== 'number' || !Number.isFinite(fLng) ||
            typeof tLat !== 'number' || !Number.isFinite(tLat) ||
            typeof tLng !== 'number' || !Number.isFinite(tLng)
        ) {
            return null;
        }
        return { fLat, fLng, tLat, tLng };
    }

    function _drawTspRouteSvg() {
        try {
            if (!activeTspRoute) return false;
            const ctx = findMapContext();
            if (!ctx || !ctx.map || typeof ctx.map.latLngToLayerPoint !== 'function') return false;
            const routeMapId = activeTspRoute.map_id !== undefined && activeTspRoute.map_id !== null
                ? parseInt(activeTspRoute.map_id, 10)
                : null;
            if (routeMapId !== null && !isNaN(routeMapId) && routeMapId !== getCurrentWebMapId()) {
                if (tspSvgEl) tspSvgEl.style.display = 'none';
                return false;
            }

            injectThemeStyles();
            const map = ctx.map;
            const panes = (typeof map.getPanes === 'function') ? map.getPanes() : null;
            const overlayPane = (panes && panes.overlayPane)
                ? panes.overlayPane
                : (typeof map.getContainer === 'function' ? map.getContainer() : null);
            if (!overlayPane) return false;

            const legs = Array.isArray(activeTspRoute.legs) ? activeTspRoute.legs : [];
            const stops = Array.isArray(activeTspRoute.ordered_pins) ? activeTspRoute.ordered_pins : [];
            if (legs.length === 0 && stops.length === 0) {
                clearTspRouteFromMap();
                return false;
            }

            const SVG_NS = 'http://www.w3.org/2000/svg';
            const XLINK_NS = 'http://www.w3.org/1999/xlink';

            if (!tspSvgEl || !tspSvgEl.isConnected || tspSvgEl.parentNode !== overlayPane) {
                if (tspSvgEl) tspSvgEl.remove();
                const old = document.getElementById('gh-tsp-route-svg');
                if (old) old.remove();
                tspSvgEl = document.createElementNS(SVG_NS, 'svg');
                tspSvgEl.setAttribute('id', 'gh-tsp-route-svg');
                tspSvgEl.setAttribute('class', 'gh-tsp-route-svg');
                tspSvgEl.setAttribute('aria-hidden', 'true');
                overlayPane.appendChild(tspSvgEl);
            }
            tspSvgEl.style.display = '';

            if (tspMapRef !== map) {
                if (tspMapRef && typeof tspMapRef.off === 'function') {
                    tspMapRef.off('move zoom viewreset zoomend', _scheduleTspRouteRedraw);
                }
                tspMapRef = map;
                if (typeof map.on === 'function') {
                    map.on('move zoom viewreset zoomend', _scheduleTspRouteRedraw);
                }
            }

            let minX = Infinity;
            let minY = Infinity;
            let maxX = -Infinity;
            let maxY = -Infinity;
            const trackPt = (pt) => {
                if (!pt || !Number.isFinite(pt.x) || !Number.isFinite(pt.y)) return;
                if (pt.x < minX) minX = pt.x;
                if (pt.y < minY) minY = pt.y;
                if (pt.x > maxX) maxX = pt.x;
                if (pt.y > maxY) maxY = pt.y;
            };

            const legGeom = [];
            for (let i = 0; i < legs.length; i++) {
                const coords = _extractLegCoords(legs[i]);
                if (!coords) continue;
                const p1 = map.latLngToLayerPoint([coords.fLat, coords.fLng]);
                const p2 = map.latLngToLayerPoint([coords.tLat, coords.tLng]);
                trackPt(p1);
                trackPt(p2);
                legGeom.push({ idx: i, leg: legs[i], p1, p2 });
            }

            const stopGeom = [];
            for (let i = 0; i < stops.length; i++) {
                const s = stops[i];
                if (!s || typeof s.lat !== 'number' || typeof s.lng !== 'number') continue;
                const pt = map.latLngToLayerPoint([s.lat, s.lng]);
                trackPt(pt);
                stopGeom.push({ idx: i, stop: s, pt });
            }

            if (!Number.isFinite(minX) || !Number.isFinite(minY)) {
                return false;
            }

            const pad = 52;
            const vbX = Math.floor(minX - pad);
            const vbY = Math.floor(minY - pad);
            const vbW = Math.max(2, Math.ceil((maxX - minX) + pad * 2));
            const vbH = Math.max(2, Math.ceil((maxY - minY) + pad * 2));

            tspSvgEl.setAttribute('width', String(vbW));
            tspSvgEl.setAttribute('height', String(vbH));
            tspSvgEl.setAttribute('viewBox', `${vbX} ${vbY} ${vbW} ${vbH}`);
            tspSvgEl.style.transform = `translate3d(${vbX}px, ${vbY}px, 0)`;

            while (tspSvgEl.firstChild) {
                tspSvgEl.removeChild(tspSvgEl.firstChild);
            }

            const defs = document.createElementNS(SVG_NS, 'defs');
            const legsGroup = document.createElementNS(SVG_NS, 'g');
            const arrowsGroup = document.createElementNS(SVG_NS, 'g');
            const stopsGroup = document.createElementNS(SVG_NS, 'g');

            for (const entry of legGeom) {
                const { idx, leg, p1, p2 } = entry;
                const dx = p2.x - p1.x;
                const dy = p2.y - p1.y;
                const segLen = Math.hypot(dx, dy);
                if (segLen < 1.5) continue;

                const isTeleport = Boolean(leg.is_teleport);
                let pathD;
                if (isTeleport) {
                    const midX = (p1.x + p2.x) * 0.5;
                    const midY = (p1.y + p2.y) * 0.5;
                    const bow = Math.min(36, segLen * 0.14);
                    const nx = -dy / segLen;
                    const ny = dx / segLen;
                    const cx = midX + nx * bow;
                    const cy = midY + ny * bow;
                    pathD = `M ${p1.x.toFixed(1)} ${p1.y.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
                } else {
                    pathD = `M ${p1.x.toFixed(1)} ${p1.y.toFixed(1)} L ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
                }

                const halo = document.createElementNS(SVG_NS, 'path');
                halo.setAttribute('d', pathD);
                halo.setAttribute('fill', 'none');
                halo.setAttribute('stroke', 'rgba(15, 23, 42, 0.78)');
                halo.setAttribute('stroke-width', isTeleport ? '5.2' : '6.5');
                halo.setAttribute('stroke-linecap', 'round');
                if (isTeleport) halo.setAttribute('stroke-dasharray', '8 6');
                legsGroup.appendChild(halo);

                const line = document.createElementNS(SVG_NS, 'path');
                line.setAttribute('d', pathD);
                line.setAttribute('fill', 'none');
                line.setAttribute('stroke', isTeleport ? '#f59e0b' : '#38bdf8');
                line.setAttribute('stroke-width', isTeleport ? '2.6' : '3.2');
                line.setAttribute('stroke-linecap', 'round');
                if (isTeleport) {
                    line.setAttribute('stroke-dasharray', '8 6');
                }
                legsGroup.appendChild(line);

                if (isTeleport) {
                    const wpRing = document.createElementNS(SVG_NS, 'circle');
                    wpRing.setAttribute('cx', p2.x.toFixed(1));
                    wpRing.setAttribute('cy', p2.y.toFixed(1));
                    wpRing.setAttribute('r', '7');
                    wpRing.setAttribute('fill', 'rgba(245, 158, 11, 0.25)');
                    wpRing.setAttribute('stroke', '#f59e0b');
                    wpRing.setAttribute('stroke-width', '2');
                    legsGroup.appendChild(wpRing);
                } else if (segLen >= 26) {
                    const pathId = `gh-tsp-leg-path-${idx}`;
                    const defPath = document.createElementNS(SVG_NS, 'path');
                    defPath.setAttribute('id', pathId);
                    defPath.setAttribute('d', pathD);
                    defs.appendChild(defPath);

                    const repeatCount = Math.max(1, Math.min(12, Math.floor(segLen / 40)));
                    const chevronStr = '  ►  '.repeat(repeatCount);

                    const textEl = document.createElementNS(SVG_NS, 'text');
                    textEl.setAttribute('class', 'gh-tsp-chevron-text');
                    textEl.setAttribute('dy', '3.6');

                    const textPathEl = document.createElementNS(SVG_NS, 'textPath');
                    textPathEl.setAttribute('href', `#${pathId}`);
                    textPathEl.setAttributeNS(XLINK_NS, 'xlink:href', `#${pathId}`);
                    textPathEl.setAttribute('startOffset', '50%');
                    textPathEl.setAttribute('text-anchor', 'middle');
                    textPathEl.textContent = chevronStr;

                    textEl.appendChild(textPathEl);
                    arrowsGroup.appendChild(textEl);
                }
            }

            for (const entry of stopGeom) {
                const { idx, stop, pt } = entry;
                const stepNum = stop.step || (idx + 1);
                const isFirst = stepNum === 1;

                const badge = document.createElementNS(SVG_NS, 'g');
                badge.setAttribute('class', 'gh-tsp-stop');

                const circle = document.createElementNS(SVG_NS, 'circle');
                circle.setAttribute('cx', pt.x.toFixed(1));
                circle.setAttribute('cy', pt.y.toFixed(1));
                circle.setAttribute('r', isFirst ? '11.5' : '10');
                circle.setAttribute('fill', isFirst ? '#064e3b' : '#0f172a');
                circle.setAttribute('stroke', isFirst ? '#22c55e' : '#38bdf8');
                circle.setAttribute('stroke-width', '2');
                badge.appendChild(circle);

                const label = document.createElementNS(SVG_NS, 'text');
                label.setAttribute('class', 'gh-tsp-stop-label');
                label.setAttribute('x', pt.x.toFixed(1));
                label.setAttribute('y', pt.y.toFixed(1));
                label.setAttribute('text-anchor', 'middle');
                label.setAttribute('dominant-baseline', 'central');
                label.textContent = String(stepNum);
                badge.appendChild(label);

                stopsGroup.appendChild(badge);
            }

            tspSvgEl.appendChild(defs);
            tspSvgEl.appendChild(legsGroup);
            tspSvgEl.appendChild(arrowsGroup);
            tspSvgEl.appendChild(stopsGroup);
            return true;
        } catch (e) {
            console.error('[Genshin Helper] Error rendering TSP route SVG:', e);
            return false;
        }
    }

    function renderTspRouteOnMap(routeData) {
        if (!routeData || typeof routeData !== 'object') {
            clearTspRouteFromMap();
            return false;
        }
        const legs = Array.isArray(routeData.legs) ? routeData.legs : [];
        const stops = Array.isArray(routeData.ordered_pins) ? routeData.ordered_pins : [];
        if (legs.length === 0 && stops.length === 0) {
            clearTspRouteFromMap();
            return false;
        }
        activeTspRoute = routeData;
        return _drawTspRouteSvg();
    }

    function _collectVisibleUncollectedPins(ctx, startLat, startLng, maxPins = 60) {
        const markedSet = _collectMarkedPinIds(ctx);
        const lookup = _getMarkerMetaLookup(ctx);
        const candidates = [];
        const seenKeys = new Set();

        let minLat = -Infinity;
        let maxLat = Infinity;
        let minLng = -Infinity;
        let maxLng = Infinity;
        if (ctx.map && typeof ctx.map.getBounds === 'function') {
            try {
                const b = ctx.map.getBounds();
                if (b && typeof b.getSouthWest === 'function' && typeof b.getNorthEast === 'function') {
                    const sw = b.getSouthWest();
                    const ne = b.getNorthEast();
                    minLat = Math.min(sw.lat, ne.lat);
                    maxLat = Math.max(sw.lat, ne.lat);
                    minLng = Math.min(sw.lng, ne.lng);
                    maxLng = Math.max(sw.lng, ne.lng);
                }
            } catch (e) {}
        }

        const pushCandidate = (item, markerObj, pinLat, pinLng) => {
            if (typeof pinLat !== 'number' || !Number.isFinite(pinLat) || typeof pinLng !== 'number' || !Number.isFinite(pinLng)) return;
            if (pinLat < minLat || pinLat > maxLat || pinLng < minLng || pinLng > maxLng) return;

            const meta = _resolveRichPinItem(item, markerObj, lookup);
            if (_isNavLandmarkPin(meta.raw, markerObj) || _isNavLandmarkPin(item, markerObj)) return;
            if (_isPinMarked(meta.raw, markerObj, markedSet) || _isPinMarked(item, markerObj, markedSet)) return;

            const dedupKey = meta.id !== null ? `id:${meta.id}` : `${pinLat.toFixed(2)},${pinLng.toFixed(2)}`;
            if (seenKeys.has(dedupKey)) return;
            seenKeys.add(dedupKey);

            candidates.push({
                id: meta.id !== null ? meta.id : `pin_${candidates.length}`,
                name: meta.name,
                lat: pinLat,
                lng: pinLng,
                label_id: meta.labelId,
                floor_id: meta.floorId,
                is_underground: meta.isUnderground,
                image_url: meta.imageUrl,
                description: meta.description,
                _dist: Math.hypot(pinLat - startLat, pinLng - startLng)
            });
        };

        const tree = ctx.ciLayer && ctx.ciLayer._latlngMarkers;
        if (tree && typeof tree.search === 'function' && Number.isFinite(minLat) && Number.isFinite(minLng)) {
            const hits = tree.search({ minX: minLng, minY: minLat, maxX: maxLng, maxY: maxLat });
            for (const entry of hits) {
                const m = entry.data || entry;
                const ll = (m && typeof m.getLatLng === 'function')
                    ? m.getLatLng()
                    : { lat: (entry.minY + entry.maxY) / 2, lng: (entry.minX + entry.maxX) / 2 };
                pushCandidate(entry, m, ll.lat, ll.lng);
            }
        }

        if (candidates.length === 0 && Array.isArray(ctx.markerList) && ctx.markerList.length > 0) {
            for (let i = 0; i < ctx.markerList.length; i++) {
                const item = ctx.markerList[i];
                const m = item.marker;
                const y = (item.y_pos !== undefined) ? item.y_pos : (m && typeof m.getLatLng === 'function' ? m.getLatLng().lat : null);
                const x = (item.x_pos !== undefined) ? item.x_pos : (m && typeof m.getLatLng === 'function' ? m.getLatLng().lng : null);
                if (y === null || x === null) continue;
                pushCandidate(item, m, y, x);
            }
        }

        candidates.sort((a, b) => a._dist - b._dist);
        const trimmed = candidates.slice(0, maxPins).map(({ _dist, ...rest }) => rest);

        if (trimmed.length < 2) {
            const fallback = queryNearbyUncollectedPins(startLat, startLng, 1500, maxPins);
            if (fallback.length > trimmed.length) {
                return fallback.map((p, idx) => ({
                    id: p.id !== null && p.id !== undefined ? p.id : `pin_${idx}`,
                    name: p.name,
                    lat: p.lat,
                    lng: p.lng,
                    label_id: p.label_id ?? p.labelId ?? null,
                    floor_id: p.floor_id ?? p.floorId ?? null,
                    is_underground: Boolean(p.is_underground),
                    image_url: p.image_url || '',
                    description: p.description || ''
                }));
            }
        }
        return trimmed;
    }

    async function requestPlanTspRoute(opts = {}) {
        try {
            const ctx = findMapContext();
            if (!ctx || !ctx.map) {
                setLog(tr('page.sync.map_not_found'), '#ef4444');
                return { ok: false, error: 'Map not ready' };
            }
            const currentMapId = getCurrentWebMapId();
            const center = (typeof ctx.map.getCenter === 'function') ? ctx.map.getCenter() : { lat: 0, lng: 0 };
            const usePlayer = Boolean(
                playerLastState &&
                typeof playerLastState.lat === 'number' &&
                typeof playerLastState.lng === 'number' &&
                Number(playerLastState.map_id || currentMapId) === Number(currentMapId)
            );
            const startLat = usePlayer ? playerLastState.lat : center.lat;
            const startLng = usePlayer ? playerLastState.lng : center.lng;

            const maxPins = (opts && typeof opts.maxPins === 'number' && opts.maxPins > 0) ? Math.min(60, Math.floor(opts.maxPins)) : 60;
            const pins = _collectVisibleUncollectedPins(ctx, startLat, startLng, maxPins);
            if (pins.length === 0) {
                setLog(tr('page.tsp.no_pins'), '#f59e0b');
                return { ok: false, empty: true, error: 'No visible uncollected pins' };
            }

            setLog(tr('page.tsp.planning'), '#38bdf8');
            const res = await extRequest('/plan_tsp_route', {
                method: 'POST',
                body: {
                    map_id: currentMapId,
                    start_pos: [startLat, startLng],
                    pins
                }
            });

            const routeData = (res && res.route && typeof res.route === 'object') ? res.route : res;
            if (!routeData || typeof routeData !== 'object' || (!Array.isArray(routeData.legs) && !Array.isArray(routeData.ordered_pins))) {
                const errMsg = (res && res.error) ? String(res.error) : 'Invalid route response';
                setLog(tr('page.tsp.error', { error: errMsg }), '#ef4444');
                return { ok: false, error: errMsg };
            }
            if (routeData.map_id === undefined) {
                routeData.map_id = currentMapId;
            }

            renderTspRouteOnMap(routeData);
            const stopCount = typeof routeData.pin_count === 'number'
                ? routeData.pin_count
                : (Array.isArray(routeData.ordered_pins) ? routeData.ordered_pins.length : pins.length);
            const tpCount = typeof routeData.teleport_count === 'number'
                ? routeData.teleport_count
                : (Array.isArray(routeData.teleport_hints) ? routeData.teleport_hints.length : 0);
            setLog(tr('page.tsp.planned', { count: stopCount, teleports: tpCount }), '#22c55e');
            return { ok: true, route: routeData };
        } catch (err) {
            const errMsg = (err && err.message) ? err.message : 'Request failed';
            setLog(tr('page.tsp.error', { error: errMsg }), '#ef4444');
            return { ok: false, error: errMsg };
        }
    }

    // =========================================================================
    // 7.2 1-Click HoYoLAB Cross-Map Pin Exporter (F8)
    // =========================================================================
    function _triggerClientFileDownload(filename, contentStr, mimeType) {
        const blob = new Blob([contentStr], { type: mimeType });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        link.style.display = 'none';
        (document.body || document.documentElement).appendChild(link);
        link.click();
        setTimeout(() => {
            try { link.remove(); } catch (e) {}
            try { URL.revokeObjectURL(url); } catch (e) {}
        }, 4000);
    }

    async function _fetchHoyolabOriginJson(url) {
        const res = await fetch(url, {
            method: 'GET',
            credentials: 'include',
            headers: {
                'x-rpc-map_version': '4.5'
            }
        });
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
        return res.json();
    }

    async function exportMarkedPins(targetFormat = 'canonical') {
        if (exportInProgress) {
            return { ok: false, busy: true };
        }
        const allowedFormats = ['kongying', 'appsample', 'genshinmap', 'canonical'];
        const normalizedFmt = String(targetFormat || 'canonical').trim().toLowerCase();
        const fmt = allowedFormats.includes(normalizedFmt) ? normalizedFmt : 'canonical';
        const mapId = getCurrentWebMapId();
        const apiLang = (window.GHI18n && window.GHI18n.getLang() === 'vi') ? 'vi-vn' : 'en-us';
        const apiBase = 'https://sg-public-api.hoyolab.com/common/map_user/ys_obc';

        exportInProgress = true;
        setLog(tr('page.export.fetching', { format: fmt }), '#38bdf8');
        try {
            // Fetch FULL point/list WITHOUT narrow label_ids filter so hidden categories are never dropped
            const pointListUrl = `${apiBase}/v2/map/point/list?map_id=${encodeURIComponent(mapId)}&app_sn=ys_obc&lang=${encodeURIComponent(apiLang)}`;
            const markedListUrl = `${apiBase}/v1/map/mark_map_point_list?map_id=${encodeURIComponent(mapId)}&app_sn=ys_obc&lang=${encodeURIComponent(apiLang)}`;

            const [pointListEnvelope, markedListEnvelope] = await Promise.all([
                _fetchHoyolabOriginJson(pointListUrl),
                _fetchHoyolabOriginJson(markedListUrl)
            ]);

            if (markedListEnvelope && typeof markedListEnvelope.retcode === 'number' && markedListEnvelope.retcode !== 0) {
                if (markedListEnvelope.retcode === -100 || markedListEnvelope.retcode === 10001) {
                    setLog(tr('page.export.login_required'), '#f59e0b');
                    return { ok: false, error: 'HoYoLAB login required' };
                }
                throw new Error(markedListEnvelope.message || `HoYoLAB retcode ${markedListEnvelope.retcode}`);
            }

            const res = await extRequest('/cross_map_export', {
                method: 'POST',
                timeoutMs: 30000,
                body: {
                    point_list: pointListEnvelope,
                    marked_list: markedListEnvelope,
                    map_id: mapId,
                    target_format: fmt
                }
            });

            if (!res || res.ok === false) {
                throw new Error((res && res.error) ? String(res.error) : 'Export failed');
            }

            const rawPayload = (res.content !== undefined)
                ? res.content
                : (res.exported !== undefined
                    ? res.exported
                    : (res.payload !== undefined
                        ? res.payload
                        : (res.output !== undefined ? res.output : res.data)));

            const isTextFormat = (
                fmt === 'genshinmap' ||
                (typeof rawPayload === 'string' && rawPayload.startsWith('GM_007~')) ||
                (typeof res.filename === 'string' && res.filename.toLowerCase().endsWith('.txt'))
            );
            const ext = isTextFormat ? 'txt' : 'json';
            const mimeType = isTextFormat ? 'text/plain;charset=utf-8' : 'application/json;charset=utf-8';
            const filename = (typeof res.filename === 'string' && res.filename.trim())
                ? res.filename.trim()
                : `genshin-helper-export-${fmt}-map${mapId}.${ext}`;

            const fileContent = (typeof rawPayload === 'string')
                ? rawPayload
                : JSON.stringify(rawPayload !== undefined ? rawPayload : res, null, 2);

            _triggerClientFileDownload(filename, fileContent, mimeType);

            const stats = (res.stats && typeof res.stats === 'object')
                ? res.stats
                : ((res.summary && typeof res.summary === 'object')
                    ? res.summary
                    : ((res.report && typeof res.report === 'object') ? res.report : res));

            const matched = Number(
                stats.matched_count ?? stats.matched ?? stats.total_pins ?? stats.exported_count ??
                (rawPayload && Array.isArray(rawPayload.pins) ? rawPayload.pins.length : 0)
            ) || 0;
            const unmatched = Number(stats.unmatched_count ?? stats.unmatched ?? 0) || 0;
            const missing = Number(
                stats.missing_from_point_list_count ??
                (Array.isArray(stats.missing_from_point_list) ? stats.missing_from_point_list.length : null) ??
                (Array.isArray(res.missing_from_point_list) ? res.missing_from_point_list.length : 0)
            ) || 0;

            setLog(tr('page.export.done', { format: fmt, matched, unmatched, missing }), '#22c55e');
            return { ok: true, filename, matched, unmatched, missing };
        } catch (err) {
            const errMsg = (err && err.message) ? err.message : 'Export failed';
            setLog(tr('page.export.error', { error: errMsg }), '#ef4444');
            return { ok: false, error: errMsg };
        } finally {
            exportInProgress = false;
        }
    }

    function handleLocalCommand(cmdData) {
        if (!cmdData || typeof cmdData !== 'object') return;
        const cmd = cmdData.type || cmdData.kind;
        if (cmd === 'GH_CMD_PLAN_TSP') {
            requestPlanTspRoute(cmdData);
        } else if (cmd === 'GH_CMD_CLEAR_TSP') {
            clearTspRouteFromMap({ notifyToast: true });
            extRequest('/plan_tsp_route', {
                method: 'POST',
                body: { map_id: getCurrentWebMapId(), pins: [] }
            }).catch(() => {});
        } else if (cmd === 'GH_CMD_EXPORT_PINS') {
            exportMarkedPins(cmdData.targetFormat || cmdData.target_format || 'canonical');
        }
    }

    window.__GH_BRIDGE__ = Object.freeze({
        version: SCRIPT_VERSION,
        getContext: () => findMapContext(),
        getCurrentMapId: () => getCurrentWebMapId(),
        getPlayerState: () => (playerLastState ? { ...playerLastState } : null),
        updatePlayerMarker: (data) => {
            const ctx = findMapContext();
            if (ctx && ctx.map) {
                updatePlayerMarker(ctx, data);
                return true;
            }
            return false;
        },
        queryNearbyUncollectedPins,
        pushNearbyPinsToServer,
        renderTspRouteOnMap,
        clearTspRouteFromMap,
        requestPlanTspRoute,
        exportMarkedPins,
        getLanguage: () => (window.GHI18n ? window.GHI18n.getLang() : 'en'),
        setLanguage: (lang) => {
            if (window.GHI18n && typeof window.GHI18n.setLang === 'function') {
                return window.GHI18n.setLang(lang);
            }
            return Promise.resolve(lang);
        },
        isCompactUI: () => compactUiEnabled,
        setCompactUI: (enabled) => setCompactUI(Boolean(enabled)),
        toggleCompactUI: () => setCompactUI(!compactUiEnabled),
        isAutoPanEnabled: () => autoPanEnabled,
        setAutoPan: (enabled) => setAutoPanPinned(Boolean(enabled)),
        recenter: () => recenterToPlayer(),
        unwrapAngle
    });

    // Inject marker/toast/compact styles, restore opt-in compact mode, attach drag-unpin, and sync initial status
    injectThemeStyles();
    if (compactUiEnabled && document.body) {
        document.body.classList.add('gh-compact-ui');
        applyCompactDomTags();
        setTimeout(applyCompactDomTags, 1200);
    }
    bindDragUnpinListeners(findMapContext());
    _ensureMarkRedrawHooks(findMapContext());
    setTimeout(() => {
        const c = findMapContext();
        bindDragUnpinListeners(c);
        _ensureMarkRedrawHooks(c);
    }, 1200);
    _refreshMarkedPinIds(getCurrentWebMapId(), false);
    // Refresh marked pin cache shortly after any user click inside a HoYoLAB pin popup (mark/unmark)
    const onDocClick = (ev) => {
        try {
            const target = ev.target && ev.target.closest ? ev.target.closest('.map-popup, .expand-popup, .leaflet-popup') : null;
            if (target) {
                _ensureMarkRedrawHooks(findMapContext());
                setTimeout(() => {
                    _refreshMarkedPinIds(getCurrentWebMapId(), true).finally(() => {
                        _reconcileMarkedVisuals(findMapContext(), { skipSelectedPopup: true });
                    });
                }, 600);
            }
        } catch (e) {}
    };
    document.addEventListener('click', onDocClick, true);
    window.__gh_teardown = () => {
        try { window.removeEventListener('message', onWindowMessage); } catch (e) {}
        try { document.removeEventListener('visibilitychange', onVisibilityChange); } catch (e) {}
        try { document.removeEventListener('click', onDocClick, true); } catch (e) {}
        try { clearInterval(reportStateTimer); } catch (e) {}
        try { clearLayerTimers(); } catch (e) {}
        try { if (typeof dragListenersCleanup === 'function') dragListenersCleanup(); } catch (e) {}
        try {
            if (playerMarkerLayer && typeof playerMarkerLayer.remove === 'function') {
                playerMarkerLayer.remove();
            }
        } catch (e) {}
        try {
            if (playerDomMapRef && typeof playerDomMapRef.off === 'function') {
                playerDomMapRef.off('move zoom viewreset zoomend', _repositionDomPlayerMarker);
            }
            if (playerDomMarkerEl) {
                playerDomMarkerEl.remove();
            }
        } catch (e) {}
        playerMarkerLayer = null;
        playerMarkerMapRef = null;
        playerMarkerWrapEl = null;
        playerDomMarkerEl = null;
        playerDomWrapEl = null;
        playerDomMapRef = null;
        _cleanupOrphanPlayerMarkers(null);
    };
    fetchInitialStatus();
    }

    bootGenshinHelper();

})();
