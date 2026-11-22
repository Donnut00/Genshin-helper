# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/asset_setup.py (MIT License)
"""
Local map asset cache resolution, MD5 manifest verification, atomic staged promotion
with rollback, and first-run hydration via scripts/sync_hoyolab_data.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger("GenshinHelper.AssetSetup")

REQUIRED_ASSETS: tuple[str, ...] = (
    "multimap_v4.json",
    "terrain_n2_v4.npz",
    "cave_masks_v4.npz",
)
OPTIONAL_ASSETS: tuple[str, ...] = ("ui_anchors_v4.npz",)
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DataStatus:
    """Verification snapshot of a map asset directory."""

    ready: bool
    data_dir: Path
    missing: tuple[str, ...]
    corrupted: tuple[str, ...]
    mismatched: tuple[str, ...]
    manifest_present: bool
    source: str = "cache"


def get_user_data_cache_dir() -> Path:
    """
    Resolve the per-user writable map asset cache directory:
    - Env override: GENSHIN_HELPER_DATA_DIR (if set)
    - Windows: %LOCALAPPDATA%\\GenshinHelper\\data
    - Other OS: $XDG_DATA_HOME/genshin-helper/data or ~/.local/share/genshin-helper/data
    """
    override = os.environ.get("GENSHIN_HELPER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "win32" or os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            return Path(local_app).resolve() / "GenshinHelper" / "data"
        return Path.home() / "AppData" / "Local" / "GenshinHelper" / "data"

    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data).expanduser().resolve() / "genshin-helper" / "data"
    return Path.home() / ".local" / "share" / "genshin-helper" / "data"


def get_default_bundle_data_dir() -> Path:
    """Resolve the dev/bundled src/data directory."""
    try:
        from src.paths import get_bundle_dir

        return Path(get_bundle_dir()) / "src" / "data"
    except Exception:
        return Path(__file__).resolve().parent / "data"


def resolve_data_asset(filename: str) -> Path:
    """Resolve a data asset file checking bundled/dev src/data first, then user cache."""
    bundle_candidate = get_default_bundle_data_dir() / filename
    if bundle_candidate.is_file():
        return bundle_candidate
    cache_candidate = get_user_data_cache_dir() / filename
    if cache_candidate.is_file():
        return cache_candidate
    return bundle_candidate


def resolve_data_dir() -> Path:
    """Return the first data directory containing all required map assets."""
    bundle_dir = get_default_bundle_data_dir()
    if verify_data_dir(bundle_dir, REQUIRED_ASSETS, check_manifest=False, source="bundle").ready:
        return bundle_dir
    cache_dir = get_user_data_cache_dir()
    if verify_data_dir(cache_dir, REQUIRED_ASSETS, check_manifest=False, source="cache").ready:
        return cache_dir
    return bundle_dir


def compute_md5(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute lowercase hexadecimal MD5 digest for a file."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_asset_file(path: Path) -> bool:
    """Verify that an asset file exists, is non-empty, and has valid container syntax."""
    if not path.is_file():
        return False
    try:
        if path.stat().st_size <= 0:
            return False
        suffix = path.suffix.lower()
        if suffix == ".json":
            with path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            return isinstance(parsed, (dict, list)) and len(parsed) > 0
        if suffix == ".npz":
            if not zipfile.is_zipfile(path):
                return False
            with zipfile.ZipFile(path, "r") as archive:
                names = archive.namelist()
                return len(names) > 0 and all(name.endswith(".npy") for name in names)
        return True
    except Exception:
        return False


def read_manifest(data_dir: Path) -> dict[str, Any] | None:
    """Load manifest.json from data_dir if present and valid."""
    manifest_path = data_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def build_manifest(
    data_dir: Path,
    required_assets: Sequence[str] = REQUIRED_ASSETS,
    *,
    optional_assets: Sequence[str] = OPTIONAL_ASSETS,
    source: str = "local",
) -> dict[str, Any]:
    """Build an MD5 manifest dictionary for existing required/optional assets in data_dir."""
    files_meta: dict[str, dict[str, Any]] = {}
    ordered = list(required_assets) + [a for a in optional_assets if a not in required_assets]
    for name in ordered:
        asset_path = data_dir / name
        if asset_path.is_file():
            stat = asset_path.stat()
            files_meta[name] = {
                "md5": compute_md5(asset_path),
                "size": stat.st_size,
            }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "files": files_meta,
    }


def write_manifest(data_dir: Path, manifest: dict[str, Any]) -> Path:
    """Atomically write manifest.json (UTF-8) into data_dir."""
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / MANIFEST_FILENAME
    tmp_path = data_dir / f".{MANIFEST_FILENAME}.tmp"
    tmp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, target)
    return target


def verify_data_dir(
    data_dir: Path,
    required_assets: Sequence[str] = REQUIRED_ASSETS,
    *,
    check_manifest: bool = True,
    source: str = "cache",
) -> DataStatus:
    """Verify presence, basic integrity, and optional MD5 manifest consistency."""
    missing: list[str] = []
    corrupted: list[str] = []
    mismatched: list[str] = []

    manifest = read_manifest(data_dir) if check_manifest else None
    recorded_files = manifest.get("files", {}) if isinstance(manifest, dict) else {}

    for name in required_assets:
        asset_path = data_dir / name
        if not asset_path.is_file():
            missing.append(name)
            continue
        if not validate_asset_file(asset_path):
            corrupted.append(name)
            continue
        if check_manifest and isinstance(recorded_files, dict) and name in recorded_files:
            entry = recorded_files[name]
            expected_md5 = entry.get("md5") if isinstance(entry, dict) else None
            if expected_md5 and compute_md5(asset_path) != str(expected_md5).lower():
                mismatched.append(name)

    ready = not missing and not corrupted and not mismatched
    return DataStatus(
        ready=ready,
        data_dir=data_dir,
        missing=tuple(missing),
        corrupted=tuple(corrupted),
        mismatched=tuple(mismatched),
        manifest_present=manifest is not None,
        source=source,
    )


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def promote_staged_assets(pairs: Sequence[tuple[Path, Path]]) -> None:
    """
    Atomically promote staged files/directories to their final destinations and
    roll back every promoted item if any step fails.

    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/asset_setup.py.
    """
    promoted: list[tuple[Path, Path, Path, bool]] = []
    try:
        for staged, destination in pairs:
            staged_path = Path(staged)
            dest_path = Path(destination)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            previous = dest_path.with_name(f".{dest_path.name}.previous")
            _remove_path(previous)
            had_previous = dest_path.exists()
            if had_previous:
                os.replace(dest_path, previous)
            try:
                os.replace(staged_path, dest_path)
            except Exception:
                if had_previous and previous.exists() and not dest_path.exists():
                    os.replace(previous, dest_path)
                raise
            promoted.append((staged_path, dest_path, previous, had_previous))
    except Exception:
        for _, dest_path, previous, had_previous in reversed(promoted):
            _remove_path(dest_path)
            if had_previous and previous.exists():
                os.replace(previous, dest_path)
        raise
    for _, _, previous, _ in promoted:
        _remove_path(previous)


# Upstream compatibility alias
_promote_assets = promote_staged_assets


def _default_sync_runner(
    stage_dir: Path,
    *,
    sync_script: Path | None,
    fallback_bundle_dir: Path | None,
    required_assets: Sequence[str],
) -> None:
    """Hydrate assets directly in-process (PyInstaller-safe) or via explicit sync_script."""
    from src.paths import is_frozen

    if sync_script is not None and sync_script.is_file() and not is_frozen():
        logger.info("Hydrating local HoYoLAB map assets via %s...", sync_script)
        subprocess.run(
            [sys.executable, str(sync_script)],
            cwd=str(sync_script.parent.parent),
            check=True,
        )
        repo_data_dir = sync_script.parent.parent / "src" / "data"
        search_dirs = [repo_data_dir]
        if fallback_bundle_dir is not None and fallback_bundle_dir != repo_data_dir:
            search_dirs.append(fallback_bundle_dir)
        for name in required_assets:
            dest = stage_dir / name
            if dest.is_file():
                continue
            for candidate_dir in search_dirs:
                candidate = candidate_dir / name
                if candidate.is_file():
                    shutil.copy2(candidate, dest)
                    break
        return

    from src.hoyolab_sync import sync_to_directory

    legacy_ug = Path(__file__).resolve().parent / "underground_db.json"
    sync_to_directory(
        stage_dir,
        legacy_ug_path=legacy_ug if legacy_ug.is_file() else None,
    )


def ensure_data_ready(
    fallback_bundle_dir: Path | str | None = None,
    *,
    cache_dir: Path | str | None = None,
    required_assets: Sequence[str] = REQUIRED_ASSETS,
    auto_sync: bool = True,
    sync_script: Path | str | None = None,
    sync_runner: Callable[[Path], None] | None = None,
) -> Path:
    """
    Return a verified directory containing all required map assets.

    Resolution order:
    1. User cache dir (%LOCALAPPDATA%\\GenshinHelper\\data) if complete and manifest-valid.
    2. Bundled/dev fallback dir (e.g. src/data) if complete and structurally valid.
    3. Otherwise, stage and auto-hydrate into user cache via scripts/sync_hoyolab_data.py
       (or custom sync_runner), compute MD5 manifest.json, and atomically promote.
    """
    target_cache = Path(cache_dir) if cache_dir is not None else get_user_data_cache_dir()
    cache_status = verify_data_dir(
        target_cache, required_assets, check_manifest=True, source="cache"
    )
    if cache_status.ready:
        return target_cache

    bundle_dir = (
        Path(fallback_bundle_dir)
        if fallback_bundle_dir is not None
        else get_default_bundle_data_dir()
    )
    bundle_status = verify_data_dir(
        bundle_dir, required_assets, check_manifest=False, source="bundle"
    )
    if bundle_status.ready:
        return bundle_dir

    if not auto_sync:
        raise FileNotFoundError(
            "Required map assets are unavailable in cache "
            f"({target_cache}; missing={cache_status.missing}, "
            f"corrupted={cache_status.corrupted}, mismatched={cache_status.mismatched}) "
            f"and bundle ({bundle_dir}; missing={bundle_status.missing}). "
            "Run scripts/sync_hoyolab_data.py to hydrate local map data."
        )

    target_cache.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(
        tempfile.mkdtemp(prefix=".genshin_helper_stage_", dir=str(target_cache.parent))
    )
    try:
        # Seed any valid assets already present in bundle_dir (e.g., ui_anchors_v4.npz)
        if bundle_dir.is_dir():
            for name in required_assets:
                candidate = bundle_dir / name
                if validate_asset_file(candidate):
                    shutil.copy2(candidate, stage_dir / name)

        if sync_runner is not None:
            sync_runner(stage_dir)
        else:
            resolved_script = Path(sync_script) if sync_script is not None else None
            _default_sync_runner(
                stage_dir,
                sync_script=resolved_script,
                fallback_bundle_dir=bundle_dir,
                required_assets=required_assets,
            )

        staged_status = verify_data_dir(
            stage_dir, required_assets, check_manifest=False, source="staged"
        )
        if not staged_status.ready:
            raise RuntimeError(
                "Staged map asset hydration incomplete: "
                f"missing={staged_status.missing}, corrupted={staged_status.corrupted}"
            )

        manifest = build_manifest(stage_dir, required_assets, source="sync_hoyolab_data")
        staged_manifest = write_manifest(stage_dir, manifest)

        pairs: list[tuple[Path, Path]] = [
            (stage_dir / name, target_cache / name) for name in required_assets
        ]
        for opt_name in OPTIONAL_ASSETS:
            opt_src = stage_dir / opt_name
            if opt_name not in required_assets and opt_src.is_file():
                pairs.append((opt_src, target_cache / opt_name))
        pairs.append((staged_manifest, target_cache / MANIFEST_FILENAME))
        promote_staged_assets(pairs)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    final_status = verify_data_dir(
        target_cache, required_assets, check_manifest=True, source="cache"
    )
    if not final_status.ready:
        raise RuntimeError(
            f"Asset verification failed after promotion into {target_cache}: {final_status}"
        )
    return target_cache


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or hydrate Genshin-Helper local map asset cache."
    )
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--bundle-dir", type=Path, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check availability without triggering network sync.",
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Generate/refresh manifest.json for the resolved data directory.",
    )
    args = parser.parse_args(argv)

    try:
        resolved = ensure_data_ready(
            fallback_bundle_dir=args.bundle_dir,
            cache_dir=args.cache_dir,
            auto_sync=not args.check,
        )
    except Exception as exc:
        print(f"[asset_setup] NOT READY: {exc}", file=sys.stderr)
        return 1

    if args.write_manifest:
        manifest = build_manifest(resolved, REQUIRED_ASSETS, source="cli")
        write_manifest(resolved, manifest)

    status = verify_data_dir(resolved, REQUIRED_ASSETS, check_manifest=True)
    print(
        f"[asset_setup] READY: {resolved} "
        f"(manifest={'yes' if status.manifest_present else 'no'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
