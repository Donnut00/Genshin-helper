"""In-game Guide Card data: background fetch of HoYoLAB pin notes & guide image.

The HoYoLAB point list only carries coordinates + label ids; the guide text (`content`) and
screenshot (`img`) live behind the public `point/info` endpoint. This module fetches them off the
game/tracking threads, caches them per pin id, and hands decoded bytes back via callbacks so the
Win32 Mini-HUD can render the Guide Card without ever blocking a frame.
"""
from __future__ import annotations

from collections import OrderedDict
import html
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("GenshinHelper.PinGuide")

API_BASE = "https://sg-public-api.hoyolab.com/common/map_user/ys_obc"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GenshinHelper",
    "Accept": "application/json, image/*",
}
_IMAGE_RESIZE_QS = "x-oss-process=image/resize,w_960"
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*br\s*/?>|</\s*p\s*>", re.IGNORECASE)


def _short_lang(lang: Optional[str]) -> str:
    return "vi" if str(lang or "vi").strip().lower().startswith("vi") else "en"


def _hoyolab_lang_tag(lang: Optional[str]) -> str:
    return "vi-vn" if _short_lang(lang) == "vi" else "en-us"


@dataclass(frozen=True)
class PinGuide:
    pin_id: str
    description: str
    image_url: str
    image_bytes: Optional[bytes] = None
    ok: bool = True
    lang: str = ""


def clean_guide_text(raw: str) -> str:
    """Strips HoYoLAB rich-text markup down to plain multi-line text."""
    if not raw:
        return ""
    text = _BR_RE.sub("\n", str(raw))
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _resized_image_url(url: str) -> str:
    if not url or "x-oss-process" in url:
        return url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.netloc.endswith(("hoyoverse.com", "hoyolab.com", "mihoyo.com")):
        return url
    sep = "&" if parsed.query else "?"
    return f"{url}{sep}{_IMAGE_RESIZE_QS}"


def _http_get(url: str, timeout: float, limit: int) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response too large")
    return data


class PinGuideFetcher:
    """Single background worker; newest request wins, results cached in a small LRU."""

    MAX_CACHE = 64

    def __init__(
        self,
        lang_getter: Callable[[], str] = lambda: "vi",
        *,
        fetch_json: Optional[Callable[[str], Dict]] = None,
        fetch_bytes: Optional[Callable[[str], bytes]] = None,
        timeout_s: float = 8.0,
    ) -> None:
        self._lang_getter = lang_getter
        self._timeout = float(timeout_s)
        self._fetch_json = fetch_json or (lambda u: json.loads(_http_get(u, self._timeout, 512 * 1024)))
        self._fetch_bytes = fetch_bytes or (lambda u: _http_get(u, self._timeout, _MAX_IMAGE_BYTES))
        self._cache: "OrderedDict[Tuple[str, str], PinGuide]" = OrderedDict()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: Optional[Tuple[str, str, int, Callable[[PinGuide], None]]] = None
        self._inflight: Optional[Tuple[str, str]] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    @staticmethod
    def is_fetchable_id(pin_id: str) -> bool:
        return str(pin_id or "").strip().isdigit()

    def _active_lang_tag(self, lang: Optional[str] = None) -> str:
        return _hoyolab_lang_tag(lang if lang is not None else self._lang_getter())

    def get_cached(self, pin_id: str, lang: Optional[str] = None) -> Optional[PinGuide]:
        cache_key = (self._active_lang_tag(lang), str(pin_id))
        with self._lock:
            g = self._cache.get(cache_key)
            if g is not None:
                self._cache.move_to_end(cache_key)
            return g

    def is_loading(self, pin_id: str, lang: Optional[str] = None) -> bool:
        cache_key = (self._active_lang_tag(lang), str(pin_id))
        with self._lock:
            return self._inflight == cache_key or (
                self._pending is not None and (self._pending[0], self._pending[1]) == cache_key
            )

    def request(
        self,
        pin_id: str,
        map_id: int,
        on_done: Callable[[PinGuide], None],
        *,
        lang: Optional[str] = None,
    ) -> bool:
        """Queues a localized fetch (replacing any queued one). Returns False if cached / invalid."""
        pid = str(pin_id or "").strip()
        if not self.is_fetchable_id(pid) or self._stopped:
            return False
        lang_tag = self._active_lang_tag(lang)
        cache_key = (lang_tag, pid)
        with self._cv:
            if cache_key in self._cache or self._inflight == cache_key:
                return False
            self._pending = (lang_tag, pid, int(map_id), on_done)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="GH_PinGuide", daemon=True)
                self._thread.start()
            self._cv.notify()
        return True

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._pending = None
            self._cv.notify_all()

    def fetch_sync(self, pin_id: str, map_id: int, *, lang: Optional[str] = None) -> PinGuide:
        lang_tag = self._active_lang_tag(lang)
        url = (
            f"{API_BASE}/v1/map/point/info?map_id={int(map_id)}&point_id={urllib.parse.quote(str(pin_id))}"
            f"&app_sn=ys_obc&lang={lang_tag}"
        )
        try:
            env = self._fetch_json(url)
            if not isinstance(env, dict) or env.get("retcode") not in (0, "0"):
                raise ValueError(f"retcode={env.get('retcode') if isinstance(env, dict) else '?'}")
            info = ((env.get("data") or {}).get("info")) or {}
            desc = clean_guide_text(str(info.get("content") or ""))
            img_url = str(info.get("img") or "").strip()
            if not img_url and isinstance(info.get("url_list"), list) and info["url_list"]:
                first = info["url_list"][0]
                img_url = str(first.get("url") if isinstance(first, dict) else first or "").strip()
            img_bytes: Optional[bytes] = None
            if img_url.startswith("https://"):
                try:
                    img_bytes = self._fetch_bytes(_resized_image_url(img_url))
                except Exception:
                    try:
                        img_bytes = self._fetch_bytes(img_url)
                    except Exception as exc:
                        logger.debug("Guide image fetch failed for %s: %s", pin_id, exc)
            return PinGuide(str(pin_id), desc, img_url, img_bytes, True, lang_tag)
        except Exception as exc:
            logger.debug("Guide info fetch failed for %s: %s", pin_id, exc)
            return PinGuide(str(pin_id), "", "", None, False, lang_tag)

    def _worker(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._stopped:
                    self._cv.wait(timeout=30.0)
                    if self._pending is None and not self._stopped:
                        self._thread = None
                        return
                if self._stopped:
                    return
                lang_tag, pid, map_id, cb = self._pending  # type: ignore[misc]
                self._pending = None
                cache_key = (lang_tag, pid)
                self._inflight = cache_key
            guide = self.fetch_sync(pid, map_id, lang=lang_tag)
            with self._cv:
                self._inflight = None
                if guide.ok:
                    self._cache[cache_key] = guide
                    self._cache.move_to_end(cache_key)
                    while len(self._cache) > self.MAX_CACHE:
                        self._cache.popitem(last=False)
            try:
                cb(guide)
            except Exception as exc:
                logger.debug("Guide callback error: %s", exc)


class HoYoLABLabelCatalog:
    """
    Non-blocking bilingual HoYoLAB category/label and map-name catalog.
    Resolves `label_id -> localized name` via `v1/map/label/tree` per `(map_id, lang)`
    so the Mini-HUD immediately reflects Popup language changes even if the web tab stays in VI/EN.
    """

    _MAP_NAMES: Dict[int, Dict[str, str]] = {
        2: {"en": "Teyvat Interactive Map", "vi": "Bản Đồ Tương Tác Teyvat"},
        7: {"en": "Enkanomiya", "vi": "Enkanomiya"},
        9: {"en": "The Chasm: Underground Mines", "vi": "Vực Đá Sâu - Khu Mỏ Ngầm"},
        34: {"en": "Sea of Bygone Eras", "vi": "Biển Kỷ Nguyên Cũ"},
        36: {"en": "Ancient Sacred Mountain", "vi": "Núi Thánh Cổ Đại"},
        37: {"en": "Temple of Space", "vi": "Đền Thờ Không Gian"},
        40: {"en": "Nod-Krai", "vi": "Nod-Krai"},
    }
    _RETRY_BACKOFF_S = 60.0

    def __init__(
        self,
        *,
        fetch_json: Optional[Callable[[str], Dict]] = None,
        on_loaded: Optional[Callable[[int, str], None]] = None,
        timeout_s: float = 8.0,
    ) -> None:
        self._timeout = float(timeout_s)
        self._fetch_json = fetch_json or (lambda u: json.loads(_http_get(u, self._timeout, 2 * 1024 * 1024)))
        self._on_loaded = on_loaded
        self._lock = threading.Lock()
        self._labels: Dict[Tuple[int, str], Dict[int, str]] = {}
        self._inflight: set[Tuple[int, str]] = set()
        self._retry_after: Dict[Tuple[int, str], float] = {}

    @classmethod
    def get_map_name(cls, map_id: int, lang: Optional[str] = None, fallback: str = "") -> str:
        short = _short_lang(lang)
        entry = cls._MAP_NAMES.get(int(map_id))
        if entry:
            return entry.get(short) or entry.get("en") or fallback or f"Map {int(map_id)}"
        return fallback or f"Map {int(map_id)}"

    @staticmethod
    def _walk_tree(nodes: Any, out: Dict[int, str]) -> None:
        if not isinstance(nodes, list):
            return
        for item in nodes:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            raw_name = str(item.get("name") or "").strip()
            if raw_id is not None and raw_name:
                try:
                    out[int(raw_id)] = raw_name
                except (TypeError, ValueError):
                    pass
            children = item.get("children")
            if isinstance(children, list) and children:
                HoYoLABLabelCatalog._walk_tree(children, out)

    def fetch_sync(self, map_id: int, lang: Optional[str] = None) -> Dict[int, str]:
        mid = int(map_id)
        short = _short_lang(lang)
        lang_tag = _hoyolab_lang_tag(short)
        url = f"{API_BASE}/v1/map/label/tree?map_id={mid}&app_sn=ys_obc&lang={lang_tag}"
        env = self._fetch_json(url)
        if not isinstance(env, dict) or env.get("retcode") not in (0, "0"):
            raise ValueError(f"retcode={env.get('retcode') if isinstance(env, dict) else '?'}")
        tree = ((env.get("data") or {}).get("tree")) or []
        parsed: Dict[int, str] = {}
        self._walk_tree(tree, parsed)
        with self._lock:
            self._labels[(mid, short)] = parsed
            self._retry_after.pop((mid, short), None)
        return parsed

    def prefetch(self, map_id: int, lang: Optional[str] = None) -> None:
        import time as _time

        mid = int(map_id)
        short = _short_lang(lang)
        key = (mid, short)
        now = _time.monotonic()
        with self._lock:
            if key in self._labels or key in self._inflight:
                return
            if now < self._retry_after.get(key, 0.0):
                return
            self._inflight.add(key)

        def _worker() -> None:
            ok = False
            try:
                self.fetch_sync(mid, short)
                ok = True
            except Exception as exc:
                logger.debug("Label tree fetch failed for map=%s lang=%s: %s", mid, short, exc)
                with self._lock:
                    self._retry_after[key] = _time.monotonic() + self._RETRY_BACKOFF_S
            finally:
                with self._lock:
                    self._inflight.discard(key)
            if ok and self._on_loaded is not None:
                try:
                    self._on_loaded(mid, short)
                except Exception as exc:
                    logger.debug("Label loaded callback error: %s", exc)

        threading.Thread(target=_worker, name=f"GH_Labels_{mid}_{short}", daemon=True).start()

    def get_label(self, map_id: int, label_id: Optional[int], lang: Optional[str] = None) -> Optional[str]:
        if label_id is None:
            return None
        try:
            mid = int(map_id)
            lid = int(label_id)
        except (TypeError, ValueError):
            return None
        short = _short_lang(lang)
        with self._lock:
            mapping = self._labels.get((mid, short))
        if mapping is not None:
            return mapping.get(lid)
        self.prefetch(mid, short)
        return None
