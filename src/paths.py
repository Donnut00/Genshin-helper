import os
import sys


def is_frozen() -> bool:
    """Returns True when running inside a PyInstaller bundled executable."""
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def get_bundle_dir() -> str:
    """Returns the read-only bundle root (sys._MEIPASS when frozen, repo root in dev)."""
    if is_frozen():
        return os.path.abspath(str(getattr(sys, "_MEIPASS")))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_app_dir() -> str:
    """Returns the executable directory when frozen, or repo root in dev."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return get_bundle_dir()


def get_data_dir() -> str:
    """Returns the writable per-user data directory (%APPDATA%\\GenshinHelper when frozen)."""
    if is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(os.path.abspath(base), "GenshinHelper")
    return get_bundle_dir()


def get_config_path() -> str:
    """Returns config.json path (repo root in dev, %APPDATA%\\GenshinHelper\\config.json when frozen)."""
    return os.path.join(get_data_dir(), "config.json")


def get_diagnostics_dir() -> str:
    """Returns diagnostics bundle root (scratch/diagnostics in dev, %APPDATA%\\GenshinHelper\\diagnostics when frozen)."""
    if is_frozen():
        return os.path.join(get_data_dir(), "diagnostics")
    return os.path.join(get_bundle_dir(), "scratch", "diagnostics")


def get_ocr_cache_dir() -> str:
    """Returns temp OCR cache directory (scratch in dev, %LOCALAPPDATA%\\GenshinHelper\\ocr_cache when frozen)."""
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(os.path.abspath(base), "GenshinHelper", "ocr_cache")
    return os.path.join(get_bundle_dir(), "scratch")


def get_asset_path(*parts: str) -> str:
    """Resolves a bundled asset relative to bundle dir (or executable dir fallback when frozen)."""
    primary = os.path.join(get_bundle_dir(), *parts)
    if os.path.exists(primary):
        return primary
    if is_frozen():
        secondary = os.path.join(get_app_dir(), *parts)
        if os.path.exists(secondary):
            return secondary
    return primary
