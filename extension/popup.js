'use strict';

function sendBg(msg) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(msg, (response) => {
        const err = chrome.runtime.lastError;
        if (err) {
          resolve({ ok: false, error: err.message });
          return;
        }
        resolve(response || { ok: false, error: 'Empty response' });
      });
    } catch (e) {
      resolve({ ok: false, error: e.message || 'sendMessage failed' });
    }
  });
}

function apiRequest(path, method = 'GET', body = undefined) {
  return sendBg({ kind: 'API_REQUEST', path, method, body });
}

function t(key, params) {
  if (window.GHI18n && typeof window.GHI18n.t === 'function') {
    return window.GHI18n.t(key, params);
  }
  return key;
}

function getActiveLang() {
  if (window.GHI18n && typeof window.GHI18n.getLang === 'function') {
    return window.GHI18n.getLang();
  }
  return 'vi';
}

function notifyMapTabs(payload) {
  try {
    if (typeof chrome === 'undefined' || !chrome.tabs || typeof chrome.tabs.query !== 'function') {
      return;
    }
    chrome.tabs.query({ url: 'https://act.hoyolab.com/*' }, (tabs) => {
      void (chrome.runtime && chrome.runtime.lastError);
      if (!Array.isArray(tabs)) return;
      for (const tab of tabs) {
        if (tab && typeof tab.id === 'number') {
          try {
            chrome.tabs.sendMessage(tab.id, payload, () => {
              void (chrome.runtime && chrome.runtime.lastError);
            });
          } catch (e) {}
        }
      }
    });
  } catch (e) {}
}

function readLocalStorage(keys) {
  return new Promise((resolve) => {
    try {
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        chrome.storage.local.get(keys, (items) => {
          void (chrome.runtime && chrome.runtime.lastError);
          resolve(items || {});
        });
        return;
      }
    } catch (e) {}
    resolve({});
  });
}

function writeLocalStorage(obj) {
  return new Promise((resolve) => {
    try {
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        chrome.storage.local.set(obj, () => {
          void (chrome.runtime && chrome.runtime.lastError);
          resolve(true);
        });
        return;
      }
    } catch (e) {}
    resolve(false);
  });
}

function formatRelativeTime(tsVal) {
  if (typeof tsVal !== 'number' || !Number.isFinite(tsVal) || tsVal <= 0) {
    return '';
  }
  const tsMs = tsVal < 1e12 ? tsVal * 1000 : tsVal;
  const diffSec = Math.max(0, Math.floor((Date.now() - tsMs) / 1000));
  if (diffSec < 5) return t('popup.time.just_now');
  if (diffSec < 60) return t('popup.time.seconds_ago', { n: diffSec });
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return t('popup.time.minutes_ago', { n: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t('popup.time.hours_ago', { n: diffHr });
  try {
    const localeTag = getActiveLang() === 'vi' ? 'vi-VN' : 'en-US';
    return new Date(tsMs).toLocaleTimeString(localeTag, { hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return '';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Header & Tabs
  const versionBadge = document.getElementById('version-badge');
  const btnLangToggle = document.getElementById('btn-lang-toggle');
  const statusPill = document.getElementById('status-pill');
  const statusText = document.getElementById('status-text');
  const tabButtons = Array.from(document.querySelectorAll('.tab-btn'));
  const tabPanels = {
    control: document.getElementById('tab-panel-control'),
    tools: document.getElementById('tab-panel-tools'),
    settings: document.getElementById('tab-panel-settings')
  };
  const uipiWarningBanner = document.getElementById('uipi-warning-banner');

  // Tab 1: Control
  const powerTitle = document.getElementById('power-title');
  const powerDesc = document.getElementById('power-desc');
  const btnPowerToggle = document.getElementById('btn-power-toggle');
  const powerSwitchLabel = document.getElementById('power-switch-label');
  const nativeHostBanner = document.getElementById('native-host-banner');

  const syncFloorBadge = document.getElementById('sync-floor-badge');
  const syncLocation = document.getElementById('sync-location');
  const syncCoordsRow = document.getElementById('sync-coords-row');
  const syncCoords = document.getElementById('sync-coords');
  const syncTime = document.getElementById('sync-time');

  const toggleAutoSync = document.getElementById('toggle-auto-sync');
  const toggleMinimapTracking = document.getElementById('toggle-minimap-tracking');
  const toggleNavHud = document.getElementById('toggle-nav-hud');
  const selectHudPosition = document.getElementById('select-hud-position');
  const btnHudEdit = document.getElementById('btn-hud-edit');
  const toggleHudCapture = document.getElementById('toggle-hud-capture');
  const toggleSingleMonitor = document.getElementById('toggle-single-monitor');
  const singleMonitorHint = document.getElementById('single-monitor-hint');
  const singleMonitorTip = document.getElementById('single-monitor-tip');
  const toggleCompactUi = document.getElementById('toggle-compact-ui');

  const btnOpenMap = document.getElementById('btn-open-map');
  const btnSwitchGame = document.getElementById('btn-switch-game');

  // Tab 2: v5 Tools (TSP Route, Cross-Map Export, LAN Companion QR, Borderless Launch)
  const btnPlanTsp = document.getElementById('btn-plan-tsp');
  const btnClearTsp = document.getElementById('btn-clear-tsp');
  const selectExportFormat = document.getElementById('select-export-format');
  const btnExportPins = document.getElementById('btn-export-pins');
  const lanStatusBadge = document.getElementById('lan-status-badge');
  const btnToggleLan = document.getElementById('btn-toggle-lan');
  const lanDetails = document.getElementById('lan-details');
  const lanQrBox = document.getElementById('lan-qr-box');
  const lanPinCode = document.getElementById('lan-pin-code');
  const lanUrlText = document.getElementById('lan-url-text');
  const btnCopyLanUrl = document.getElementById('btn-copy-lan-url');
  const lanClientsText = document.getElementById('lan-clients-text');
  const btnLaunchBorderless = document.getElementById('btn-launch-borderless');

  // Tab 3: Hotkeys & Diagnostics
  const recorders = {
    sync: {
      box: document.getElementById('rec-box-sync'),
      display: document.getElementById('rec-display-sync'),
      badge: document.getElementById('rec-badge-sync'),
      val: 'alt+f11',
      displayText: 'Alt + F11'
    },
    switch: {
      box: document.getElementById('rec-box-switch'),
      display: document.getElementById('rec-display-switch'),
      badge: document.getElementById('rec-badge-switch'),
      val: 'alt+q',
      displayText: 'Alt + Q'
    },
    debug: {
      box: document.getElementById('rec-box-debug'),
      display: document.getElementById('rec-display-debug'),
      badge: document.getElementById('rec-badge-debug'),
      val: 'alt+f12',
      displayText: 'Alt + F12'
    }
  };
  // In-game navigation hotkeys (Mini-HUD target actions)
  const NAV_HOTKEY_DEFAULTS = {
    nav_mark: ['alt+k', 'Alt + K'],
    nav_undo: ['alt+shift+k', 'Alt + Shift + K'],
    nav_guide: ['alt+h', 'Alt + H'],
    nav_next: ['alt+n', 'Alt + N'],
    nav_layout: ['alt+shift+h', 'Alt + Shift + H']
  };
  for (const [navKey, [navVal, navDisp]] of Object.entries(NAV_HOTKEY_DEFAULTS)) {
    const box = document.getElementById(`rec-box-${navKey}`);
    if (!box) continue;
    recorders[navKey] = {
      box,
      display: document.getElementById(`rec-display-${navKey}`),
      badge: document.getElementById(`rec-badge-${navKey}`),
      val: navVal,
      displayText: navDisp
    };
  }
  const HOTKEY_ACTIONS = ['sync', 'switch', 'debug', ...Object.keys(NAV_HOTKEY_DEFAULTS)];
  const presetButtons = Array.from(document.querySelectorAll('.preset-btn[data-target]'));
  const hotkeyBackupsBar = document.getElementById('hotkey-backups-bar');
  const btnSaveHotkeys = document.getElementById('btn-save-hotkeys');
  const btnTriggerDiag = document.getElementById('btn-trigger-diag');
  const diagStatus = document.getElementById('diag-status');
  const btnHardQuit = document.getElementById('btn-hard-quit');

  // Global feedback & footer
  const feedbackBar = document.getElementById('popup-feedback');
  const mapTabStatus = document.getElementById('map-tab-status');
  const donateDrawer = document.getElementById('donate-drawer');
  const btnDonateToggle = document.getElementById('btn-donate-toggle');
  const btnDonateClose = document.getElementById('btn-donate-close');
  const btnDonateMomo = document.getElementById('btn-donate-momo');
  const donateMomoSub = document.getElementById('donate-momo-sub');
  const donateQrWrap = document.getElementById('donate-qr-wrap');

  // Local state
  let currentConnected = false;
  let currentEngineActive = true;
  let currentLastStatus = null;
  let currentLastSync = null;
  let cachedTabCount = 0;
  let powerBusy = false;
  let hotkeysDirty = false;
  let activeRecordTarget = null;
  let activeExtRecord = null;
  let conflictCheckSeq = 0;
  let initialConflictChecked = false;
  let feedbackTimer = null;
  let diagHasCustomStatus = false;
  let lanRunning = false;
  let lanInfo = null;
  let lanBusy = false;
  let lanStatusFetchInFlight = false;
  let lastRenderedQrSvg = '';

  function renderLangToggleUI(lang) {
    if (!btnLangToggle) return;
    const active = lang === 'en' ? 'en' : 'vi';
    btnLangToggle.setAttribute('data-lang', active);
    const opts = btnLangToggle.querySelectorAll('.lang-opt');
    for (const opt of opts) {
      opt.classList.toggle('is-active', opt.getAttribute('data-lang-opt') === active);
    }
  }

  function applyLanguageToPopup() {
    const lang = getActiveLang();
    renderLangToggleUI(lang);
    if (window.GHI18n && typeof window.GHI18n.applyDom === 'function') {
      window.GHI18n.applyDom(document);
    }
    if (donateMomoSub && donateQrWrap) {
      donateMomoSub.textContent = donateQrWrap.hidden
        ? t('popup.donate.momo_sub')
        : t('popup.donate.momo_hide');
    }
    document.title = t('popup.title');
    for (const rec of Object.values(recorders)) {
      if (rec.box.classList.contains('is-recording')) {
        rec.display.textContent = t('popup.hotkeys.press_key');
        rec.badge.textContent = t('popup.hotkeys.badge_listening');
      } else if (rec.box.classList.contains('is-dirty')) {
        rec.badge.textContent = t('popup.hotkeys.badge_unsaved');
      } else {
        rec.badge.textContent = t('popup.hotkeys.badge_change');
      }
    }
    if (!diagHasCustomStatus) {
      diagStatus.textContent = t('popup.diag.ready');
    }
    applyState({
      connected: currentConnected,
      engineActive: currentEngineActive,
      statusData: currentLastStatus,
      lastSync: currentLastSync,
      tabCount: cachedTabCount
    });
  }

  function showFeedback(text, variant = 'warn', autoHideMs = 0) {
    if (feedbackTimer) {
      clearTimeout(feedbackTimer);
      feedbackTimer = null;
    }
    feedbackBar.classList.remove('is-error', 'is-success');
    if (!text) {
      feedbackBar.hidden = true;
      feedbackBar.textContent = '';
      return;
    }
    feedbackBar.hidden = false;
    feedbackBar.textContent = text;
    if (variant === 'error') {
      feedbackBar.classList.add('is-error');
    } else if (variant === 'success') {
      feedbackBar.classList.add('is-success');
    }
    if (autoHideMs > 0) {
      feedbackTimer = setTimeout(() => {
        feedbackBar.hidden = true;
        feedbackBar.textContent = '';
        feedbackBar.classList.remove('is-error', 'is-success');
      }, autoHideMs);
    }
  }

  function renderSafeQrSvg(container, svgMarkup) {
    if (!container) return;
    if (!svgMarkup || typeof svgMarkup !== 'string') {
      while (container.firstChild) container.removeChild(container.firstChild);
      lastRenderedQrSvg = '';
      return;
    }
    if (svgMarkup === lastRenderedQrSvg && container.firstChild) {
      return;
    }
    try {
      const parser = new DOMParser();
      const doc = parser.parseFromString(svgMarkup, 'image/svg+xml');
      const root = doc && doc.documentElement;
      if (!root || root.nodeName.toLowerCase() !== 'svg' || doc.querySelector('parsererror')) {
        return;
      }
      const SVG_NS = 'http://www.w3.org/2000/svg';
      const ALLOWED_TAGS = new Set(['svg', 'rect', 'path', 'g']);
      const ALLOWED_ATTRS = new Set([
        'viewbox', 'width', 'height', 'shape-rendering', 'role', 'aria-label',
        'd', 'fill', 'x', 'y', 'rx', 'ry'
      ]);
      const cloneSafe = (node) => {
        const tag = node.nodeName.toLowerCase();
        if (!ALLOWED_TAGS.has(tag)) return null;
        const el = document.createElementNS(SVG_NS, tag);
        if (node.attributes) {
          for (const attr of Array.from(node.attributes)) {
            const nameLower = attr.name.toLowerCase();
            if (ALLOWED_ATTRS.has(nameLower)) {
              const properName = nameLower === 'viewbox' ? 'viewBox' : attr.name;
              el.setAttribute(properName, attr.value);
            }
          }
        }
        for (const child of Array.from(node.childNodes)) {
          if (child.nodeType === 1) {
            const safeChild = cloneSafe(child);
            if (safeChild) el.appendChild(safeChild);
          }
        }
        return el;
      };
      const safeSvg = cloneSafe(root);
      if (safeSvg) {
        while (container.firstChild) container.removeChild(container.firstChild);
        container.appendChild(safeSvg);
        lastRenderedQrSvg = svgMarkup;
      }
    } catch (e) {}
  }

  function renderLanCompanionUI() {
    if (lanStatusBadge) {
      lanStatusBadge.classList.toggle('is-on', lanRunning);
      lanStatusBadge.classList.toggle('is-off', !lanRunning);
      lanStatusBadge.textContent = lanRunning ? t('popup.lan.status_on') : t('popup.lan.status_off');
    }
    if (btnToggleLan && !lanBusy) {
      btnToggleLan.textContent = lanRunning ? t('popup.lan.stop_btn') : t('popup.lan.start_btn');
      btnToggleLan.classList.toggle('btn-amber', lanRunning);
      btnToggleLan.classList.toggle('btn-primary', !lanRunning);
      btnToggleLan.disabled = !currentConnected;
    }
    if (lanDetails) {
      lanDetails.hidden = !lanRunning;
    }
    if (lanRunning && lanInfo && typeof lanInfo === 'object') {
      if (lanPinCode) {
        lanPinCode.textContent = lanInfo.pin ? String(lanInfo.pin) : '------';
      }
      if (lanUrlText) {
        const urlStr = lanInfo.url ? String(lanInfo.url) : '';
        lanUrlText.textContent = urlStr;
        lanUrlText.title = urlStr;
      }
      if (lanClientsText) {
        const cnt = typeof lanInfo.client_count === 'number' ? lanInfo.client_count : 0;
        lanClientsText.textContent = t('popup.lan.clients', { count: cnt });
      }
      if (lanQrBox) {
        renderSafeQrSvg(lanQrBox, lanInfo.qr_svg || '');
      }
    } else if (!lanRunning && lanQrBox) {
      renderSafeQrSvg(lanQrBox, '');
    }
  }

  async function fetchLanStatus() {
    if (lanStatusFetchInFlight || !currentConnected) return;
    lanStatusFetchInFlight = true;
    try {
      const res = await apiRequest('/lan_companion', 'POST', { action: 'status' });
      if (res && res.ok && res.data && typeof res.data === 'object') {
        const isOn = Boolean(res.data.running || res.data.is_running);
        lanRunning = isOn;
        lanInfo = isOn ? res.data : null;
        renderLanCompanionUI();
      }
    } finally {
      lanStatusFetchInFlight = false;
    }
  }

  function switchTab(tabName) {
    if (activeRecordTarget || activeExtRecord) {
      stopRecording(null, null, false);
    }
    for (const btn of tabButtons) {
      const active = btn.getAttribute('data-tab') === tabName;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    }
    for (const [key, panel] of Object.entries(tabPanels)) {
      if (panel) {
        panel.hidden = key !== tabName;
      }
    }
    if (tabName === 'tools' && currentConnected && lanRunning && (!lanInfo || !lanInfo.qr_svg)) {
      fetchLanStatus();
    }
    if (tabName === 'settings') {
      checkHotkeyConflicts();
    }
  }

  function renderTelemetry(lastSync) {
    currentLastSync = lastSync || null;
    const valid = Boolean(
      lastSync &&
      typeof lastSync === 'object' &&
      typeof lastSync.lat === 'number' &&
      typeof lastSync.lng === 'number'
    );

    if (!valid) {
      syncFloorBadge.hidden = true;
      syncFloorBadge.classList.remove('is-underground');
      syncLocation.textContent = t('popup.telemetry.empty');
      syncLocation.classList.add('is-empty');
      syncCoordsRow.hidden = true;
      return;
    }

    const mapName = lastSync.map_name || (lastSync.map_id ? `Map ${lastSync.map_id}` : 'Teyvat');
    const areaPart = lastSync.area_name ? ` • ${lastSync.area_name}` : '';
    syncLocation.textContent = `${mapName}${areaPart}`;
    syncLocation.classList.remove('is-empty');

    syncFloorBadge.hidden = false;
    if (lastSync.is_underground) {
      syncFloorBadge.textContent = t('popup.telemetry.floor_underground', { floor: lastSync.floor || 'B1' });
      syncFloorBadge.classList.add('is-underground');
    } else {
      syncFloorBadge.textContent = (lastSync.floor && lastSync.floor !== 'G')
        ? lastSync.floor
        : t('popup.telemetry.floor_surface');
      syncFloorBadge.classList.remove('is-underground');
    }

    syncCoordsRow.hidden = false;
    syncCoords.textContent = `(${lastSync.lat.toFixed(1)}, ${lastSync.lng.toFixed(1)})`;
    syncTime.textContent = formatRelativeTime(lastSync.ts);
  }

  function renderHotkeysFromStatus(statusData) {
    if (!statusData || typeof statusData !== 'object' || hotkeysDirty || activeRecordTarget || activeExtRecord) {
      return;
    }
    const mappings = [
      { key: 'sync', raw: statusData.hotkey_sync || statusData.hotkey, disp: statusData.display_name_sync || statusData.display_name },
      { key: 'switch', raw: statusData.hotkey_switch, disp: statusData.display_name_switch },
      { key: 'debug', raw: statusData.hotkey_debug, disp: statusData.display_name_debug },
      ...Object.keys(NAV_HOTKEY_DEFAULTS).map((k) => ({
        key: k,
        raw: statusData[`hotkey_${k}`],
        disp: statusData[`display_name_${k}`]
      }))
    ];
    for (const m of mappings) {
      const rec = recorders[m.key];
      if (!rec) continue;
      if (m.raw) rec.val = String(m.raw).trim().toLowerCase();
      if (m.disp) {
        rec.displayText = String(m.disp).trim();
        rec.display.textContent = rec.displayText;
      }
      rec.box.classList.remove('is-dirty');
      rec.badge.textContent = t('popup.hotkeys.badge_change');
    }
    if (!initialConflictChecked && currentConnected) {
      initialConflictChecked = true;
      checkHotkeyConflicts();
    }
  }

  function applyState({ connected, engineActive, statusData, lastSync, tabCount }) {
    if (typeof connected === 'boolean') currentConnected = connected;
    if (typeof engineActive === 'boolean') currentEngineActive = engineActive;
    if (statusData !== undefined) currentLastStatus = statusData;
    if (typeof tabCount === 'number') cachedTabCount = tabCount;

    mapTabStatus.textContent = t('popup.footer.map_tabs', { count: cachedTabCount });

    const verRaw = (currentLastStatus && currentLastStatus.version) ? String(currentLastStatus.version) : '5.4.0';
    versionBadge.textContent = verRaw.startsWith('v') ? verRaw : `v${verRaw}`;

    statusPill.classList.remove('is-online', 'is-standby', 'is-offline');
    if (currentConnected && currentEngineActive) {
      statusPill.classList.add('is-online');
      statusText.textContent = t('popup.status.active');
    } else if (currentConnected && !currentEngineActive) {
      statusPill.classList.add('is-standby');
      statusText.textContent = t('popup.status.standby');
    } else {
      statusPill.classList.add('is-offline');
      statusText.textContent = t('popup.status.offline');
    }

    if (!powerBusy) {
      btnPowerToggle.classList.remove('is-on', 'is-standby', 'is-off');
      if (currentConnected && currentEngineActive) {
        powerTitle.textContent = t('popup.power.active.title');
        powerDesc.textContent = t('popup.power.active.desc');
        btnPowerToggle.classList.add('is-on');
        btnPowerToggle.setAttribute('aria-checked', 'true');
        powerSwitchLabel.textContent = t('popup.power.active.label');
        nativeHostBanner.hidden = true;
      } else if (currentConnected && !currentEngineActive) {
        powerTitle.textContent = t('popup.power.standby.title');
        powerDesc.textContent = t('popup.power.standby.desc');
        btnPowerToggle.classList.add('is-standby');
        btnPowerToggle.setAttribute('aria-checked', 'false');
        powerSwitchLabel.textContent = t('popup.power.standby.label');
        nativeHostBanner.hidden = true;
      } else {
        powerTitle.textContent = t('popup.power.offline.title');
        powerDesc.textContent = t('popup.power.offline.desc');
        btnPowerToggle.classList.add('is-off');
        btnPowerToggle.setAttribute('aria-checked', 'false');
        powerSwitchLabel.textContent = t('popup.power.offline.label');
      }
      btnPowerToggle.disabled = false;
    }

    btnHardQuit.disabled = !currentConnected;

    const uipiMismatch = Boolean(
      currentConnected &&
      currentLastStatus &&
      (currentLastStatus.uipi_mismatch === true || currentLastStatus.uipi_blocked === true)
    );
    if (uipiWarningBanner) {
      uipiWarningBanner.hidden = !uipiMismatch;
    }

    if (currentLastStatus && typeof currentLastStatus === 'object') {
      if (typeof currentLastStatus.auto_sync_m === 'boolean') {
        toggleAutoSync.checked = currentLastStatus.auto_sync_m;
      }
      if (toggleMinimapTracking && typeof currentLastStatus.minimap_tracking === 'boolean') {
        toggleMinimapTracking.checked = currentLastStatus.minimap_tracking;
      }
      if (toggleNavHud && typeof currentLastStatus.nav_hud_enabled === 'boolean') {
        toggleNavHud.checked = currentLastStatus.nav_hud_enabled;
      }
      if (selectHudPosition && typeof currentLastStatus.nav_hud_position === 'string' && document.activeElement !== selectHudPosition) {
        const pos = currentLastStatus.nav_hud_position;
        if (Array.from(selectHudPosition.options).some((o) => o.value === pos)) {
          selectHudPosition.value = pos;
        }
      }
      if (btnHudEdit) {
        btnHudEdit.classList.toggle('is-active', currentLastStatus.nav_hud_edit_mode === true);
      }
      if (toggleHudCapture && typeof currentLastStatus.nav_hud_capture_visible === 'boolean') {
        toggleHudCapture.checked = currentLastStatus.nav_hud_capture_visible;
      }
      if (typeof currentLastStatus.single_monitor_mode === 'boolean') {
        toggleSingleMonitor.checked = currentLastStatus.single_monitor_mode;
      }
      const monCount = typeof currentLastStatus.monitor_count === 'number' ? currentLastStatus.monitor_count : 0;
      singleMonitorHint.hidden = !(monCount === 1 && !toggleSingleMonitor.checked);
      singleMonitorTip.hidden = !toggleSingleMonitor.checked;
      renderHotkeysFromStatus(currentLastStatus);

      if (!lanBusy) {
        const nextLanOn = Boolean(
          currentConnected && (
            currentLastStatus.lan_companion_running === true ||
            (currentLastStatus.lan_companion && (currentLastStatus.lan_companion.running || currentLastStatus.lan_companion.is_running))
          )
        );
        lanRunning = nextLanOn;
        if (nextLanOn) {
          if (currentLastStatus.lan_companion && currentLastStatus.lan_companion.qr_svg) {
            lanInfo = currentLastStatus.lan_companion;
          } else if (!lanInfo || !lanInfo.qr_svg) {
            fetchLanStatus();
          }
        } else {
          lanInfo = null;
        }
      }
    } else if (!currentConnected && !lanBusy) {
      lanRunning = false;
      lanInfo = null;
    }

    renderLanCompanionUI();

    if (lastSync !== undefined) {
      renderTelemetry(lastSync);
    }
  }

  async function refreshFromBg() {
    const bgState = await sendBg({ kind: 'GET_STATE' });
    if (bgState && typeof bgState === 'object') {
      applyState({
        connected: Boolean(bgState.connected),
        engineActive: typeof bgState.engineActive === 'boolean' ? bgState.engineActive : true,
        statusData: bgState.lastStatus || null,
        lastSync: bgState.lastSync !== undefined ? bgState.lastSync : currentLastSync,
        tabCount: typeof bgState.tabCount === 'number' ? bgState.tabCount : cachedTabCount
      });
    }
    return bgState;
  }

  async function initPopup() {
    if (window.GHI18n && typeof window.GHI18n.init === 'function') {
      await window.GHI18n.init();
    }
    const storedPrefs = await readLocalStorage(['gh_compact_ui', 'gh_export_format']);
    if (toggleCompactUi) {
      toggleCompactUi.checked = Boolean(storedPrefs.gh_compact_ui);
    }
    if (selectExportFormat && typeof storedPrefs.gh_export_format === 'string') {
      const valid = ['kongying', 'appsample', 'genshinmap', 'canonical'];
      if (valid.includes(storedPrefs.gh_export_format)) {
        selectExportFormat.value = storedPrefs.gh_export_format;
      }
    }
    applyLanguageToPopup();

    const bgState = await refreshFromBg();
    const res = await apiRequest('/status', 'GET');
    if (res && res.ok && res.data) {
      if (res.data.language && window.GHI18n && !window.GHI18n.hasExplicitPreference()) {
        await window.GHI18n.setLang(res.data.language, { persist: false });
        applyLanguageToPopup();
      }
      const nextEngine = typeof res.data.engine_active === 'boolean' ? res.data.engine_active : true;
      const nextSync = Object.prototype.hasOwnProperty.call(res.data, 'last_sync')
        ? res.data.last_sync
        : (bgState ? bgState.lastSync : null);
      applyState({
        connected: true,
        engineActive: nextEngine,
        statusData: res.data,
        lastSync: nextSync,
        tabCount: cachedTabCount
      });
    } else {
      applyState({
        connected: false,
        engineActive: false,
        statusData: (bgState && bgState.lastStatus) || null,
        lastSync: (bgState && bgState.lastSync) || null,
        tabCount: cachedTabCount
      });
    }
  }

  // Smart Conflict Advisor + NVIDIA Overlay Remapper (Tab 3)
  function collectCurrentBindings() {
    const bindings = {};
    for (const act of HOTKEY_ACTIONS) {
      const rec = recorders[act];
      if (rec && rec.val) {
        bindings[act] = String(rec.val).trim().toLowerCase();
      }
    }
    return bindings;
  }

  function buildLocalDuplicateResults(bindings) {
    const byKey = new Map();
    for (const [act, hk] of Object.entries(bindings || {})) {
      if (!hk) continue;
      if (!byKey.has(hk)) byKey.set(hk, []);
      byKey.get(hk).push(act);
    }
    const actions = {};
    for (const [act, hk] of Object.entries(bindings || {})) {
      const peers = (byKey.get(hk) || []).filter((x) => x !== act);
      const conflicts = peers.map((otherAct) => ({
        kind: 'internal',
        severity: 'error',
        app_id: 'genshin_helper',
        app_name: 'Genshin Helper',
        shortcut_id: otherAct,
        can_remap: false,
        message: t('popup.hotkeys.duplicate_error', { key: hk.toUpperCase() })
      }));
      actions[act] = { hotkey: hk, ok: conflicts.length === 0, severity: conflicts.length ? 'error' : 'ok', conflicts, suggestions: [] };
    }
    return { ok: true, actions, active_backups: [] };
  }

  function renderBackupsBar(backups) {
    if (!hotkeyBackupsBar) return;
    hotkeyBackupsBar.textContent = '';
    const list = Array.isArray(backups) ? backups : [];
    if (list.length === 0) {
      hotkeyBackupsBar.hidden = true;
      return;
    }
    hotkeyBackupsBar.hidden = false;

    const titleEl = document.createElement('div');
    titleEl.className = 'backups-title';
    titleEl.textContent = t('popup.hotkeys.backups_title');
    hotkeyBackupsBar.appendChild(titleEl);

    const lang = getActiveLang();
    for (const b of list) {
      const row = document.createElement('div');
      row.className = 'backup-row';

      const desc = (lang === 'en' ? b.desc_en : b.desc_vi) || b.shortcut_id || '';
      const origDisp = b.original_display || b.original_hotkey || '';
      const curDisp = b.current_display || b.current_hotkey || '';
      const infoEl = document.createElement('span');
      infoEl.className = 'backup-info';
      infoEl.textContent = `${b.app_name || 'NVIDIA'} (${desc}): ${origDisp} -> ${curDisp}`;

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-backup-restore';
      btn.textContent = t('popup.hotkeys.restore_btn', { old_key: origDisp });
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = t('popup.hotkeys.restoring');
        const res = await apiRequest('/restore_external_hotkey', 'POST', {
          app_id: b.app_id,
          shortcut_id: b.shortcut_id
        });
        if (res && res.ok && res.data && res.data.ok) {
          showFeedback(
            t('popup.hotkeys.restore_ok', {
              old_key: res.data.restored_display || origDisp,
              app: b.app_name || 'NVIDIA Overlay'
            }),
            'success',
            3200
          );
          checkHotkeyConflicts();
        } else {
          btn.disabled = false;
          btn.textContent = t('popup.hotkeys.restore_btn', { old_key: origDisp });
          const errMsg = (res && res.data && res.data.error) || (res && res.error) || 'Error';
          showFeedback(t('popup.hotkeys.restore_err', { error: errMsg }), 'error', 3600);
        }
      });

      row.appendChild(infoEl);
      row.appendChild(btn);
      hotkeyBackupsBar.appendChild(row);
    }
  }

  function renderConflictAnalysis(data) {
    const actions = (data && data.actions && typeof data.actions === 'object') ? data.actions : {};
    for (const act of HOTKEY_ACTIONS) {
      const groupEl = document.querySelector(`.recorder-group[data-group="${act}"]`);
      if (!groupEl) continue;
      const oldPanel = groupEl.querySelector('.hotkey-conflict-panel');
      const analysis = actions[act];
      const conflicts = (analysis && Array.isArray(analysis.conflicts)) ? analysis.conflicts : [];
      if (conflicts.length === 0) {
        groupEl.classList.remove('has-conflict');
        if (oldPanel) oldPanel.remove();
        continue;
      }

      groupEl.classList.add('has-conflict');
      const panel = oldPanel || document.createElement('div');
      panel.className = 'hotkey-conflict-panel';
      panel.textContent = '';

      const suggestions = Array.isArray(analysis.suggestions) ? analysis.suggestions : [];
      for (const c of conflicts) {
        const item = document.createElement('div');
        item.className = `conflict-item ${c.severity === 'warn' ? 'is-warn' : 'is-error'}`;

        const msgEl = document.createElement('div');
        msgEl.className = 'conflict-msg';
        msgEl.textContent = c.message || '';
        item.appendChild(msgEl);

        if (c.can_remap === true) {
          const remapBox = document.createElement('div');
          remapBox.className = 'conflict-remap-box';

          const lbl = document.createElement('div');
          lbl.className = 'conflict-remap-label';
          lbl.textContent = t('popup.hotkeys.ext_move_label', {
            app: c.app_name || 'External App',
            action: c.shortcut_label || c.shortcut_id || '',
            old_key: c.current_external_display || c.current_external_hotkey || ''
          });
          remapBox.appendChild(lbl);

          const defaultExt = suggestions.find(
            (s) => s && s.hotkey && !String(s.hotkey).includes('mouse') && !['middle', 'right'].includes(String(s.hotkey))
          ) || null;
          let chosenExtKey = defaultExt ? defaultExt.hotkey : '';
          let chosenExtDisp = defaultExt ? (defaultExt.display_name || defaultExt.hotkey) : t('popup.hotkeys.ext_custom_placeholder');

          const ctrlRow = document.createElement('div');
          ctrlRow.className = 'conflict-remap-controls';

          const chipBtn = document.createElement('button');
          chipBtn.type = 'button';
          chipBtn.className = 'remap-target-chip';
          chipBtn.textContent = chosenExtDisp;
          chipBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            startExternalRecording(chipBtn, chosenExtDisp, (val, disp) => {
              chosenExtKey = val;
              chosenExtDisp = disp;
            });
          });

          const claimBtn = document.createElement('button');
          claimBtn.type = 'button';
          claimBtn.className = 'btn-conflict-remap';
          const claimLabel = t('popup.hotkeys.ext_claim_btn', { app: c.app_name || 'NVIDIA' });
          claimBtn.textContent = claimLabel;
          claimBtn.addEventListener('click', async () => {
            if (activeRecordTarget || activeExtRecord) {
              stopRecording(null, null, false);
            }
            if (!chosenExtKey) {
              showFeedback(t('popup.hotkeys.ext_need_key'), 'warn');
              return;
            }
            claimBtn.disabled = true;
            claimBtn.textContent = t('popup.hotkeys.ext_remapping');
            const rec = recorders[act];
            const res = await apiRequest('/remap_external_hotkey', 'POST', {
              app_id: c.app_id,
              shortcut_id: c.shortcut_id,
              new_external_hotkey: chosenExtKey,
              claim_action: act,
              claim_hotkey: (rec && rec.val) || '',
              bindings: collectCurrentBindings(),
              lang: getActiveLang()
            });
            claimBtn.disabled = false;
            claimBtn.textContent = claimLabel;

            if (res && res.ok && res.data && res.data.ok) {
              if (res.data.claim_updated && rec) {
                const cu = res.data.claim_updated;
                rec.val = cu.hotkey || rec.val;
                rec.displayText = cu.display_name || rec.displayText;
                rec.display.textContent = rec.displayText;
                rec.box.classList.remove('is-dirty');
                rec.badge.textContent = t('popup.hotkeys.badge_change');
                hotkeysDirty = Object.values(recorders).some((r) => r.box.classList.contains('is-dirty'));
              }
              showFeedback(
                t('popup.hotkeys.ext_remap_ok', {
                  app: c.app_name || 'NVIDIA Overlay',
                  new_key: res.data.new_display || chosenExtDisp || chosenExtKey
                }),
                'success',
                4500
              );
              checkHotkeyConflicts();
            } else {
              const errMsg = (res && res.data && res.data.error) || (res && res.error) || 'Unknown error';
              showFeedback(t('popup.hotkeys.ext_remap_err', { error: errMsg }), 'error', 4200);
            }
          });

          ctrlRow.appendChild(chipBtn);
          ctrlRow.appendChild(claimBtn);
          remapBox.appendChild(ctrlRow);
          item.appendChild(remapBox);
        }

        panel.appendChild(item);
      }

      if (suggestions.length > 0 && recorders[act]) {
        const sugRow = document.createElement('div');
        sugRow.className = 'conflict-suggest-row';
        const sugLbl = document.createElement('span');
        sugLbl.className = 'conflict-suggest-label';
        sugLbl.textContent = t('popup.hotkeys.suggest_switch');
        sugRow.appendChild(sugLbl);

        for (const sug of suggestions) {
          if (!sug || !sug.hotkey) continue;
          const pill = document.createElement('button');
          pill.type = 'button';
          pill.className = 'conflict-suggest-pill';
          pill.textContent = sug.display_name || sug.hotkey;
          pill.addEventListener('click', () => {
            if (activeRecordTarget || activeExtRecord) {
              stopRecording(null, null, false);
            }
            const rec = recorders[act];
            rec.val = sug.hotkey;
            rec.displayText = sug.display_name || sug.hotkey;
            rec.display.textContent = rec.displayText;
            rec.box.classList.add('is-dirty');
            rec.badge.textContent = t('popup.hotkeys.badge_unsaved');
            hotkeysDirty = true;
            checkHotkeyConflicts();
          });
          sugRow.appendChild(pill);
        }
        panel.appendChild(sugRow);
      }

      if (!oldPanel) {
        groupEl.appendChild(panel);
      }
    }
    renderBackupsBar(data && data.active_backups);
  }

  async function checkHotkeyConflicts() {
    const seq = ++conflictCheckSeq;
    const bindings = collectCurrentBindings();
    const res = await apiRequest('/check_hotkey_conflicts', 'POST', {
      bindings,
      lang: getActiveLang()
    });
    if (seq !== conflictCheckSeq) return;
    if (res && res.ok && res.data && res.data.actions) {
      renderConflictAnalysis(res.data);
    } else {
      renderConflictAnalysis(buildLocalDuplicateResults(bindings));
    }
  }

  // Hotkey Recorder Logic (Keyboard + Preset Pills)
  function stopRecording(newVal, newDisplay, markDirty = true) {
    if (activeExtRecord) {
      const ext = activeExtRecord;
      activeExtRecord = null;
      removeRecordingListeners();
      ext.chipEl.classList.remove('is-recording');
      if (newVal && newDisplay) {
        ext.displayText = newDisplay;
        ext.chipEl.textContent = newDisplay;
        if (typeof ext.onDone === 'function') {
          ext.onDone(newVal, newDisplay);
        }
      } else {
        ext.chipEl.textContent = ext.displayText;
      }
      return;
    }
    if (!activeRecordTarget) return;
    const target = activeRecordTarget;
    const rec = recorders[target];
    activeRecordTarget = null;
    removeRecordingListeners();
    if (!rec) return;

    rec.box.classList.remove('is-recording');
    if (newVal && newDisplay) {
      rec.val = newVal;
      rec.displayText = newDisplay;
      rec.display.textContent = newDisplay;
      if (markDirty) {
        rec.box.classList.add('is-dirty');
        rec.badge.textContent = t('popup.hotkeys.badge_unsaved');
        hotkeysDirty = true;
        checkHotkeyConflicts();
      } else {
        rec.box.classList.remove('is-dirty');
        rec.badge.textContent = t('popup.hotkeys.badge_change');
      }
    } else {
      rec.display.textContent = rec.displayText;
      rec.badge.textContent = rec.box.classList.contains('is-dirty')
        ? t('popup.hotkeys.badge_unsaved')
        : t('popup.hotkeys.badge_change');
    }
  }

  function startRecording(target) {
    if (activeRecordTarget || activeExtRecord) {
      stopRecording(null, null, false);
    }
    const rec = recorders[target];
    if (!rec) return;
    activeRecordTarget = target;
    rec.box.classList.add('is-recording');
    rec.display.textContent = t('popup.hotkeys.press_key');
    rec.badge.textContent = t('popup.hotkeys.badge_listening');
    rec.box.focus();
    addRecordingListeners();
  }

  function startExternalRecording(chipEl, currentDisplay, onDone) {
    if (activeRecordTarget || activeExtRecord) {
      stopRecording(null, null, false);
    }
    activeExtRecord = { chipEl, displayText: currentDisplay, onDone };
    chipEl.classList.add('is-recording');
    chipEl.textContent = t('popup.hotkeys.press_key');
    chipEl.focus();
    addRecordingListeners();
  }

  function onRecordKeyDown(e) {
    if (!activeRecordTarget && !activeExtRecord) return;
    e.preventDefault();
    e.stopPropagation();

    if (e.key === 'Escape') {
      stopRecording(null, null, false);
      return;
    }

    if (['Control', 'Alt', 'Shift', 'Meta', 'OS'].includes(e.key)) {
      const modsPreview = [];
      if (e.ctrlKey) modsPreview.push('Ctrl');
      if (e.altKey) modsPreview.push('Alt');
      if (e.shiftKey) modsPreview.push('Shift');
      const targetEl = activeExtRecord
        ? activeExtRecord.chipEl
        : (recorders[activeRecordTarget] ? recorders[activeRecordTarget].display : null);
      if (targetEl) {
        targetEl.textContent = `${modsPreview.join(' + ')} + ...`;
      }
      return;
    }

    const modsVal = [];
    const modsDisplay = [];
    if (e.ctrlKey) { modsVal.push('ctrl'); modsDisplay.push('Ctrl'); }
    if (e.altKey) { modsVal.push('alt'); modsDisplay.push('Alt'); }
    if (e.shiftKey) { modsVal.push('shift'); modsDisplay.push('Shift'); }

    let keyName = (e.key || '').toLowerCase();
    let keyDisplayName = (e.key || '').toUpperCase();

    if (e.code && e.code.startsWith('F') && !isNaN(parseInt(e.code.slice(1), 10))) {
      keyName = e.code.toLowerCase();
      keyDisplayName = e.code.toUpperCase();
    } else if (e.code === 'Space') {
      keyName = 'space';
      keyDisplayName = 'Space';
    } else if (e.code && e.code.startsWith('Key')) {
      keyName = e.code.slice(3).toLowerCase();
      keyDisplayName = e.code.slice(3).toUpperCase();
    } else if (e.code && e.code.startsWith('Digit')) {
      keyName = e.code.slice(5);
      keyDisplayName = keyName;
    } else if (keyName === ' ') {
      keyName = 'space';
      keyDisplayName = 'Space';
    }

    if (!keyName) return;
    modsVal.push(keyName);
    modsDisplay.push(keyDisplayName);

    stopRecording(modsVal.join('+'), modsDisplay.join(' + '), true);
  }

  function onRecordMouseDown(e) {
    if (activeExtRecord) {
      if (e.target !== activeExtRecord.chipEl) {
        stopRecording(null, null, false);
      }
      return;
    }
    if (!activeRecordTarget) return;
    if (e.button === 1 || e.button === 2 || e.button === 3 || e.button === 4) {
      e.preventDefault();
      e.stopPropagation();
      const map = {
        1: { val: 'middle', name: t('popup.hotkeys.mouse_middle') },
        2: { val: 'right', name: t('popup.hotkeys.mouse_right') },
        3: { val: 'mouse4', name: t('popup.hotkeys.mouse4') },
        4: { val: 'mouse5', name: t('popup.hotkeys.mouse5') }
      };
      const btn = map[e.button];
      if (!btn) return;
      const modsVal = [];
      const modsDisplay = [];
      if (e.ctrlKey) { modsVal.push('ctrl'); modsDisplay.push('Ctrl'); }
      if (e.altKey) { modsVal.push('alt'); modsDisplay.push('Alt'); }
      if (e.shiftKey) { modsVal.push('shift'); modsDisplay.push('Shift'); }
      modsVal.push(btn.val);
      modsDisplay.push(btn.name);
      stopRecording(modsVal.join('+'), modsDisplay.join(' + '), true);
    } else if (e.target && typeof e.target.closest === 'function') {
      const insideActiveBox = e.target.closest('.recorder-box') === recorders[activeRecordTarget].box;
      if (!insideActiveBox) {
        stopRecording(null, null, false);
      }
    }
  }

  function onRecordContextMenu(e) {
    if (activeRecordTarget) {
      e.preventDefault();
      e.stopPropagation();
    }
  }

  function addRecordingListeners() {
    window.addEventListener('keydown', onRecordKeyDown, { capture: true });
    window.addEventListener('mousedown', onRecordMouseDown, { capture: true });
    window.addEventListener('auxclick', onRecordMouseDown, { capture: true });
    window.addEventListener('contextmenu', onRecordContextMenu, { capture: true });
  }

  function removeRecordingListeners() {
    window.removeEventListener('keydown', onRecordKeyDown, { capture: true });
    window.removeEventListener('mousedown', onRecordMouseDown, { capture: true });
    window.removeEventListener('auxclick', onRecordMouseDown, { capture: true });
    setTimeout(() => {
      window.removeEventListener('contextmenu', onRecordContextMenu, { capture: true });
    }, 200);
  }

  for (const [target, rec] of Object.entries(recorders)) {
    rec.box.addEventListener('click', (e) => {
      e.stopPropagation();
      if (activeRecordTarget !== target) {
        startRecording(target);
      }
    });
  }

  for (const btn of presetButtons) {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-target');
      const key = btn.getAttribute('data-key');
      const labelKey = btn.getAttribute('data-label-i18n');
      const label = labelKey ? t(labelKey) : (btn.getAttribute('data-label') || btn.textContent.trim());
      const rec = recorders[target];
      if (!rec || !key) return;
      if (activeRecordTarget || activeExtRecord) {
        stopRecording(null, null, false);
      }
      rec.val = key;
      rec.displayText = label;
      rec.display.textContent = label;
      rec.box.classList.add('is-dirty');
      rec.badge.textContent = t('popup.hotkeys.badge_unsaved');
      hotkeysDirty = true;
      checkHotkeyConflicts();
    });
  }

  // Event Listeners
  if (btnLangToggle) {
    btnLangToggle.addEventListener('click', async (e) => {
      const clickedOpt = e.target && typeof e.target.closest === 'function'
        ? e.target.closest('.lang-opt')
        : null;
      const curLang = getActiveLang();
      const targetLang = clickedOpt
        ? clickedOpt.getAttribute('data-lang-opt')
        : (curLang === 'vi' ? 'en' : 'vi');
      if (!targetLang || targetLang === curLang) return;

      if (window.GHI18n && typeof window.GHI18n.setLang === 'function') {
        await window.GHI18n.setLang(targetLang);
      }
      applyLanguageToPopup();
      notifyMapTabs({ kind: 'GH_SET_LANG', lang: targetLang });
      if (currentConnected) {
        apiRequest('/set_language', 'POST', { language: targetLang }).catch(() => {});
        checkHotkeyConflicts();
      }
    });
  }

  if (toggleCompactUi) {
    toggleCompactUi.addEventListener('change', async () => {
      const want = Boolean(toggleCompactUi.checked);
      await writeLocalStorage({ gh_compact_ui: want });
      if (cachedTabCount === 0) {
        const re = await sendBg({ kind: 'RECONNECT_MAP_TABS' });
        if (re && typeof re.tabCount === 'number') {
          cachedTabCount = re.tabCount;
          if (mapTabStatus) {
            mapTabStatus.textContent = t('popup.footer.map_tabs', { count: cachedTabCount });
          }
        }
      }
      notifyMapTabs({ kind: 'GH_SET_COMPACT_UI', enabled: want });
    });
  }

  for (const btn of tabButtons) {
    btn.addEventListener('click', () => {
      const targetTab = btn.getAttribute('data-tab');
      if (targetTab) switchTab(targetTab);
    });
  }

  btnPowerToggle.addEventListener('click', async () => {
    if (powerBusy) return;
    const wantActive = !(currentConnected && currentEngineActive);
    powerBusy = true;
    btnPowerToggle.disabled = true;
    powerSwitchLabel.textContent = wantActive ? t('popup.power.starting') : t('popup.power.stopping');
    nativeHostBanner.hidden = true;
    showFeedback('');

    const res = await sendBg({ kind: 'SET_ENGINE_POWER', active: wantActive });
    powerBusy = false;

    if (wantActive) {
      if (res && res.ok) {
        await refreshFromBg();
        const statusRes = await apiRequest('/status', 'GET');
        if (statusRes && statusRes.ok && statusRes.data) {
          applyState({
            connected: true,
            engineActive: typeof statusRes.data.engine_active === 'boolean' ? statusRes.data.engine_active : true,
            statusData: statusRes.data,
            lastSync: Object.prototype.hasOwnProperty.call(statusRes.data, 'last_sync') ? statusRes.data.last_sync : currentLastSync
          });
        }
        showFeedback(
          res.via === 'native_host'
            ? t('popup.power.feedback.started_native')
            : t('popup.power.feedback.resumed'),
          'success',
          2600
        );
      } else if (res && res.needNativeHost) {
        applyState({ connected: false, engineActive: false });
        nativeHostBanner.hidden = false;
      } else {
        await refreshFromBg();
        showFeedback((res && res.error) || t('popup.power.feedback.start_failed'), 'error');
      }
    } else {
      if (res && res.alreadyOffline) {
        applyState({ connected: false, engineActive: false });
        showFeedback(t('popup.power.feedback.already_offline'), 'warn', 2200);
      } else {
        await refreshFromBg();
        showFeedback(t('popup.power.feedback.standby'), 'success', 2400);
      }
    }
  });

  toggleAutoSync.addEventListener('change', async () => {
    const want = Boolean(toggleAutoSync.checked);
    showFeedback('');
    const res = await apiRequest('/set_auto_sync', 'POST', { auto_sync_m: want });
    if (res && res.ok && res.data) {
      toggleAutoSync.checked = Boolean(res.data.auto_sync_m);
    } else {
      toggleAutoSync.checked = !want;
      showFeedback(t('popup.modes.offline_error'), 'error');
    }
  });

  toggleSingleMonitor.addEventListener('change', async () => {
    const want = Boolean(toggleSingleMonitor.checked);
    showFeedback('');
    const res = await apiRequest('/set_single_monitor', 'POST', { single_monitor_mode: want });
    if (res && res.ok && res.data) {
      const nextVal = Boolean(res.data.single_monitor_mode);
      toggleSingleMonitor.checked = nextVal;
      singleMonitorTip.hidden = !nextVal;
      if (nextVal) singleMonitorHint.hidden = true;
    } else {
      toggleSingleMonitor.checked = !want;
      showFeedback(t('popup.modes.offline_error'), 'error');
    }
  });

  if (toggleMinimapTracking) {
    toggleMinimapTracking.addEventListener('change', async () => {
      const want = Boolean(toggleMinimapTracking.checked);
      showFeedback('');
      const res = await apiRequest('/set_minimap_tracking', 'POST', {
        enabled: want,
        minimap_tracking: want
      });
      if (res && res.ok && res.data) {
        const nextVal = typeof res.data.minimap_tracking === 'boolean'
          ? res.data.minimap_tracking
          : Boolean(res.data.enabled);
        toggleMinimapTracking.checked = nextVal;
      } else {
        toggleMinimapTracking.checked = !want;
        showFeedback(t('popup.modes.offline_error'), 'error');
      }
    });
  }

  if (toggleNavHud) {
    toggleNavHud.addEventListener('change', async () => {
      const want = Boolean(toggleNavHud.checked);
      showFeedback('');
      const res = await apiRequest('/set_nav_hud', 'POST', {
        enabled: want,
        nav_hud_enabled: want
      });
      if (res && res.ok && res.data) {
        const nextVal = typeof res.data.nav_hud_enabled === 'boolean'
          ? res.data.nav_hud_enabled
          : Boolean(res.data.enabled);
        toggleNavHud.checked = nextVal;
      } else {
        toggleNavHud.checked = !want;
        showFeedback(t('popup.modes.offline_error'), 'error');
      }
    });
  }

  if (selectHudPosition) {
    selectHudPosition.addEventListener('change', async () => {
      const want = selectHudPosition.value;
      showFeedback('');
      const res = await apiRequest('/set_nav_hud', 'POST', { position: want, nav_hud_position: want });
      if (res && res.ok && res.data) {
        if (res.data.edit_mode === true) {
          showFeedback(t('popup.nav_hud.edit_started'), 'success', 4200);
        } else if (typeof res.data.position === 'string') {
          selectHudPosition.value = res.data.position;
        }
      } else {
        showFeedback(t('popup.modes.offline_error'), 'error');
      }
    });
  }

  if (btnHudEdit) {
    btnHudEdit.addEventListener('click', async () => {
      showFeedback('');
      const res = await apiRequest('/set_nav_hud', 'POST', { edit_mode: 'toggle' });
      if (res && res.ok && res.data) {
        const editing = res.data.edit_mode === true;
        btnHudEdit.classList.toggle('is-active', editing);
        showFeedback(t(editing ? 'popup.nav_hud.edit_started' : 'popup.nav_hud.edit_saved'), 'success', editing ? 4200 : 2200);
      } else {
        showFeedback(t('popup.modes.offline_error'), 'error');
      }
    });
  }

  if (toggleHudCapture) {
    toggleHudCapture.addEventListener('change', async () => {
      const want = Boolean(toggleHudCapture.checked);
      showFeedback('');
      const res = await apiRequest('/set_nav_hud', 'POST', {
        capture_visible: want,
        nav_hud_capture_visible: want
      });
      if (res && res.ok && res.data) {
        const nextVal = typeof res.data.nav_hud_capture_visible === 'boolean'
          ? res.data.nav_hud_capture_visible
          : (typeof res.data.capture_visible === 'boolean' ? res.data.capture_visible : want);
        toggleHudCapture.checked = nextVal;
      } else {
        toggleHudCapture.checked = !want;
        showFeedback(t('popup.modes.offline_error'), 'error');
      }
    });
  }

  if (btnPlanTsp) {
    btnPlanTsp.addEventListener('click', async () => {
      btnPlanTsp.disabled = true;
      showFeedback('');
      const res = await sendBg({ kind: 'GH_CMD_PLAN_TSP' });
      btnPlanTsp.disabled = false;
      if (res && res.ok) {
        showFeedback(t('popup.tsp.sent'), 'success', 2400);
      } else if (res && res.noTab) {
        showFeedback(t('popup.tools.need_map_tab'), 'warn', 3200);
      } else {
        showFeedback((res && res.error) || t('popup.tools.need_map_tab'), 'error');
      }
    });
  }

  if (btnClearTsp) {
    btnClearTsp.addEventListener('click', async () => {
      btnClearTsp.disabled = true;
      showFeedback('');
      const res = await sendBg({ kind: 'GH_CMD_CLEAR_TSP' });
      btnClearTsp.disabled = false;
      if (res && res.ok) {
        showFeedback(t('popup.tsp.cleared'), 'success', 2000);
      } else if (res && res.noTab) {
        showFeedback(t('popup.tools.need_map_tab'), 'warn', 3200);
      } else {
        showFeedback((res && res.error) || t('popup.tools.need_map_tab'), 'error');
      }
    });
  }

  if (selectExportFormat) {
    selectExportFormat.addEventListener('change', () => {
      writeLocalStorage({ gh_export_format: selectExportFormat.value });
    });
  }

  if (btnExportPins) {
    btnExportPins.addEventListener('click', async () => {
      const fmt = selectExportFormat ? selectExportFormat.value : 'canonical';
      btnExportPins.disabled = true;
      showFeedback('');
      const res = await sendBg({ kind: 'GH_CMD_EXPORT_PINS', targetFormat: fmt });
      btnExportPins.disabled = false;
      if (res && res.ok) {
        showFeedback(t('popup.export.started', { format: fmt }), 'success', 2800);
      } else if (res && res.noTab) {
        showFeedback(t('popup.tools.need_map_tab'), 'warn', 3200);
      } else {
        showFeedback((res && res.error) || t('popup.tools.need_map_tab'), 'error');
      }
    });
  }

  if (btnToggleLan) {
    btnToggleLan.addEventListener('click', async () => {
      if (lanBusy) return;
      const wantStart = !lanRunning;
      lanBusy = true;
      btnToggleLan.disabled = true;
      btnToggleLan.textContent = wantStart ? t('popup.lan.starting') : t('popup.lan.stopping');
      showFeedback('');

      const res = await apiRequest('/lan_companion', 'POST', { action: wantStart ? 'start' : 'stop' });
      lanBusy = false;
      btnToggleLan.disabled = false;

      if (res && res.ok && res.data && res.data.ok !== false) {
        const isOn = Boolean(res.data.running || res.data.is_running);
        lanRunning = isOn;
        lanInfo = isOn ? res.data : null;
        renderLanCompanionUI();
        showFeedback(isOn ? t('popup.lan.started') : t('popup.lan.stopped'), isOn ? 'success' : 'warn', 2600);
      } else {
        renderLanCompanionUI();
        const errText = (res && res.data && res.data.error) || (res && res.error);
        if (!currentConnected) {
          showFeedback(t('popup.modes.offline_error'), 'error');
        } else {
          showFeedback(t('popup.lan.error', { error: errText || 'Unknown error' }), 'error');
        }
      }
    });
  }

  if (btnCopyLanUrl) {
    btnCopyLanUrl.addEventListener('click', async () => {
      const urlStr = (lanInfo && lanInfo.url) ? String(lanInfo.url) : (lanUrlText ? lanUrlText.textContent : '');
      if (!urlStr) return;
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          await navigator.clipboard.writeText(urlStr);
        }
        showFeedback(t('popup.lan.copied'), 'success', 1800);
      } catch (e) {}
    });
  }

  if (btnLaunchBorderless) {
    btnLaunchBorderless.addEventListener('click', async () => {
      btnLaunchBorderless.disabled = true;
      btnLaunchBorderless.textContent = t('popup.borderless.launching');
      showFeedback('');
      const res = await apiRequest('/launch_borderless', 'POST', {});
      btnLaunchBorderless.disabled = false;
      btnLaunchBorderless.textContent = t('popup.borderless.btn');

      if (res && res.ok && res.data && (res.data.ok || res.data.launched)) {
        showFeedback(t('popup.borderless.launched'), 'success', 3000);
      } else {
        const errMsg = (res && res.data && (res.data.message || res.data.error)) || (res && res.error) || t('popup.modes.offline_error');
        showFeedback(t('popup.borderless.failed', { error: errMsg }), 'error');
      }
    });
  }

  btnOpenMap.addEventListener('click', async () => {
    btnOpenMap.disabled = true;
    await sendBg({ kind: 'OPEN_MAP_TAB' });
    window.close();
  });

  btnSwitchGame.addEventListener('click', async () => {
    btnSwitchGame.disabled = true;
    showFeedback('');
    const res = await apiRequest('/quick_switch', 'POST', {});
    btnSwitchGame.disabled = false;

    if (!res || !res.ok || !res.data) {
      showFeedback(t('popup.action.switch_offline'), 'error');
      return;
    }
    const target = res.data.target;
    if (target === 'game' || target === 'browser') {
      window.close();
    } else if (target === 'no_browser') {
      showFeedback(t('popup.action.switch_no_browser'));
    } else {
      showFeedback(t('popup.action.switch_no_game'));
    }
  });

  btnSaveHotkeys.addEventListener('click', async () => {
    if (activeRecordTarget || activeExtRecord) {
      stopRecording(null, null, false);
    }
    const payload = {};
    for (const act of HOTKEY_ACTIONS) {
      const rec = recorders[act];
      if (!rec) continue;
      payload[`hotkey_${act}`] = (rec.val || '').trim().toLowerCase();
    }

    if (Object.values(payload).some((v) => !v)) {
      showFeedback(t('popup.hotkeys.missing_error'), 'error');
      return;
    }
    const seenKeys = new Map();
    for (const [k, v] of Object.entries(payload)) {
      if (seenKeys.has(v)) {
        showFeedback(t('popup.hotkeys.duplicate_error', { key: v.toUpperCase() }), 'error');
        return;
      }
      seenKeys.set(v, k);
    }

    btnSaveHotkeys.disabled = true;
    btnSaveHotkeys.textContent = t('popup.hotkeys.saving');
    showFeedback('');

    const res = await apiRequest('/set_hotkeys', 'POST', payload);

    btnSaveHotkeys.disabled = false;
    btnSaveHotkeys.textContent = t('popup.hotkeys.save_btn');

    if (!res || !res.ok || !res.data || !res.data.results) {
      showFeedback(t('popup.hotkeys.offline_error'), 'error');
      return;
    }

    const results = res.data.results;
    const failed = [];
    for (const act of HOTKEY_ACTIONS) {
      const r = results[act];
      if (r && r.ok === false) {
        failed.push(act);
      } else if (r && r.ok) {
        const rec = recorders[act];
        if (rec) {
          rec.val = r.hotkey || rec.val;
          rec.displayText = r.display_name || rec.displayText;
          rec.display.textContent = rec.displayText;
          rec.box.classList.remove('is-dirty');
          rec.badge.textContent = t('popup.hotkeys.badge_change');
        }
      }
    }

    if (failed.length > 0) {
      showFeedback(t('popup.hotkeys.register_failed', { actions: failed.join(', ') }), 'error');
      return;
    }

    hotkeysDirty = false;
    showFeedback(t('popup.hotkeys.saved'), 'success', 2600);
    checkHotkeyConflicts();
  });

  btnTriggerDiag.addEventListener('click', async () => {
    btnTriggerDiag.disabled = true;
    btnTriggerDiag.textContent = t('popup.diag.capturing_btn');
    diagHasCustomStatus = true;
    diagStatus.classList.remove('is-success', 'is-error');
    diagStatus.textContent = t('popup.diag.capturing_status');

    const res = await apiRequest('/trigger_diagnostic', 'POST', { source: 'extension_popup' });
    btnTriggerDiag.disabled = false;
    btnTriggerDiag.textContent = t('popup.diag.btn');

    if (res && res.ok && res.data && res.data.bundle) {
      const fname = res.data.bundle.folder_name || 'diag_bundle';
      diagStatus.classList.add('is-success');
      diagStatus.textContent = t('popup.diag.saved_status', { folder: fname });
      showFeedback(t('popup.diag.saved_feedback', { folder: fname }), 'success', 3000);
    } else {
      diagStatus.classList.add('is-error');
      diagStatus.textContent = t('popup.diag.error_status');
    }
  });

  btnHardQuit.addEventListener('click', async () => {
    btnHardQuit.disabled = true;
    btnHardQuit.textContent = t('popup.quit.stopping');
    showFeedback('');

    const res = await sendBg({ kind: 'HARD_SHUTDOWN' });
    btnHardQuit.textContent = t('popup.quit.btn');
    nativeHostBanner.hidden = true;

    applyState({ connected: false, engineActive: false, statusData: null });
    if (res && res.ok) {
      showFeedback(t('popup.quit.done'), 'warn', 3000);
    } else {
      showFeedback(t('popup.quit.already_off'), 'warn', 2500);
    }
  });

  if (btnDonateToggle && donateDrawer) {
    const setDonateOpen = (open) => {
      donateDrawer.hidden = !open;
      btnDonateToggle.classList.toggle('is-open', open);
      btnDonateToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open && donateQrWrap) {
        donateQrWrap.hidden = true;
        if (btnDonateMomo) btnDonateMomo.setAttribute('aria-expanded', 'false');
        if (donateMomoSub) donateMomoSub.textContent = t('popup.donate.momo_sub');
      }
    };
    btnDonateToggle.addEventListener('click', () => {
      setDonateOpen(donateDrawer.hidden);
    });
    if (btnDonateClose) {
      btnDonateClose.addEventListener('click', () => setDonateOpen(false));
    }
    if (btnDonateMomo && donateQrWrap) {
      btnDonateMomo.addEventListener('click', () => {
        const showQr = donateQrWrap.hidden;
        donateQrWrap.hidden = !showQr;
        btnDonateMomo.setAttribute('aria-expanded', showQr ? 'true' : 'false');
        if (donateMomoSub) {
          donateMomoSub.textContent = showQr ? t('popup.donate.momo_hide') : t('popup.donate.momo_sub');
        }
      });
    }
  }

  initPopup();
  setInterval(() => {
    if (!powerBusy) {
      refreshFromBg();
    }
  }, 2000);
});
