"""
Bilingual VI / EN internationalization module for Genshin Helper Desktop.

Language resolution priority:
    explicit setting ("vi" / "en") > OS locale > "en"

Translation lookup fallback chain:
    requested/active lang (non-empty) > "en" (non-empty) > key
"""
from __future__ import annotations

import ctypes
import json
import locale
import logging
import os
import threading
from typing import Any, Dict, Mapping, Optional, Tuple

from src.paths import get_asset_path

logger = logging.getLogger("GenshinHelper.i18n")

SUPPORTED_LANGUAGES: Tuple[str, ...] = ("vi", "en")
DEFAULT_LANGUAGE: str = "en"


class _SafeFormatDict(dict):
    """Preserves unresolved {placeholders} instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _normalize_lang_code(raw: Optional[str]) -> Optional[str]:
    """Maps explicit language tags (e.g. 'vi', 'vi-VN', 'Vietnamese', 'en_US') to 'vi'/'en', else None."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower().replace("_", "-")
    if not cleaned or cleaned in ("auto", "system", "default"):
        return None
    if cleaned == "vi" or cleaned.startswith("vi-") or cleaned.startswith("vietnamese"):
        return "vi"
    if cleaned == "en" or cleaned.startswith("en-") or cleaned.startswith("english"):
        return "en"
    return None


def _detect_windows_locale_name() -> Optional[str]:
    """Queries Windows user UI / locale name via kernel32 with explicit 64-bit ctypes prototypes."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Primary: GetUserDefaultLocaleName -> BCP-47 tag (e.g. "vi-VN", "en-US")
        get_locale_name = kernel32.GetUserDefaultLocaleName
        get_locale_name.argtypes = [wintypes.LPWSTR, ctypes.c_int]
        get_locale_name.restype = ctypes.c_int

        buf_len = 85  # LOCALE_NAME_MAX_LENGTH
        buf = ctypes.create_unicode_buffer(buf_len)
        res = get_locale_name(buf, buf_len)
        if res > 0 and buf.value:
            return buf.value

        # Secondary: GetUserDefaultUILanguage -> LANGID (0x2A == LANG_VIETNAMESE)
        get_ui_lang = kernel32.GetUserDefaultUILanguage
        get_ui_lang.argtypes = []
        get_ui_lang.restype = wintypes.WORD

        lang_id = int(get_ui_lang())
        primary_id = lang_id & 0x3FF
        if primary_id == 0x2A:
            return "vi-VN"
        if primary_id == 0x09:
            return "en-US"
    except Exception as exc:
        logger.debug("Windows locale query fallback triggered: %s", exc)
    return None


def _detect_os_language() -> str:
    """Detects OS locale and maps it to a supported language ('vi' or 'en')."""
    candidates = []

    win_loc = _detect_windows_locale_name()
    if win_loc:
        candidates.append(win_loc)

    try:
        loc_tuple = locale.getlocale()
        if loc_tuple and loc_tuple[0]:
            candidates.append(str(loc_tuple[0]))
    except Exception:
        pass

    for env_key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        env_val = os.environ.get(env_key)
        if env_val:
            candidates.append(env_val)

    for cand in candidates:
        matched = _normalize_lang_code(cand)
        if matched in SUPPORTED_LANGUAGES:
            return matched

    return DEFAULT_LANGUAGE


def resolve_language(setting: Optional[str] = "auto") -> str:
    """
    Resolves language code using priority:
        explicit ("vi" / "en") > OS locale > "en".
    """
    explicit = _normalize_lang_code(setting)
    if explicit in SUPPORTED_LANGUAGES:
        return explicit
    detected = _detect_os_language()
    return detected if detected in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def _resolve_locale_file(lang: str) -> str:
    """Resolves locale JSON file path for both dev and PyInstaller frozen builds."""
    asset_candidate = get_asset_path("src", "locales", f"{lang}.json")
    if os.path.isfile(asset_candidate):
        return asset_candidate
    local_candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales", f"{lang}.json")
    return local_candidate


def _load_locale_file(lang: str) -> Dict[str, str]:
    path = _resolve_locale_file(lang)
    if not os.path.isfile(path):
        logger.warning("Locale file not found for '%s': %s", lang, path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception as exc:
        logger.error("Failed to load locale file '%s': %s", path, exc)
        return {}


class Translator:
    """Thread-safe bilingual translator supporting clean fallback and parameter interpolation."""

    def __init__(
        self,
        bundles: Optional[Mapping[str, Mapping[str, str]]] = None,
        setting: Optional[str] = "auto",
    ) -> None:
        self._lock = threading.RLock()
        self._bundles: Dict[str, Dict[str, str]] = {}
        self._language: str = resolve_language(setting)
        if bundles is not None:
            for code, mapping in bundles.items():
                self._bundles[str(code).lower()] = {str(k): str(v) for k, v in mapping.items()}
        else:
            self.reload()

    def reload(self) -> Dict[str, Dict[str, str]]:
        loaded = {code: _load_locale_file(code) for code in SUPPORTED_LANGUAGES}
        with self._lock:
            self._bundles = loaded
            return {code: dict(vals) for code, vals in self._bundles.items()}

    def get_language(self) -> str:
        with self._lock:
            return self._language

    def set_language(self, setting: Optional[str] = "auto") -> str:
        resolved = resolve_language(setting)
        with self._lock:
            self._language = resolved
            return self._language

    def get_locale_dict(self, lang: str) -> Dict[str, str]:
        code = _normalize_lang_code(lang) or DEFAULT_LANGUAGE
        with self._lock:
            return dict(self._bundles.get(code, {}))

    def t(self, key: str, lang: Optional[str] = None, **params: Any) -> str:
        """
        Looks up `key` in `lang` (or active language), falling back to 'en' when
        missing or empty/whitespace, and finally to `key` itself.
        """
        if not isinstance(key, str):
            return str(key)

        with self._lock:
            target_lang = resolve_language(lang) if lang is not None else self._language
            primary_bundle = self._bundles.get(target_lang, {})
            en_bundle = self._bundles.get(DEFAULT_LANGUAGE, {})

            raw_val = primary_bundle.get(key)
            if not isinstance(raw_val, str) or not raw_val.strip():
                raw_val = en_bundle.get(key)
            if not isinstance(raw_val, str) or not raw_val.strip():
                template = key
            else:
                template = raw_val

        if not params:
            return template

        try:
            return template.format_map(_SafeFormatDict(params))
        except (ValueError, KeyError, IndexError):
            return template


_default_translator = Translator()


def get_language() -> str:
    """Returns currently active language code ('vi' or 'en')."""
    return _default_translator.get_language()


def set_language(setting: Optional[str] = "auto") -> str:
    """Sets active language using explicit > OS > 'en' priority and returns resolved code."""
    return _default_translator.set_language(setting)


def reload_locales() -> Dict[str, Dict[str, str]]:
    """Reloads locale JSON dictionaries from disk."""
    return _default_translator.reload()


def get_locale_dict(lang: str) -> Dict[str, str]:
    """Returns a shallow copy of the loaded dictionary for the specified language."""
    return _default_translator.get_locale_dict(lang)


def t(key: str, lang: Optional[str] = None, **params: Any) -> str:
    """Translates `key` with optional language override and `{param}` interpolation."""
    return _default_translator.t(key, lang=lang, **params)
