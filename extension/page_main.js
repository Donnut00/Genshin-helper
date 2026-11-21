(function () {
    'use strict';

    window.__gh_runtime_mode = 'extension';

    if (!window.location.hostname.includes('hoyolab.com') || !window.location.pathname.includes('/interactive-map/')) {
        return;
    }

    const SCRIPT_VERSION = 'v4.5.0';

    function bootGenshinHelper() {
    if (window.__gh_booted_version === SCRIPT_VERSION) return;
    window.__gh_booted_version = SCRIPT_VERSION;

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
    } catch(e) {}

    console.log('%c[Genshin Helper v4.5.0] Loaded (Zero-Clutter Map Surface + Player Marker + Vuex AreaSync)', 'color: #38bdf8; font-size: 16px; font-weight: bold;');

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
        console.log('[Genshin Helper v4.5.0]', msg);
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
    // 4. Extension Bridge Events & Live State Sync
    // =========================================================================
    function warnServerVersionMismatch(data) {
        if (data && data.script_version && data.script_version !== SCRIPT_VERSION && !warnedVersionMismatch) {
            warnedVersionMismatch = true;
            console.warn(`[Genshin Helper] Phiên bản Desktop (${data.script_version}) khác Extension (${SCRIPT_VERSION}).`);
        }
    }

    window.addEventListener('message', (e) => {
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
            if (!initialStatusSynced) {
                fetchInitialStatus();
            }
            return;
        }

        if (msg.kind === 'SERVER_EVENT' && msg.data) {
            setStatus(true);
            const data = msg.data;
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
    });

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

    if (!window.__gh_vis_listener_bound) {
        window.__gh_vis_listener_bound = true;
        document.addEventListener('visibilitychange', () => {
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
                    forceRefreshCaveCanvas(resolveGisVm(ctx));
                }
            } catch (e) {}
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
                    runtimeMode: 'extension'
                }
            }).catch(() => {});
        } catch (e) {}
    }

    setInterval(reportState, 5000);

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

                apiRequest('/save_diagnostic_note', {
                    method: 'POST',
                    body: { folder_name: fname, note: noteText }
                })
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
                .catch(err => {
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Lưu ghi chú';
                    errEl.textContent = (err && err.data && err.data.error)
                        ? err.data.error
                        : 'Không thể kết nối server desktop (127.0.0.1:8765)!';
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

        if (data.type === 'SINGLE_MONITOR_UPDATED') {
            if (typeof data.single_monitor_mode === 'boolean') {
                setSingleMonitorUI(data.single_monitor_mode);
                setLog(data.single_monitor_mode ? '🖥️ Chế độ 1 Màn Hình: BẬT' : '🖥️ Chế độ 1 Màn Hình: TẮT', '#10b981');
            }
            return;
        }

        if (data.type === 'ENGINE_STATE_UPDATED') {
            if (typeof data.engine_active === 'boolean') {
                setStatus(true, data.engine_active);
                setLog(
                    data.engine_active ? '⚡ Server đã hoạt động trở lại!' : '💤 Server đang tạm nghỉ (Standby — 0% CPU)',
                    data.engine_active ? '#22c55e' : '#fbbf24'
                );
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
            const eventSeq = typeof data._seq === 'number' ? data._seq : 0;
            if (data.detected === false) {
                if (markAfterSync) {
                    setLog('⚠️ Chưa tìm thấy vị trí game! Hãy mở Map (M) trong game rồi kích hoạt lại.', '#f59e0b');
                    sendActionFeedback('SYNC_AND_MARK', 'error', 'Không thấy vị trí game', 'Hãy mở Map (M) trong game rồi thử lại', eventSeq);
                }
                return;
            }
            performAutoSyncAndMark(data, markAfterSync, eventSeq);
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

    function performAutoSyncAndMark(data, markAfterSync = false, eventSeq = 0) {
        try {
            const targetMapId = data.map_id ? parseInt(data.map_id) : 2;
            const currentMapId = getCurrentWebMapId();

            if (data.has_world_coords && targetMapId !== currentMapId) {
                const mapName = data.map_name || `Map ${targetMapId}`;
                setLog(`🔄 Tự động chuyển sang bản đồ [${mapName}]...`, '#38bdf8');
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
                    setLog(`✅ Đã mở bản đồ [${mapName}]! Đang trượt tới vị trí...`, '#22c55e');
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
                setLog('❌ Chưa tìm thấy Leaflet Map! Hãy chắc chắn map đã tải xong.', '#ef4444');
                if (markAfterSync) {
                    sendActionFeedback('SYNC_AND_MARK', 'error', 'Bản đồ HoYoLAB chưa sẵn sàng', 'Hãy đợi trang map tải xong rồi thử lại', eventSeq);
                }
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
                    const floorLabel = data.is_underground
                        ? (data.hoyolab_floor_name || data.floor_short || data.floor_name || 'Phân tầng')
                        : (data.map_name || 'Mặt đất');
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
                        sendActionFeedback('SYNC_AND_MARK', 'no_nearby', 'Không có điểm gần (< 60m)', '', eventSeq);
                        return;
                    }

                    setLog(`Điểm gần nhất cách người chơi ${minDistance.toFixed(1)}m. Đang mở...`, '#10b981');
                } else {
                    // Screen center fallback
                    const markers = getAllMarkers(ctx);
                    if (markers.length === 0) {
                        setLog('❌ Chưa thấy điểm nào! Hãy chọn bật loại Rương hoặc Thần Đồng ở menu!', '#ef4444');
                        sendActionFeedback('SYNC_AND_MARK', 'no_nearby', 'Thiếu mốc định vị', 'Thu nhỏ bản đồ để thấy mốc dịch chuyển', eventSeq);
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
                    sendActionFeedback('SYNC_AND_MARK', 'no_nearby', 'Thiếu mốc định vị', 'Thu nhỏ bản đồ để thấy mốc dịch chuyển', eventSeq);
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
                        resolved = true;
                        if (checkInterval) clearInterval(checkInterval);
                        clickGhimAndNotify(ghimBtn, eventSeq, hasWorld ? minDistance : null);
                        return;
                    }

                    const alreadyMarked = findAlreadyMarkedButton();
                    if (alreadyMarked) {
                        resolved = true;
                        if (checkInterval) clearInterval(checkInterval);
                        const itemTitle = getPopupTitle();
                        if (!singleMonitorMode) playBeep();
                        setLog('ℹ️ Điểm này ĐÃ ĐƯỢC GHIM từ trước!', '#fbbf24');
                        sendActionFeedback('SYNC_AND_MARK', 'already_marked', 'Đã ghim từ trước', itemTitle, eventSeq);
                        return;
                    }

                    if (Date.now() >= deadline) {
                        resolved = true;
                        if (checkInterval) clearInterval(checkInterval);
                        setLog('⚠️ Không thấy nút Ghim trên điểm gần nhất.', '#f59e0b');
                        sendActionFeedback('SYNC_AND_MARK', 'no_nearby', 'Không thấy nút Ghim trên điểm gần nhất', '', eventSeq);
                    }
                };

                checkInterval = setInterval(pollPopupTick, 100);
                setTimeout(pollPopupTick, 30);

            }, waitTime);

        } catch (err) {
            console.error('[Genshin Helper Fatal Error]', err);
            setLog(`❌ Lỗi: ${err.message}`, '#ef4444');
            if (markAfterSync) {
                sendActionFeedback('SYNC_AND_MARK', 'error', 'Lỗi đồng bộ', err.message || '', eventSeq);
            }
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

    function getPopupTitle() {
        let itemTitle = 'Điểm đánh dấu';
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

    function clickGhimAndNotify(btn, seq = 0, distM = null) {
        const itemTitle = getPopupTitle();
        btn.click();
        if (!singleMonitorMode) playBeep();
        setLog(`✅ Đã kéo map & GHIM: [${itemTitle}]!`, '#22c55e');
        const distStr = (typeof distM === 'number' && isFinite(distM)) ? `Cách ${distM.toFixed(1)}m` : '';
        sendActionFeedback('SYNC_AND_MARK', 'marked', 'Đã ghim: ' + itemTitle, distStr, seq);
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

    // Inject marker/toast styles and sync initial status via extension background worker
    injectThemeStyles();
    fetchInitialStatus();
    }

    bootGenshinHelper();

})();
