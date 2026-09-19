// ==UserScript==
// @name         HoYoLAB Genshin Helper (Live Dev Loader)
// @namespace    https://github.com/genshin-helper
// @version      1.0.1
// @description  Tự động nạp code Genshin Helper mới nhất trực tiếp từ ổ cứng mỗi khi F5 web!
// @match        *://act.hoyolab.com/*
// @match        https://act.hoyolab.com/*
// @include      *act.hoyolab.com*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';
    console.log('[Genshin Helper Loader] Dang ket noi toi server http://127.0.0.1:8765/script.js...');

    GM_xmlhttpRequest({
        method: 'GET',
        url: 'http://127.0.0.1:8765/script.js?t=' + Date.now(),
        onload: function (res) {
            if (res.status === 200) {
                try {
                    const s = document.createElement('script');
                    s.textContent = res.responseText;
                    (document.head || document.documentElement).appendChild(s);
                    console.log('%c[Genshin Helper Loader] DA NAP THANH CONG BAN MOI NHAT TU O CUNG!', 'color: #22c55e; font-size: 14px; font-weight: bold;');
                } catch (e) {
                    console.error('[Genshin Helper Loader] Loi thuc thi:', e);
                }
            } else {
                console.warn('[Genshin Helper Loader] Server bao status:', res.status);
            }
        },
        onerror: function (err) {
            console.error('[Genshin Helper Loader] Khong the ket noi toi 127.0.0.1:8765:', err);
        }
    });
})();
