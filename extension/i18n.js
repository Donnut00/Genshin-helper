(function (globalScope) {
  'use strict';

  const PINNED_EXT_ID = 'nfkijdoblmphmdljfogooajhbldkeojh';
  const STORAGE_KEY_LANG = 'gh_lang';
  const STORAGE_KEY_COMPACT = 'gh_compact_ui';
  const SUPPORTED_LANGS = ['vi', 'en'];
  const DEFAULT_LANG = 'en';

  function normalizeLang(raw) {
    if (!raw || typeof raw !== 'string') return null;
    const cleaned = raw.trim().toLowerCase().replace(/_/g, '-');
    if (!cleaned || cleaned === 'auto' || cleaned === 'system' || cleaned === 'default') {
      return null;
    }
    if (cleaned === 'vi' || cleaned.startsWith('vi-') || cleaned.startsWith('vietnamese')) {
      return 'vi';
    }
    if (cleaned === 'en' || cleaned.startsWith('en-') || cleaned.startsWith('english')) {
      return 'en';
    }
    return null;
  }

  function detectBrowserLang() {
    try {
      if (typeof navigator !== 'undefined') {
        const list = Array.isArray(navigator.languages) && navigator.languages.length > 0
          ? navigator.languages
          : [navigator.language || ''];
        for (const item of list) {
          const norm = normalizeLang(item);
          if (norm) return norm;
        }
      }
    } catch (e) {}
    return DEFAULT_LANG;
  }

  function resolveLanguage(setting) {
    const explicit = normalizeLang(setting);
    if (explicit) return explicit;
    return detectBrowserLang();
  }

  function interpolate(template, params) {
    if (!params || typeof params !== 'object') return template;
    return String(template).replace(/\{(\w+)\}/g, (match, name) => {
      if (Object.prototype.hasOwnProperty.call(params, name) && params[name] !== undefined && params[name] !== null) {
        return String(params[name]);
      }
      return match;
    });
  }

  function createI18n(initialBundles, initialLang) {
    let bundles = {
      vi: (initialBundles && initialBundles.vi) ? { ...initialBundles.vi } : {},
      en: (initialBundles && initialBundles.en) ? { ...initialBundles.en } : {}
    };
    let currentLang = resolveLanguage(initialLang);
    let ready = Boolean(Object.keys(bundles.vi).length > 0 || Object.keys(bundles.en).length > 0);
    const listeners = new Set();

    function notify() {
      for (const fn of Array.from(listeners)) {
        try {
          fn(currentLang);
        } catch (e) {}
      }
    }

    function setBundles(nextBundles) {
      if (!nextBundles || typeof nextBundles !== 'object') return;
      if (nextBundles.vi && typeof nextBundles.vi === 'object') {
        bundles.vi = { ...nextBundles.vi };
      }
      if (nextBundles.en && typeof nextBundles.en === 'object') {
        bundles.en = { ...nextBundles.en };
      }
      ready = Boolean(Object.keys(bundles.vi).length > 0 || Object.keys(bundles.en).length > 0);
      notify();
    }

    function t(key, params, overrideLang) {
      if (typeof key !== 'string') return String(key);
      const lang = overrideLang ? resolveLanguage(overrideLang) : currentLang;
      const primary = bundles[lang] || {};
      const fallback = bundles[DEFAULT_LANG] || {};

      let val = primary[key];
      if (typeof val !== 'string' || val.trim() === '') {
        val = fallback[key];
      }
      if (typeof val !== 'string' || val.trim() === '') {
        val = key;
      }
      return interpolate(val, params);
    }

    function getLang() {
      return currentLang;
    }

    function setLangSync(nextLang) {
      const resolved = resolveLanguage(nextLang);
      const changed = resolved !== currentLang;
      currentLang = resolved;
      if (changed) notify();
      return currentLang;
    }

    function applyDom(root) {
      const target = root || (typeof document !== 'undefined' ? document : null);
      if (!target || typeof target.querySelectorAll !== 'function') return;

      const textEls = target.querySelectorAll('[data-i18n]');
      for (const el of textEls) {
        const key = el.getAttribute('data-i18n');
        if (key) {
          el.textContent = t(key);
        }
      }
      const titleEls = target.querySelectorAll('[data-i18n-title]');
      for (const el of titleEls) {
        const key = el.getAttribute('data-i18n-title');
        if (key) {
          el.setAttribute('title', t(key));
        }
      }
      const placeholderEls = target.querySelectorAll('[data-i18n-placeholder]');
      for (const el of placeholderEls) {
        const key = el.getAttribute('data-i18n-placeholder');
        if (key) {
          el.setAttribute('placeholder', t(key));
        }
      }
      const ariaEls = target.querySelectorAll('[data-i18n-aria-label]');
      for (const el of ariaEls) {
        const key = el.getAttribute('data-i18n-aria-label');
        if (key) {
          el.setAttribute('aria-label', t(key));
        }
      }
    }

    function onChange(fn) {
      if (typeof fn !== 'function') return () => {};
      listeners.add(fn);
      return () => listeners.delete(fn);
    }

    return {
      resolveLanguage,
      normalizeLang,
      setBundles,
      t,
      getLang,
      setLangSync,
      applyDom,
      onChange,
      isReady: () => ready
    };
  }

  // Export cleanly for Node / offline verification
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      SUPPORTED_LANGS,
      DEFAULT_LANG,
      resolveLanguage,
      normalizeLang,
      createI18n
    };
  }

  if (typeof window === 'undefined') {
    return;
  }

  const hasChromeRuntime = Boolean(
    typeof chrome !== 'undefined' &&
    chrome.runtime &&
    chrome.runtime.id &&
    typeof chrome.runtime.getURL === 'function'
  );
  const isExtensionPage = window.location.protocol === 'chrome-extension:';
  const isHoyolabMap = window.location.hostname.includes('hoyolab.com') &&
    window.location.pathname.includes('/interactive-map/');

  async function fetchJsonSafe(url) {
    try {
      const res = await fetch(url, { cache: 'no-cache' });
      if (!res.ok) return {};
      const parsed = await res.json();
      return (parsed && typeof parsed === 'object') ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  async function fetchLocaleBundles(baseUrlResolver) {
    const [vi, en] = await Promise.all([
      fetchJsonSafe(baseUrlResolver('locales/vi.json')),
      fetchJsonSafe(baseUrlResolver('locales/en.json'))
    ]);
    return { vi, en };
  }

  function storageGet(keys) {
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

  function storageSet(obj) {
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

  // -------------------------------------------------------------------------
  // Context A: Isolated content-script bridge on HoYoLAB Interactive Map
  // Relays locale bundles, gh_lang, and gh_compact_ui between chrome.storage /
  // popup and MAIN-world page_main.js without touching content_bridge.js.
  // -------------------------------------------------------------------------
  if (hasChromeRuntime && !isExtensionPage) {
    if (!isHoyolabMap) {
      return;
    }
    // Allow clean re-injection after an extension reload when the prior isolated context was invalidated
    if (window.__gh_i18n_isolated_check && window.__gh_i18n_isolated_check()) {
      return;
    }
    window.__gh_i18n_isolated_loaded = true;
    window.__gh_i18n_isolated_check = () => Boolean(typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.id);

    let cachedBundles = null;
    let cachedLang = null;

    function requestBundlesFromBg() {
      return new Promise((resolve) => {
        try {
          chrome.runtime.sendMessage({ kind: 'GET_I18N_BUNDLES' }, (resp) => {
            void (chrome.runtime && chrome.runtime.lastError);
            if (resp && resp.ok && resp.bundles && typeof resp.bundles === 'object') {
              resolve(resp.bundles);
              return;
            }
            resolve(null);
          });
        } catch (e) {
          resolve(null);
        }
      });
    }

    async function ensureBundlesLoaded() {
      if (cachedBundles && (Object.keys(cachedBundles.vi).length > 0 || Object.keys(cachedBundles.en).length > 0)) {
        return cachedBundles;
      }
      // Prefer Service Worker delivery first (100% immune to Brave Shields & cross-origin page CSP)
      const bgBundles = await requestBundlesFromBg();
      if (bgBundles && (Object.keys(bgBundles.vi || {}).length > 0 || Object.keys(bgBundles.en || {}).length > 0)) {
        cachedBundles = bgBundles;
        return cachedBundles;
      }
      const fetched = await fetchLocaleBundles((rel) => chrome.runtime.getURL(rel));
      if (Object.keys(fetched.vi).length > 0 || Object.keys(fetched.en).length > 0) {
        cachedBundles = fetched;
      }
      return cachedBundles || fetched;
    }

    async function pushStateToPage(explicitLang) {
      const [bundles, stored] = await Promise.all([
        ensureBundlesLoaded(),
        storageGet([STORAGE_KEY_LANG, STORAGE_KEY_COMPACT])
      ]);
      const lang = resolveLanguage(explicitLang || stored[STORAGE_KEY_LANG]);
      cachedLang = lang;

      window.postMessage({
        __gh_ext: true,
        dir: 'to-page',
        kind: 'I18N_SYNC',
        lang,
        bundles
      }, window.location.origin);

      if (typeof stored[STORAGE_KEY_COMPACT] === 'boolean') {
        window.postMessage({
          __gh_ext: true,
          dir: 'to-page',
          kind: 'COMPACT_UI_SYNC',
          enabled: stored[STORAGE_KEY_COMPACT]
        }, window.location.origin);
      }
    }

    if (chrome.storage && chrome.storage.onChanged) {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'local' || !changes) return;
        if (changes[STORAGE_KEY_LANG]) {
          pushStateToPage(changes[STORAGE_KEY_LANG].newValue);
        }
        if (changes[STORAGE_KEY_COMPACT] && typeof changes[STORAGE_KEY_COMPACT].newValue === 'boolean') {
          window.postMessage({
            __gh_ext: true,
            dir: 'to-page',
            kind: 'COMPACT_UI_SYNC',
            enabled: changes[STORAGE_KEY_COMPACT].newValue
          }, window.location.origin);
        }
      });
    }

    if (chrome.runtime && chrome.runtime.onMessage) {
      chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
        if (!msg || typeof msg !== 'object') return false;
        if (msg.kind === 'GH_SET_LANG' && msg.lang) {
          pushStateToPage(msg.lang).then(() => sendResponse({ ok: true }));
          return true;
        }
        if (msg.kind === 'GH_SET_COMPACT_UI' && typeof msg.enabled === 'boolean') {
          window.postMessage({
            __gh_ext: true,
            dir: 'to-page',
            kind: 'COMPACT_UI_SYNC',
            enabled: msg.enabled
          }, window.location.origin);
          sendResponse({ ok: true });
          return false;
        }
        return false;
      });
    }

    window.addEventListener('message', (e) => {
      if (e.source !== window || !e.data || e.data.__gh_ext !== true || e.data.dir !== 'to-isolated') {
        return;
      }
      if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.id) {
        return;
      }
      if (e.data.kind === 'I18N_REQUEST') {
        pushStateToPage(cachedLang);
      } else if (e.data.kind === 'I18N_SET_LANG' && e.data.lang) {
        const norm = resolveLanguage(e.data.lang);
        storageSet({ [STORAGE_KEY_LANG]: norm }).then(() => pushStateToPage(norm));
      } else if (e.data.kind === 'COMPACT_UI_SET' && typeof e.data.enabled === 'boolean') {
        storageSet({ [STORAGE_KEY_COMPACT]: e.data.enabled });
      }
    });

    pushStateToPage(null);
    setTimeout(() => pushStateToPage(cachedLang), 450);
    return;
  }

  // -------------------------------------------------------------------------
  // Context B: Extension Popup (chrome-extension:) & Page MAIN World
  // -------------------------------------------------------------------------
  let initialStoredLang = null;
  try {
    initialStoredLang = localStorage.getItem(STORAGE_KEY_LANG);
  } catch (e) {}

  const core = createI18n(null, initialStoredLang);
  let initPromise = null;
  let hasExplicitStoredLang = Boolean(normalizeLang(initialStoredLang));

  function updateDocumentLang(lang) {
    try {
      if (isExtensionPage && typeof document !== 'undefined' && document.documentElement) {
        document.documentElement.lang = lang;
      }
    } catch (e) {}
  }

  async function init() {
    if (initPromise) return initPromise;
    initPromise = (async () => {
      if (isExtensionPage && hasChromeRuntime) {
        const [bundles, stored] = await Promise.all([
          fetchLocaleBundles((rel) => chrome.runtime.getURL(rel)),
          storageGet([STORAGE_KEY_LANG])
        ]);
        const storedCode = normalizeLang(stored[STORAGE_KEY_LANG]);
        hasExplicitStoredLang = Boolean(storedCode);
        const resolved = resolveLanguage(storedCode || initialStoredLang);
        core.setLangSync(resolved);
        core.setBundles(bundles);
        try { localStorage.setItem(STORAGE_KEY_LANG, resolved); } catch (e) {}
        updateDocumentLang(resolved);
        return resolved;
      }

      // MAIN world on HoYoLAB: request push from isolated script (SW-backed) + retry if isolated boots later
      if (isHoyolabMap) {
        const pingIsolated = () => {
          try {
            window.postMessage({
              __gh_ext: true,
              dir: 'to-isolated',
              kind: 'I18N_REQUEST'
            }, window.location.origin);
          } catch (e) {}
        };
        pingIsolated();

        if (!core.isReady()) {
          const bundles = await fetchLocaleBundles(
            (rel) => `chrome-extension://${PINNED_EXT_ID}/${rel}`
          );
          if (!core.isReady() && (Object.keys(bundles.vi).length > 0 || Object.keys(bundles.en).length > 0)) {
            core.setBundles(bundles);
          }
        }
        if (!core.isReady()) {
          setTimeout(() => { if (!core.isReady()) pingIsolated(); }, 300);
          setTimeout(() => { if (!core.isReady()) pingIsolated(); }, 1000);
        }
      }
      return core.getLang();
    })();
    return initPromise;
  }

  async function setLang(nextLang, opts = {}) {
    const resolved = core.setLangSync(nextLang);
    hasExplicitStoredLang = true;
    try { localStorage.setItem(STORAGE_KEY_LANG, resolved); } catch (e) {}
    updateDocumentLang(resolved);

    if (isExtensionPage && hasChromeRuntime && opts.persist !== false) {
      await storageSet({ [STORAGE_KEY_LANG]: resolved });
    } else if (!isExtensionPage && isHoyolabMap && opts.notifyIsolated !== false) {
      try {
        window.postMessage({
          __gh_ext: true,
          dir: 'to-isolated',
          kind: 'I18N_SET_LANG',
          lang: resolved
        }, window.location.origin);
      } catch (e) {}
    }

    if (isExtensionPage && opts.applyDom !== false) {
      core.applyDom(document);
    }
    return resolved;
  }

  // Receive pushed bundles & live language switches from isolated bridge in MAIN world
  if (!isExtensionPage && isHoyolabMap && !window.__gh_i18n_main_listener) {
    window.__gh_i18n_main_listener = true;
    window.addEventListener('message', (e) => {
      if (e.source !== window || !e.data || e.data.__gh_ext !== true || e.data.dir !== 'to-page') {
        return;
      }
      if (e.data.kind === 'I18N_SYNC') {
        if (e.data.bundles) {
          core.setBundles(e.data.bundles);
        }
        if (e.data.lang) {
          const resolved = core.setLangSync(e.data.lang);
          hasExplicitStoredLang = true;
          try { localStorage.setItem(STORAGE_KEY_LANG, resolved); } catch (err) {}
        }
      }
    });
  }

  globalScope.GHI18n = {
    SUPPORTED_LANGS,
    DEFAULT_LANG,
    STORAGE_KEY_LANG,
    init,
    t: core.t,
    getLang: core.getLang,
    setLang,
    applyDom: core.applyDom,
    onChange: core.onChange,
    resolveLanguage: core.resolveLanguage,
    normalizeLang: core.normalizeLang,
    setBundles: core.setBundles,
    isReady: core.isReady,
    hasExplicitPreference: () => hasExplicitStoredLang
  };

  // Auto-kickstart in MAIN world so bundles arrive immediately
  if (!isExtensionPage && isHoyolabMap) {
    init();
  }
})(typeof window !== 'undefined' ? window : globalThis);
