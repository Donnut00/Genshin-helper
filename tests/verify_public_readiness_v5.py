"""
Verification suite for Genshin-Helper v5 Track 3C — Public GitHub Readiness Pack:
1. src/asset_setup.py: user cache path resolution, MD5 manifest verification,
   atomic promote_staged_assets & full rollback on failure, ensure_data_ready
   (bundle fallback + staged hydration + tamper detection).
2. scripts/audit_release.py: detects synthetic personal paths, secret cookies,
   private keys, UID leaks, scratch/ leaks, and gates HoYoverse IP with --strict-ip;
   passes cleanly on clean directories and all Track 3C public files.
3. scripts/collect_licenses.py: runs cleanly to stdout, --output, --json, and --copy-dir.
4. assets/icons/app_compass_origin.png: valid 256x256 RGBA original compass icon.
5. All public docs, config template, CI workflow, and GitHub templates exist,
   decode as valid UTF-8, and satisfy content & MIT attribution requirements.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import asset_setup


def _create_dummy_assets(directory: Path, tag: str = "v1") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "multimap_v4.json").write_text(
        json.dumps({"version": tag, "maps": {"2": {"name": "Teyvat"}}}, indent=2),
        encoding="utf-8",
    )
    for npz_name in ("terrain_n2_v4.npz", "cave_masks_v4.npz", "ui_anchors_v4.npz"):
        np.savez_compressed(directory / npz_name, payload=np.array([len(tag), 42], dtype=np.int32))


def verify_asset_setup_promote_and_rollback() -> None:
    # 1. Windows %LOCALAPPDATA% cache path resolution
    with tempfile.TemporaryDirectory() as tmp:
        fake_local = Path(tmp) / "LocalAppData"
        with patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(fake_local), "GENSHIN_HELPER_DATA_DIR": ""},
            clear=False,
        ):
            resolved = asset_setup.get_user_data_cache_dir()
            assert resolved == fake_local.resolve() / "GenshinHelper" / "data", (
                f"Unexpected Windows cache dir: {resolved}"
            )

    # 2. Atomic promotion success (existing file replaced + new file promoted + .previous cleaned)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stage = root / "stage"
        dest = root / "dest"
        stage.mkdir()
        dest.mkdir()

        existing = dest / "a.txt"
        existing.write_text("old-a", encoding="utf-8")
        new_target = dest / "b.txt"

        staged_a = stage / "a.txt"
        staged_b = stage / "b.txt"
        staged_a.write_text("new-a", encoding="utf-8")
        staged_b.write_text("new-b", encoding="utf-8")

        asset_setup.promote_staged_assets([(staged_a, existing), (staged_b, new_target)])

        assert existing.read_text(encoding="utf-8") == "new-a"
        assert new_target.read_text(encoding="utf-8") == "new-b"
        assert not (dest / ".a.txt.previous").exists()
        assert not (dest / ".b.txt.previous").exists()

    # 3. Atomic rollback on failure (missing 3rd staged file triggers full rollback of 1st & 2nd)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stage = root / "stage"
        dest = root / "dest"
        stage.mkdir()
        dest.mkdir()

        existing = dest / "keep.txt"
        existing.write_text("original-keep", encoding="utf-8")
        added_new = dest / "fresh.txt"
        failing_dest = dest / "broken.txt"
        failing_dest.write_text("original-broken", encoding="utf-8")

        staged_keep = stage / "keep.txt"
        staged_fresh = stage / "fresh.txt"
        missing_staged = stage / "does_not_exist.txt"
        staged_keep.write_text("mutated-keep", encoding="utf-8")
        staged_fresh.write_text("mutated-fresh", encoding="utf-8")

        raised = False
        try:
            asset_setup.promote_staged_assets(
                [
                    (staged_keep, existing),
                    (staged_fresh, added_new),
                    (missing_staged, failing_dest),
                ]
            )
        except OSError:
            raised = True

        assert raised, "Expected OSError when promoting missing staged asset"
        assert existing.read_text(encoding="utf-8") == "original-keep", "Existing file was not rolled back"
        assert not added_new.exists(), "Newly added file was not removed on rollback"
        assert failing_dest.read_text(encoding="utf-8") == "original-broken", "Target at failure point altered"
        assert not (dest / ".keep.txt.previous").exists()
        assert not (dest / ".broken.txt.previous").exists()

    # 4. ensure_data_ready: bundle fallback + staged auto-hydration + MD5 tamper check
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cache_dir = root / "cache" / "data"
        bundle_dir = root / "bundle" / "data"
        _create_dummy_assets(bundle_dir, tag="bundle")

        resolved_bundle = asset_setup.ensure_data_ready(
            fallback_bundle_dir=bundle_dir,
            cache_dir=cache_dir,
            auto_sync=False,
        )
        assert resolved_bundle == bundle_dir, f"Expected bundle fallback, got {resolved_bundle}"

        # Clean clone scenario: empty bundle triggers staged hydration into cache_dir
        empty_bundle = root / "empty_bundle"
        empty_bundle.mkdir()
        hydrated_calls = []

        def _fake_sync(stage_dir: Path) -> None:
            hydrated_calls.append(stage_dir)
            _create_dummy_assets(stage_dir, tag="hydrated")

        resolved_cache = asset_setup.ensure_data_ready(
            fallback_bundle_dir=empty_bundle,
            cache_dir=cache_dir,
            auto_sync=True,
            sync_runner=_fake_sync,
        )
        assert resolved_cache == cache_dir
        assert len(hydrated_calls) == 1
        manifest = asset_setup.read_manifest(cache_dir)
        assert manifest is not None and len(manifest.get("files", {})) == 4

        # Second call hits cache directly with zero re-sync
        resolved_again = asset_setup.ensure_data_ready(
            fallback_bundle_dir=empty_bundle,
            cache_dir=cache_dir,
            auto_sync=True,
            sync_runner=_fake_sync,
        )
        assert resolved_again == cache_dir
        assert len(hydrated_calls) == 1

        # Tampering with a cached asset triggers MD5 mismatch detection
        (cache_dir / "multimap_v4.json").write_text('{"tampered": true}', encoding="utf-8")
        status = asset_setup.verify_data_dir(cache_dir, check_manifest=True)
        assert not status.ready
        assert "multimap_v4.json" in status.mismatched

    print("  [OK] src/asset_setup.py promote/rollback, bundle fallback & MD5 hydration passed")


def verify_audit_release() -> None:
    audit_script = PROJECT_ROOT / "scripts" / "audit_release.py"
    assert audit_script.is_file()

    # 1. Clean synthetic directory passes
    with tempfile.TemporaryDirectory() as tmp:
        clean_dir = Path(tmp) / "clean"
        clean_dir.mkdir()
        (clean_dir / "hello.py").write_text("print('clean public code')\n", encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(audit_script), "--dir", str(clean_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert res.returncode == 0, f"Clean dir audit failed: {res.stdout}\n{res.stderr}"

    # 2. Synthetic secret/path/UID/scratch leaks are caught (constructed via concatenation)
    leak_cases = [
        ("path_users.txt", "data = '" + "C:" + "\\Users\\" + "alice\\docs'\n", "hardcoded Windows user path"),
        ("path_dev.txt", "root = '" + "E:" + "\\Dev " + "Soft\\proj'\n", "hardcoded developer workspace path"),
        ("cookie.json", '{"c": "' + "ltoken_" + "v2=abc1234567890xyz" + '"}\n', "secret cookie/token value"),
        ("cookie2.txt", "cookie_" + "token=secretvalue987654321\n", "secret cookie/token value"),
        ("key.pem", "-----" + "BEGIN RSA PRIVATE KEY-----\nabc\n", "private key material"),
        ("log.txt", "Logged in as " + "UID" + ": 801234567\n", "UID value"),
        ("scratch/dump.txt", "temporary note\n", "scratch/ directory leak"),
    ]
    for rel_path, content, expected_reason in leak_cases:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            res = subprocess.run(
                [sys.executable, str(audit_script), "--dir", tmp],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert res.returncode != 0, f"Expected audit failure for {rel_path}, got 0: {res.stdout}"
            assert expected_reason in res.stdout, (
                f"Expected '{expected_reason}' in audit output for {rel_path}, got:\n{res.stdout}"
            )

    # 3. Copyrighted HoYoverse IP assets warn normally and fail under --strict-ip
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "assets" / "icons").mkdir(parents=True)
        (root / "assets" / "icons" / "paimon_master.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (root / "src" / "data").mkdir(parents=True)
        (root / "src" / "data" / "terrain_n2_v4.npz").write_bytes(b"PK\x03\x04fake")

        res_warn = subprocess.run(
            [sys.executable, str(audit_script), "--dir", str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert res_warn.returncode == 0, f"Expected warning-only exit 0 without --strict-ip: {res_warn.stdout}"
        assert "paimon_master.png" in res_warn.stdout
        assert "terrain_n2_v4.npz" in res_warn.stdout

        res_strict = subprocess.run(
            [sys.executable, str(audit_script), "--dir", str(root), "--strict-ip"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert res_strict.returncode == 2, f"Expected strict-ip failure exit 2, got {res_strict.returncode}"
        assert "[strict-ip]" in res_strict.stdout

    # 4. Run audit_release.py against real git repo (without --strict-ip) -> must exit 0
    res_repo = subprocess.run(
        [sys.executable, str(audit_script)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res_repo.returncode == 0, f"Repo audit_release.py failed:\n{res_repo.stdout}\n{res_repo.stderr}"

    print("  [OK] scripts/audit_release.py leak detection, --strict-ip gating & repo audit passed")


def verify_collect_licenses() -> None:
    script = PROJECT_ROOT / "scripts" / "collect_licenses.py"
    assert script.is_file()

    # Stdout run
    res_stdout = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res_stdout.returncode == 0, f"collect_licenses.py stdout failed: {res_stdout.stderr}"
    assert "| Package | Version | License |" in res_stdout.stdout
    assert "| Python |" in res_stdout.stdout
    assert "numpy" in res_stdout.stdout.lower()
    assert "opencv-python" in res_stdout.stdout.lower()

    # --output & --copy-dir run
    with tempfile.TemporaryDirectory() as tmp:
        out_md = Path(tmp) / "LICENSES_SUMMARY.md"
        copy_dir = Path(tmp) / "licenses"
        res_out = subprocess.run(
            [sys.executable, str(script), "--output", str(out_md), "--copy-dir", str(copy_dir)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert res_out.returncode == 0, f"collect_licenses.py --output failed: {res_out.stderr}"
        assert out_md.is_file()
        summary_text = out_md.read_text(encoding="utf-8")
        assert "| Package | Version | License |" in summary_text
        copied_files = list(copy_dir.rglob("*"))
        assert any(p.is_file() for p in copied_files), "Expected copied license files in --copy-dir"

    print("  [OK] scripts/collect_licenses.py stdout, --output & --copy-dir passed")


def verify_original_compass_icon() -> None:
    icon_path = PROJECT_ROOT / "assets" / "icons" / "app_compass_origin.png"
    assert icon_path.is_file(), f"Missing original icon: {icon_path}"
    with Image.open(icon_path) as img:
        assert img.size == (256, 256), f"Expected (256, 256), got {img.size}"
        assert img.mode == "RGBA", f"Expected RGBA, got {img.mode}"
        arr = np.asarray(img)
        # Transparent corners outside circular badge
        assert int(arr[0, 0, 3]) == 0 and int(arr[0, 255, 3]) == 0
        assert int(arr[255, 0, 3]) == 0 and int(arr[255, 255, 3]) == 0
        # Opaque center badge
        assert int(arr[128, 128, 3]) == 255
        # Verify rich palette (non-flat RGB)
        opaque = arr[arr[:, :, 3] > 200][:, :3]
        assert float(opaque.std()) > 35.0, "Icon RGB variance unexpectedly low"

    print("  [OK] assets/icons/app_compass_origin.png is valid 256x256 RGBA original icon")


def verify_public_docs_and_templates() -> None:
    required_files = [
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "README.md",
        "README.vi.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "config.example.json",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ]
    texts: dict[str, str] = {}
    for rel in required_files:
        path = PROJECT_ROOT / rel
        assert path.is_file(), f"Missing required public file: {rel}"
        content = path.read_text(encoding="utf-8")
        assert len(content.strip()) > 50, f"Public file looks unexpectedly short: {rel}"
        texts[rel] = content

    # LICENSE check
    assert "MIT License" in texts["LICENSE"]
    assert "2026 Genshin-Helper Contributors" in texts["LICENSE"]

    # THIRD_PARTY_NOTICES check
    notices = texts["THIRD_PARTY_NOTICES.md"]
    assert "GengGode/cvAutoTrack" in notices and "336a4a6" in notices
    assert "MaxWay12/Genshin-Navigator" in notices and "99e1e13" in notices
    assert "HoYoverse" in notices and "excluded from" in notices.lower()

    # Bilingual README checks (3 hard OS/game limits + anti-cheat + cross-links)
    for key, hard_terms in (
        ("README.md", ("README.vi.md", "Exclusive Fullscreen", "UIPI", "Auto-HDR", "SendInput")),
        ("README.vi.md", ("README.md", "Exclusive Fullscreen", "UIPI", "Auto-HDR", "SendInput")),
    ):
        for term in hard_terms:
            assert term in texts[key], f"Missing required term '{term}' in {key}"

    # config.example.json valid JSON schema
    cfg = json.loads(texts["config.example.json"])
    for field in ("hotkey", "hotkey_debug", "hotkey_switch", "ws_port", "auto_sync_m", "single_monitor_mode"):
        assert field in cfg, f"Missing config.example.json field: {field}"

    # CI workflow checks
    ci = texts[".github/workflows/ci.yml"]
    for token in (
        "windows-latest",
        "py_compile",
        "verify_v45_release.py",
        "verify_single_monitor.py",
        "verify_public_readiness_v5.py",
        "scripts/audit_release.py",
    ):
        assert token in ci, f"Missing CI step/token '{token}' in .github/workflows/ci.yml"

    # Issue template Alt+F12 UID blur check
    bug_tpl = texts[".github/ISSUE_TEMPLATE/bug_report.md"]
    assert "Alt+F12" in bug_tpl and "UID" in bug_tpl

    # Ported MIT attribution headers check
    for ported_rel in (
        "src/asset_setup.py",
        "scripts/audit_release.py",
        "scripts/collect_licenses.py",
    ):
        first_line = (PROJECT_ROOT / ported_rel).read_text(encoding="utf-8").splitlines()[0]
        assert "Ported from MaxWay12/Genshin-Navigator@99e1e13:" in first_line
        assert "(MIT License)" in first_line

    # Verify audit_release inspect_file passes with zero failures on EVERY Track 3C file
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import audit_release

    track_files = required_files + [
        "src/asset_setup.py",
        "scripts/audit_release.py",
        "scripts/collect_licenses.py",
        "assets/icons/app_compass_origin.png",
        "tests/verify_public_readiness_v5.py",
    ]
    for rel in track_files:
        failures, warnings = audit_release.inspect_file(PROJECT_ROOT / rel, root=PROJECT_ROOT)
        assert not failures, f"audit_release reported failures on Track 3C file {rel}: {failures}"
        assert not warnings, f"audit_release reported IP warnings on Track 3C file {rel}: {warnings}"

    print("  [OK] All public docs, CI workflow, templates, MIT headers & self-audit passed")


def main() -> int:
    print("Running Genshin-Helper v5 Track 3C Public Readiness Verification...")
    verify_asset_setup_promote_and_rollback()
    verify_audit_release()
    verify_collect_licenses()
    verify_original_compass_icon()
    verify_public_docs_and_templates()
    print("ALL PUBLIC READINESS v5 CHECKS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
