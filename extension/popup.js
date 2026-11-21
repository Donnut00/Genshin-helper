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

function formatRelativeTime(tsVal) {
  if (typeof tsVal !== 'number' || !Number.isFinite(tsVal) || tsVal <= 0) {
    return '';
  }
  const tsMs = tsVal < 1e12 ? tsVal * 1000 : tsVal;
  const diffSec = Math.max(0, Math.floor((Date.now() - tsMs) / 1000));
  if (diffSec < 5) return 'Vừa xong';
  if (diffSec < 60) return `${diffSec} giây trước`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} phút trước`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} giờ trước`;
  try {
    return new Date(tsMs).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return '';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Header & Tabs
  const versionBadge = document.getElementById('version-badge');
  const statusPill = document.getElementById('status-pill');
  const statusText = document.getElementById('status-text');
  const tabButtons = Array.from(document.querySelectorAll('.tab-btn'));
  const tabPanels = {
    control: document.getElementById('tab-panel-control'),
    settings: document.getElementById('tab-panel-settings')
  };

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
  const toggleSingleMonitor = document.getElementById('toggle-single-monitor');
  const singleMonitorHint = document.getElementById('single-monitor-hint');
  const singleMonitorTip = document.getElementById('single-monitor-tip');

  const btnOpenMap = document.getElementById('btn-open-map');
  const btnSwitchGame = document.getElementById('btn-switch-game');

  // Tab 2: Hotkeys & Diagnostics
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
  const presetButtons = Array.from(document.querySelectorAll('.preset-btn'));
  const btnSaveHotkeys = document.getElementById('btn-save-hotkeys');
  const btnTriggerDiag = document.getElementById('btn-trigger-diag');
  const diagStatus = document.getElementById('diag-status');
  const btnHardQuit = document.getElementById('btn-hard-quit');

  // Global feedback & footer
  const feedbackBar = document.getElementById('popup-feedback');
  const mapTabStatus = document.getElementById('map-tab-status');

  // Local state
  let currentConnected = false;
  let currentEngineActive = true;
  let currentLastStatus = null;
  let currentLastSync = null;
  let cachedTabCount = 0;
  let powerBusy = false;
  let hotkeysDirty = false;
  let activeRecordTarget = null;
  let feedbackTimer = null;

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

  function switchTab(tabName) {
    if (activeRecordTarget) {
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
      syncLocation.textContent = 'Chưa có dữ liệu — Mở Map (M) trong game';
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
      syncFloorBadge.textContent = `Ngầm • ${lastSync.floor || 'B1'}`;
      syncFloorBadge.classList.add('is-underground');
    } else {
      syncFloorBadge.textContent = (lastSync.floor && lastSync.floor !== 'G') ? lastSync.floor : 'Mặt đất';
      syncFloorBadge.classList.remove('is-underground');
    }

    syncCoordsRow.hidden = false;
    syncCoords.textContent = `(${lastSync.lat.toFixed(1)}, ${lastSync.lng.toFixed(1)})`;
    syncTime.textContent = formatRelativeTime(lastSync.ts);
  }

  function renderHotkeysFromStatus(statusData) {
    if (!statusData || typeof statusData !== 'object' || hotkeysDirty || activeRecordTarget) {
      return;
    }
    const mappings = [
      { key: 'sync', raw: statusData.hotkey_sync || statusData.hotkey, disp: statusData.display_name_sync || statusData.display_name },
      { key: 'switch', raw: statusData.hotkey_switch, disp: statusData.display_name_switch },
      { key: 'debug', raw: statusData.hotkey_debug, disp: statusData.display_name_debug }
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
      rec.badge.textContent = 'Đổi phím';
    }
  }

  function applyState({ connected, engineActive, statusData, lastSync, tabCount }) {
    if (typeof connected === 'boolean') currentConnected = connected;
    if (typeof engineActive === 'boolean') currentEngineActive = engineActive;
    if (statusData !== undefined) currentLastStatus = statusData;
    if (typeof tabCount === 'number') cachedTabCount = tabCount;

    mapTabStatus.textContent = `Tab Map đang kết nối: ${cachedTabCount}`;

    const verRaw = (currentLastStatus && currentLastStatus.version) ? String(currentLastStatus.version) : '4.5.0';
    versionBadge.textContent = verRaw.startsWith('v') ? verRaw : `v${verRaw}`;

    statusPill.classList.remove('is-online', 'is-standby', 'is-offline');
    if (currentConnected && currentEngineActive) {
      statusPill.classList.add('is-online');
      statusText.textContent = 'Active';
    } else if (currentConnected && !currentEngineActive) {
      statusPill.classList.add('is-standby');
      statusText.textContent = 'Standby';
    } else {
      statusPill.classList.add('is-offline');
      statusText.textContent = 'Offline';
    }

    if (!powerBusy) {
      btnPowerToggle.classList.remove('is-on', 'is-standby', 'is-off');
      if (currentConnected && currentEngineActive) {
        powerTitle.textContent = 'Engine đang hoạt động';
        powerDesc.textContent = 'Theo dõi vị trí & phím tắt thời gian thực';
        btnPowerToggle.classList.add('is-on');
        btnPowerToggle.setAttribute('aria-checked', 'true');
        powerSwitchLabel.textContent = 'BẬT';
        nativeHostBanner.hidden = true;
      } else if (currentConnected && !currentEngineActive) {
        powerTitle.textContent = 'Chế độ tạm nghỉ (Standby)';
        powerDesc.textContent = '0% CPU — Nhấn BẬT để tiếp tục đồng bộ';
        btnPowerToggle.classList.add('is-standby');
        btnPowerToggle.setAttribute('aria-checked', 'false');
        powerSwitchLabel.textContent = 'NGHỈ';
        nativeHostBanner.hidden = true;
      } else {
        powerTitle.textContent = 'Desktop App chưa chạy';
        powerDesc.textContent = 'Nhấn BẬT để tự mở qua Native Host';
        btnPowerToggle.classList.add('is-off');
        btnPowerToggle.setAttribute('aria-checked', 'false');
        powerSwitchLabel.textContent = 'TẮT';
      }
      btnPowerToggle.disabled = false;
    }

    btnHardQuit.disabled = !currentConnected;

    if (currentLastStatus && typeof currentLastStatus === 'object') {
      if (typeof currentLastStatus.auto_sync_m === 'boolean') {
        toggleAutoSync.checked = currentLastStatus.auto_sync_m;
      }
      if (typeof currentLastStatus.single_monitor_mode === 'boolean') {
        toggleSingleMonitor.checked = currentLastStatus.single_monitor_mode;
      }
      const monCount = typeof currentLastStatus.monitor_count === 'number' ? currentLastStatus.monitor_count : 0;
      singleMonitorHint.hidden = !(monCount === 1 && !toggleSingleMonitor.checked);
      singleMonitorTip.hidden = !toggleSingleMonitor.checked;
      renderHotkeysFromStatus(currentLastStatus);
    }

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
    const bgState = await refreshFromBg();
    const res = await apiRequest('/status', 'GET');
    if (res && res.ok && res.data) {
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

  // Hotkey Recorder Logic (Keyboard + Preset Pills)
  function stopRecording(newVal, newDisplay, markDirty = true) {
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
        rec.badge.textContent = 'Chưa lưu';
        hotkeysDirty = true;
      } else {
        rec.box.classList.remove('is-dirty');
        rec.badge.textContent = 'Đổi phím';
      }
    } else {
      rec.display.textContent = rec.displayText;
      rec.badge.textContent = rec.box.classList.contains('is-dirty') ? 'Chưa lưu' : 'Đổi phím';
    }
  }

  function startRecording(target) {
    if (activeRecordTarget) {
      stopRecording(null, null, false);
    }
    const rec = recorders[target];
    if (!rec) return;
    activeRecordTarget = target;
    rec.box.classList.add('is-recording');
    rec.display.textContent = 'Nhấn phím (Esc để hủy)...';
    rec.badge.textContent = 'Đang nghe';
    rec.box.focus();
    addRecordingListeners();
  }

  function onRecordKeyDown(e) {
    if (!activeRecordTarget) return;
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
      const rec = recorders[activeRecordTarget];
      if (rec) {
        rec.display.textContent = `${modsPreview.join(' + ')} + ...`;
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
    if (!activeRecordTarget) return;
    if (e.button === 1 || e.button === 2 || e.button === 3 || e.button === 4) {
      e.preventDefault();
      e.stopPropagation();
      const map = {
        1: { val: 'middle', name: 'Chuột giữa (Con lăn)' },
        2: { val: 'right', name: 'Chuột phải' },
        3: { val: 'mouse4', name: 'Mouse 4 (Chuột hông)' },
        4: { val: 'mouse5', name: 'Mouse 5 (Chuột hông)' }
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
      const label = btn.getAttribute('data-label') || btn.textContent.trim();
      const rec = recorders[target];
      if (!rec || !key) return;
      if (activeRecordTarget) {
        stopRecording(null, null, false);
      }
      rec.val = key;
      rec.displayText = label;
      rec.display.textContent = label;
      rec.box.classList.add('is-dirty');
      rec.badge.textContent = 'Chưa lưu';
      hotkeysDirty = true;
    });
  }

  // Event Listeners
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
    powerSwitchLabel.textContent = wantActive ? 'Đang bật...' : 'Đang tắt...';
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
          res.via === 'native_host' ? '🚀 Đã khởi động Desktop App và bật Engine!' : '⚡ Engine đã hoạt động trở lại!',
          'success',
          2600
        );
      } else if (res && res.needNativeHost) {
        applyState({ connected: false, engineActive: false });
        nativeHostBanner.hidden = false;
      } else {
        await refreshFromBg();
        showFeedback((res && res.error) || 'Không thể bật Desktop App.', 'error');
      }
    } else {
      if (res && res.alreadyOffline) {
        applyState({ connected: false, engineActive: false });
        showFeedback('Desktop App hiện đã tắt sẵn.', 'warn', 2200);
      } else {
        await refreshFromBg();
        showFeedback('💤 Engine đã chuyển sang Standby (0% CPU).', 'success', 2400);
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
      showFeedback('Không thể cập nhật: Desktop App chưa chạy.', 'error');
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
      showFeedback('Không thể cập nhật: Desktop App chưa chạy.', 'error');
    }
  });

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
      showFeedback('Chưa kết nối Desktop App (127.0.0.1:8765).', 'error');
      return;
    }
    const target = res.data.target;
    if (target === 'game' || target === 'browser') {
      window.close();
    } else if (target === 'no_browser') {
      showFeedback('Không tìm thấy cửa sổ trình duyệt HoYoLAB Map.');
    } else {
      showFeedback('Không tìm thấy cửa sổ game Genshin Impact đang mở.');
    }
  });

  btnSaveHotkeys.addEventListener('click', async () => {
    if (activeRecordTarget) {
      stopRecording(null, null, false);
    }
    const hotkeySync = (recorders.sync.val || '').trim().toLowerCase();
    const hotkeySwitch = (recorders.switch.val || '').trim().toLowerCase();
    const hotkeyDebug = (recorders.debug.val || '').trim().toLowerCase();

    if (!hotkeySync || !hotkeySwitch || !hotkeyDebug) {
      showFeedback('Vui lòng chọn đủ phím tắt cho cả 3 tính năng.', 'error');
      return;
    }

    btnSaveHotkeys.disabled = true;
    btnSaveHotkeys.textContent = 'Đang lưu...';
    showFeedback('');

    const res = await apiRequest('/set_hotkeys', 'POST', {
      hotkey_sync: hotkeySync,
      hotkey_debug: hotkeyDebug,
      hotkey_switch: hotkeySwitch
    });

    btnSaveHotkeys.disabled = false;
    btnSaveHotkeys.textContent = '💾 Lưu phím tắt';

    if (!res || !res.ok || !res.data || !res.data.results) {
      showFeedback('Không thể lưu phím tắt: Desktop App chưa chạy.', 'error');
      return;
    }

    const results = res.data.results;
    const failed = [];
    for (const act of ['sync', 'debug', 'switch']) {
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
          rec.badge.textContent = 'Đổi phím';
        }
      }
    }

    if (failed.length > 0) {
      showFeedback(`Không thể đăng ký phím tắt (${failed.join(', ')}). Hãy thử phím khác!`, 'error');
      return;
    }

    hotkeysDirty = false;
    showFeedback('✅ Đã lưu & áp dụng phím tắt mới!', 'success', 2600);
  });

  btnTriggerDiag.addEventListener('click', async () => {
    btnTriggerDiag.disabled = true;
    btnTriggerDiag.textContent = '📸 Đang chụp...';
    diagStatus.classList.remove('is-success', 'is-error');
    diagStatus.textContent = 'Đang chụp Game + Web Map và gom log...';

    const res = await apiRequest('/trigger_diagnostic', 'POST', { source: 'extension_popup' });
    btnTriggerDiag.disabled = false;
    btnTriggerDiag.textContent = '📸 Chụp Chẩn Đoán Ngay';

    if (res && res.ok && res.data && res.data.bundle) {
      const fname = res.data.bundle.folder_name || 'diag_bundle';
      diagStatus.classList.add('is-success');
      diagStatus.textContent = `✅ Đã lưu: ${fname}`;
      showFeedback(`📸 Đã chụp chẩn đoán: ${fname}`, 'success', 3000);
    } else {
      diagStatus.classList.add('is-error');
      diagStatus.textContent = '❌ Không thể chụp — kiểm tra Desktop App đang chạy.';
    }
  });

  btnHardQuit.addEventListener('click', async () => {
    btnHardQuit.disabled = true;
    btnHardQuit.textContent = 'Đang thoát...';
    showFeedback('');

    const res = await sendBg({ kind: 'HARD_SHUTDOWN' });
    btnHardQuit.textContent = '⏻ Thoát hẳn Desktop App';
    nativeHostBanner.hidden = true;

    applyState({ connected: false, engineActive: false, statusData: null });
    if (res && res.ok) {
      showFeedback('⏻ Đã thoát hoàn toàn Desktop App.', 'warn', 3000);
    } else {
      showFeedback('Desktop App hiện đang tắt.', 'warn', 2500);
    }
  });

  initPopup();
  setInterval(() => {
    if (!powerBusy) {
      refreshFromBg();
    }
  }, 2000);
});
