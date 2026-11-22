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
_IMAGE_RESIZE_QS = "x-oss-process=image/resize,w_640"
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*br\s*/?>|</\s*p\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class PinGuide:
    pin_id: str
    description: str
    image_url: str
    image_bytes: Optional[bytes] = None
    ok: bool = True


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
        self._cache: "OrderedDict[str, PinGuide]" = OrderedDict()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: Optional[Tuple[str, int, Callable[[PinGuide], None]]] = None
        self._inflight: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    @staticmethod
    def is_fetchable_id(pin_id: str) -> bool:
        return str(pin_id or "").strip().isdigit()

    def get_cached(self, pin_id: str) -> Optional[PinGuide]:
        with self._lock:
            g = self._cache.get(str(pin_id))
            if g is not None:
                self._cache.move_to_end(str(pin_id))
            return g

    def is_loading(self, pin_id: str) -> bool:
        pid = str(pin_id)
        with self._lock:
            return self._inflight == pid or (self._pending is not None and self._pending[0] == pid)

    def request(self, pin_id: str, map_id: int, on_done: Callable[[PinGuide], None]) -> bool:
        """Queues a fetch (replacing any not-yet-started one). Returns False if cached / invalid."""
        pid = str(pin_id or "").strip()
        if not self.is_fetchable_id(pid) or self._stopped:
            return False
        with self._cv:
            if pid in self._cache or self._inflight == pid:
                return False
            self._pending = (pid, int(map_id), on_done)
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

    def fetch_sync(self, pin_id: str, map_id: int) -> PinGuide:
        lang = "vi-vn" if str(self._lang_getter() or "vi").lower().startswith("vi") else "en-us"
        url = (
            f"{API_BASE}/v1/map/point/info?map_id={int(map_id)}&point_id={urllib.parse.quote(str(pin_id))}"
            f"&app_sn=ys_obc&lang={lang}"
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
            return PinGuide(str(pin_id), desc, img_url, img_bytes, True)
        except Exception as exc:
            logger.debug("Guide info fetch failed for %s: %s", pin_id, exc)
            return PinGuide(str(pin_id), "", "", None, False)

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
                pid, map_id, cb = self._pending  # type: ignore[misc]
                self._pending = None
                self._inflight = pid
            guide = self.fetch_sync(pid, map_id)
            with self._cv:
                self._inflight = None
                if guide.ok:
                    self._cache[pid] = guide
                    self._cache.move_to_end(pid)
                    while len(self._cache) > self.MAX_CACHE:
                        self._cache.popitem(last=False)
            try:
                cb(guide)
            except Exception as exc:
                logger.debug("Guide callback error: %s", exc)
