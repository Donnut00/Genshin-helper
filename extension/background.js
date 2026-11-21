'use strict';

const API_BASE = 'http://127.0.0.1:8765';
const ALARM_NAME = 'gh-keepalive-poll';
const MAP_DEFAULT_URL = 'https://act.hoyolab.com/ys/app/interactive-map/index.html#/map/2';
const NATIVE_HOST_NAME = 'com.genshinhelper.host';

const ports = new Set();
let lastEventSeq = -1;
let connected = false;
let engineActive = true;
let lastStatus = null;
let lastSync = null;
let pollInFlight = false;
let pollTimer = null;
let pollAbortController = null;
let pollGeneration = 0;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function updateBadge(isConnected, isEngineActive = engineActive) {
  try {
    let text = 'OFF';
    let color = '#dc2626';
    if (isConnected) {
      if (isEngineActive) {
        text = 'ON';
        color = '#16a34a';
      } else {
        text = 'ZZZ';
        color = '#d97706';
      }
    }
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
    if (typeof chrome.action.setBadgeTextColor === 'function') {
      chrome.action.setBadgeTextColor({ color: '#ffffff' });
    }
  } catch (e) {}
}

function broadcastToTabs(payload) {
  for (const port of Array.from(ports)) {
    try {
      port.postMessage(payload);
    } catch (e) {
      ports.delete(port);
    }
  }
}

function setConnectionState(nextConnected) {
  const normalized = Boolean(nextConnected);
  const changed = connected !== normalized;
  connected = normalized;
  updateBadge(connected, engineActive);
  if (changed) {
    broadcastToTabs({ kind: 'CONNECTION_STATUS', connected, engineActive });
  }
}

function setEngineActiveState(nextActive) {
  const normalized = Boolean(nextActive);
  const changed = engineActive !== normalized;
  engineActive = normalized;
  if (lastStatus && typeof lastStatus === 'object') {
    lastStatus = { ...lastStatus, engine_active: engineActive };
  }
  updateBadge(connected, engineActive);
  if (changed) {
    broadcastToTabs({ kind: 'ENGINE_STATE', engineActive });
  }
}

function syncStatusCacheFromEvent(data) {
  if (!data || !data.type) return;
  if (!lastStatus) lastStatus = {};

  if (data.type === 'AUTO_SYNC_UPDATED' && typeof data.auto_sync_m === 'boolean') {
    lastStatus = { ...lastStatus, auto_sync_m: data.auto_sync_m };
  } else if (data.type === 'SINGLE_MONITOR_UPDATED' && typeof data.single_monitor_mode === 'boolean') {
    lastStatus = { ...lastStatus, single_monitor_mode: data.single_monitor_mode };
  } else if (data.type === 'ENGINE_STATE_UPDATED' && typeof data.engine_active === 'boolean') {
    setEngineActiveState(data.engine_active);
  } else if (data.type === 'HOTKEY_UPDATED') {
    const act = data.action || 'sync';
    const next = { ...lastStatus };
    if (data.hotkey) next[`hotkey_${act}`] = data.hotkey;
    if (data.display_name) next[`display_name_${act}`] = data.display_name;
    lastStatus = next;
  } else if ((data.type === 'SYNC_POSITION_ONLY' || data.type === 'SYNC_AND_MARK') && data.has_world_coords === true) {
    lastSync = {
      map_id: data.map_id ?? 2,
      map_name: data.map_name || '',
      area_name: data.area_name || '',
      lat: data.lat,
      lng: data.lng,
      is_underground: Boolean(data.is_underground),
      floor: data.floor_short || data.floor_name || 'G',
      ts: Date.now() / 1000
    };
  }
}

function syncStatusCacheFromApi(path, ok, data) {
  if (!ok || !data || typeof data !== 'object') return;
  if (path.startsWith('/status')) {
    lastStatus = data;
    if (typeof data.engine_active === 'boolean') {
      engineActive = data.engine_active;
    }
    if (Object.prototype.hasOwnProperty.call(data, 'last_sync')) {
      lastSync = data.last_sync || null;
    }
    if (typeof data.latest_seq === 'number') {
      if (lastEventSeq < 0 || data.latest_seq < lastEventSeq) {
        lastEventSeq = data.latest_seq;
      }
    }
  } else if (path.startsWith('/set_engine_state') && typeof data.engine_active === 'boolean') {
    engineActive = data.engine_active;
    lastStatus = { ...(lastStatus || {}), engine_active: engineActive };
  } else if (path.startsWith('/set_auto_sync') && typeof data.auto_sync_m === 'boolean') {
    lastStatus = { ...(lastStatus || {}), auto_sync_m: data.auto_sync_m };
  } else if (path.startsWith('/set_single_monitor') && typeof data.single_monitor_mode === 'boolean') {
    lastStatus = { ...(lastStatus || {}), single_monitor_mode: data.single_monitor_mode };
  } else if (path.startsWith('/set_hotkey') && data.results && typeof data.results === 'object') {
    const next = { ...(lastStatus || {}) };
    for (const act of ['sync', 'debug', 'switch']) {
      const r = data.results[act];
      if (r && r.ok) {
        if (r.hotkey) next[`hotkey_${act}`] = r.hotkey;
        if (r.display_name) next[`display_name_${act}`] = r.display_name;
      }
    }
    lastStatus = next;
  }
}

async function fetchApi(path, method = 'GET', body = undefined, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const fetchOpts = {
      method: method || 'GET',
      cache: 'no-store',
      signal: controller.signal
    };
    if (body !== undefined && fetchOpts.method !== 'GET' && fetchOpts.method !== 'HEAD') {
      fetchOpts.headers = { 'Content-Type': 'application/json' };
      fetchOpts.body = typeof body === 'string' ? body : JSON.stringify(body);
    }

    const res = await fetch(`${API_BASE}${path}`, fetchOpts);
    const text = await res.text();
    let parsed = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch (e) {
        parsed = { raw: text };
      }
    }

    syncStatusCacheFromApi(path, res.ok, parsed);
    setConnectionState(true);
    return {
      ok: res.ok,
      status: res.status,
      data: parsed,
      error: res.ok ? null : (parsed && parsed.error) || `HTTP ${res.status}`
    };
  } catch (err) {
    setConnectionState(false);
    return {
      ok: false,
      status: 0,
      data: null,
      error: (err && err.message) ? err.message : 'Network error'
    };
  } finally {
    clearTimeout(timer);
  }
}

function sendNativeHostMessage(payload) {
  return new Promise((resolve) => {
    try {
      if (typeof chrome === 'undefined' || !chrome.runtime || typeof chrome.runtime.sendNativeMessage !== 'function') {
        resolve({ ok: false, error: 'Native messaging API unavailable', lastError: true });
        return;
      }
      chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, payload, (response) => {
        const err = chrome.runtime.lastError;
        if (err) {
          resolve({ ok: false, error: err.message || 'Native host error', lastError: true });
          return;
        }
        if (!response || typeof response !== 'object') {
          resolve({ ok: false, error: 'Empty native host response' });
          return;
        }
        resolve(response);
      });
    } catch (e) {
      resolve({ ok: false, error: (e && e.message) ? e.message : 'sendNativeMessage failed', lastError: true });
    }
  });
}

async function refreshStatusQuietly() {
  return fetchApi('/status', 'GET', undefined, 2500);
}

async function handleSetEnginePower(active) {
  const wantActive = Boolean(active);

  if (!wantActive) {
    const res = await fetchApi('/set_engine_state', 'POST', { active: false }, 2500);
    if (res && res.ok) {
      setEngineActiveState(Boolean(res.data && typeof res.data.engine_active === 'boolean' ? res.data.engine_active : false));
      return { ok: true, engine_active: engineActive, via: 'http' };
    }
    engineActive = false;
    updateBadge(connected, false);
    return { ok: true, engine_active: false, alreadyOffline: true };
  }

  // active === true: first try HTTP wake from Standby
  const httpRes = await fetchApi('/set_engine_state', 'POST', { active: true }, 2500);
  if (httpRes && httpRes.ok) {
    setEngineActiveState(Boolean(httpRes.data && typeof httpRes.data.engine_active === 'boolean' ? httpRes.data.engine_active : true));
    await refreshStatusQuietly();
    ensurePolling();
    return { ok: true, engine_active: engineActive, via: 'http' };
  }

  // HTTP failed (Desktop App completely closed) -> launch via Native Messaging Host
  const nativeRes = await sendNativeHostMessage({ cmd: 'start_server' });
  if (!nativeRes || !nativeRes.ok) {
    return {
      ok: false,
      needNativeHost: true,
      error: (nativeRes && nativeRes.error) ? nativeRes.error : 'Native host unavailable'
    };
  }

  // Wait briefly for HTTP server on 127.0.0.1:8765 to accept connections
  let statusRes = null;
  for (let attempt = 0; attempt < 6; attempt++) {
    await sleep(attempt === 0 ? 300 : 450);
    statusRes = await refreshStatusQuietly();
    if (statusRes && statusRes.ok) {
      break;
    }
  }

  if (statusRes && statusRes.ok) {
    if (statusRes.data && statusRes.data.engine_active === false) {
      await fetchApi('/set_engine_state', 'POST', { active: true }, 2500);
    } else {
      setEngineActiveState(true);
    }
    ensurePolling();
    return { ok: true, engine_active: true, via: 'native_host' };
  }

  return {
    ok: false,
    error: 'Đã khởi động app nhưng server HTTP chưa kịp phản hồi, hãy thử lại sau vài giây.'
  };
}

async function handleHardShutdown(preferNative = false) {
  stopPolling();
  let via = null;
  let httpRes = null;
  let nativeRes = null;

  if (!preferNative) {
    httpRes = await fetchApi('/shutdown', 'POST', {}, 3000);
    if (httpRes && httpRes.ok) {
      via = 'http';
    }
  }

  if (!via) {
    nativeRes = await sendNativeHostMessage({ cmd: 'stop_server', mode: 'shutdown' });
    if (nativeRes && nativeRes.ok) {
      via = 'native_host';
    }
  }

  connected = false;
  engineActive = false;
  lastEventSeq = -1;
  if (lastStatus && typeof lastStatus === 'object') {
    lastStatus = { ...lastStatus, engine_active: false };
  }
  updateBadge(false, false);
  broadcastToTabs({ kind: 'CONNECTION_STATUS', connected: false, engineActive: false });

  if (ports.size > 0) {
    setTimeout(() => {
      if (ports.size > 0 && !connected) {
        ensurePolling();
      }
    }, 2000);
  }

  if (via) {
    return { ok: true, via };
  }
  return {
    ok: false,
    alreadyOffline: true,
    error: (nativeRes && nativeRes.error) || (httpRes && httpRes.error) || 'Desktop app is already offline'
  };
}

function stopPolling() {
  pollGeneration += 1;
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (pollAbortController) {
    try {
      pollAbortController.abort();
    } catch (e) {}
    pollAbortController = null;
  }
  pollInFlight = false;
}

function scheduleNextPoll(gen, delayMs) {
  if (gen !== pollGeneration || ports.size === 0) return;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => {
    pollTimer = null;
    pollLoop(gen);
  }, delayMs);
}

async function pollLoop(gen) {
  if (gen !== pollGeneration || ports.size === 0 || pollInFlight) return;
  pollInFlight = true;

  const controller = new AbortController();
  pollAbortController = controller;
  const timeoutId = setTimeout(() => controller.abort(), 20000);

  let nextDelay = 20;
  try {
    const res = await fetch(`${API_BASE}/poll?since=${lastEventSeq}`, {
      cache: 'no-store',
      signal: controller.signal
    });
    if (gen !== pollGeneration) return;
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();
    if (gen !== pollGeneration) return;

    const wasConnected = connected;

    if (data && typeof data._seq === 'number') {
      if (data.type === 'HEARTBEAT') {
        if (lastEventSeq < 0 || data._seq < lastEventSeq) {
          lastEventSeq = data._seq;
        }
      } else {
        lastEventSeq = data._seq;
      }
    }

    if (data && data.type === 'SHUTDOWN') {
      engineActive = false;
      setConnectionState(false);
      lastEventSeq = -1;
      nextDelay = 1000;
    } else {
      if (data && typeof data.engine_active === 'boolean') {
        engineActive = data.engine_active;
      }
      if (data && data.type && data.type !== 'HEARTBEAT') {
        syncStatusCacheFromEvent(data);
        setConnectionState(true);
        broadcastToTabs({ kind: 'SERVER_EVENT', data });
      } else {
        setConnectionState(true);
      }
      if (!wasConnected) {
        refreshStatusQuietly();
      }
    }
  } catch (err) {
    if (gen !== pollGeneration) return;
    setConnectionState(false);
    nextDelay = 1000;
  } finally {
    clearTimeout(timeoutId);
    if (pollAbortController === controller) {
      pollAbortController = null;
    }
    pollInFlight = false;
  }

  if (gen === pollGeneration && ports.size > 0) {
    scheduleNextPoll(gen, nextDelay);
  }
}

function ensurePolling() {
  if (ports.size === 0) return;
  if (pollInFlight || pollTimer) return;
  const gen = ++pollGeneration;
  pollLoop(gen);
}

function ensureAlarm() {
  try {
    chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
  } catch (e) {}
}

chrome.runtime.onConnect.addListener((port) => {
  if (!port || port.name !== 'gh-map-tab') return;
  const isFirstTab = ports.size === 0;
  ports.add(port);

  try {
    port.postMessage({ kind: 'CONNECTION_STATUS', connected, engineActive });
  } catch (e) {}

  port.onMessage.addListener((msg) => {
    if (msg && msg.type === 'PING') {
      ensurePolling();
    }
  });

  port.onDisconnect.addListener(() => {
    ports.delete(port);
    if (ports.size === 0) {
      stopPolling();
    }
  });

  if (isFirstTab && !lastStatus) {
    refreshStatusQuietly().finally(() => ensurePolling());
  } else {
    ensurePolling();
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return false;

  if (msg.kind === 'API_REQUEST') {
    const path = typeof msg.path === 'string' ? msg.path : '';
    if (!path.startsWith('/')) {
      sendResponse({ ok: false, status: 0, data: null, error: 'Invalid API path' });
      return false;
    }
    const method = (msg.method || 'GET').toUpperCase();
    const timeoutMs = path.startsWith('/trigger_diagnostic') ? 25000 : 12000;
    fetchApi(path, method, msg.body, timeoutMs).then(sendResponse);
    return true;
  }

  if (msg.kind === 'GET_STATE') {
    sendResponse({
      connected,
      engineActive,
      lastStatus,
      lastSync,
      tabCount: ports.size
    });
    return false;
  }

  if (msg.kind === 'SET_ENGINE_POWER') {
    handleSetEnginePower(Boolean(msg.active)).then(sendResponse);
    return true;
  }

  if (msg.kind === 'HARD_SHUTDOWN') {
    handleHardShutdown(Boolean(msg.preferNative)).then(sendResponse);
    return true;
  }

  if (msg.kind === 'OPEN_MAP_TAB') {
    chrome.tabs.query({ url: 'https://act.hoyolab.com/*' }, (tabs) => {
      if (chrome.runtime.lastError) {
        sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      const mapTab = Array.isArray(tabs)
        ? tabs.find((t) => t && typeof t.url === 'string' && t.url.includes('/interactive-map/'))
        : null;
      if (mapTab && typeof mapTab.id === 'number') {
        chrome.windows.update(mapTab.windowId, { focused: true }, () => {
          void chrome.runtime.lastError;
          chrome.tabs.update(mapTab.id, { active: true }, () => {
            const err = chrome.runtime.lastError;
            sendResponse(err ? { ok: false, error: err.message } : { ok: true, created: false, tabId: mapTab.id });
          });
        });
      } else {
        chrome.tabs.create({ url: MAP_DEFAULT_URL, active: true }, (created) => {
          const err = chrome.runtime.lastError;
          sendResponse(err ? { ok: false, error: err.message } : { ok: true, created: true, tabId: created ? created.id : null });
        });
      }
    });
    return true;
  }

  return false;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (!alarm || alarm.name !== ALARM_NAME) return;
  if (ports.size > 0) {
    ensurePolling();
  } else {
    refreshStatusQuietly();
  }
});

chrome.runtime.onInstalled.addListener(() => {
  updateBadge(false, false);
  ensureAlarm();
  refreshStatusQuietly();
});

chrome.runtime.onStartup.addListener(() => {
  updateBadge(false, false);
  ensureAlarm();
  refreshStatusQuietly();
});

updateBadge(false, false);
ensureAlarm();
refreshStatusQuietly();
