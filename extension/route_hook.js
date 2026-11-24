(function () {
    'use strict';

    if (!window.location.hostname.includes('hoyolab.com') || !window.location.pathname.includes('/interactive-map/')) {
        return;
    }
    if (window.__ghRouteHookVer === 2) {
        return;
    }
    window.__ghRouteHookVer = 2;

    const buf = window.__ghRouteHookBuffer || { recommends: [], lastRoute: null };
    window.__ghRouteHookBuffer = buf;

    function isRouteApiUrl(urlStr) {
        if (typeof urlStr !== 'string' || !urlStr) return false;
        const low = urlStr.toLowerCase();
        if (low.includes('point_list') || low.includes('label_list') || low.includes('mark_map_point')) {
            return false;
        }
        if (low.includes('get_recommend_path') || low.includes('get_share_map_spots')) {
            return true;
        }
        return low.includes('/map_user/') && (low.includes('path') || low.includes('route') || low.includes('spot_kind'));
    }

    function dispatchHookedJson(urlStr, jsonData) {
        try {
            if (!jsonData || typeof jsonData !== 'object') return;
            const d = jsonData.data;
            if (!d || typeof d !== 'object') return;

            if (String(urlStr || '').toLowerCase().includes('get_recommend_path') && Array.isArray(d.list)) {
                buf.recommends.push({ url: urlStr, data: jsonData });
                while (buf.recommends.length > 4) {
                    buf.recommends.shift();
                }
                if (typeof window.__ghOnHoyolabRouteApi === 'function') {
                    window.__ghOnHoyolabRouteApi(urlStr, jsonData);
                }
                return;
            }

            const hasPathSections = Boolean(d.path && typeof d.path === 'object' && Array.isArray(d.path.sections));
            const hasDirectSections = Array.isArray(d.sections);
            if (hasPathSections || hasDirectSections) {
                buf.lastRoute = { url: urlStr, data: jsonData, ts: Date.now() };
                if (typeof window.__ghOnHoyolabRouteApi === 'function') {
                    window.__ghOnHoyolabRouteApi(urlStr, jsonData);
                }
            }
        } catch (e) {}
    }

    if (typeof window.fetch === 'function') {
        const origFetch = window.fetch;
        window.fetch = function (...args) {
            const urlStr = typeof args[0] === 'string'
                ? args[0]
                : (args[0] && typeof args[0].url === 'string' ? args[0].url : '');
            const p = origFetch.apply(this, args);
            if (isRouteApiUrl(urlStr)) {
                p.then((res) => {
                    if (!res || !res.ok || typeof res.clone !== 'function') return;
                    res.clone().json()
                        .then((j) => dispatchHookedJson(urlStr, j))
                        .catch(() => {});
                }).catch(() => {});
            }
            return p;
        };
    }

    if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
        const origOpen = window.XMLHttpRequest.prototype.open;
        const origSend = window.XMLHttpRequest.prototype.send;
        window.XMLHttpRequest.prototype.open = function (method, url, ...rest) {
            this.__ghReqUrl = typeof url === 'string' ? url : '';
            return origOpen.call(this, method, url, ...rest);
        };
        window.XMLHttpRequest.prototype.send = function (...args) {
            const u = this.__ghReqUrl || '';
            if (isRouteApiUrl(u)) {
                this.addEventListener('load', () => {
                    try {
                        if (this.status >= 200 && this.status < 300) {
                            const payload = (this.responseType === 'json')
                                ? this.response
                                : (typeof this.responseText === 'string' ? JSON.parse(this.responseText) : null);
                            if (payload) {
                                dispatchHookedJson(u, payload);
                            }
                        }
                    } catch (e) {}
                }, { once: true });
            }
            return origSend.apply(this, args);
        };
    }
})();
