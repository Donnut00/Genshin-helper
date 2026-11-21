# Contributing to Genshin-Helper

Thank you for your interest in improving Genshin-Helper! Because this project interacts alongside a live game on Windows and is distributed publicly under the MIT License, every contribution must uphold our **anti-cheat safety**, **license/IP hygiene**, and **verification** invariants.

---

## 1. Development Environment (Windows 10/11 64-bit)

```powershell
git clone https://github.com/<owner>/Genshin-Helper.git
cd Genshin-Helper
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pillow
Copy-Item config.example.json config.json
```

Hydrate local HoYoLAB map assets into your local cache (or `src/data/`):

```powershell
.\.venv\Scripts\python.exe scripts/sync_hoyolab_data.py
```

Always execute Python scripts, syntax checks, and tests through `.\.venv\Scripts\python.exe`, and pass `encoding="utf-8"` on every Python text file read/write.

---

## 2. Non-Negotiable Engineering Invariants

1. **Passive Computer Vision Only (Anti-Cheat Safety):**
   - **NEVER** read/write game memory, inject DLLs, or hook game rendering pipelines.
   - **NEVER** call `SendInput` (or equivalent synthetic input APIs) against the game window.
2. **Preserve `DECISION:` Invariants:**
   - Do not modify or delete code annotated with `DECISION:` without understanding and preserving the documented invariant.
   - Keep `src/zoom_slider.py` dual-gate verification (`TM_CCORR_NORMED` + zero-mean `TM_CCOEFF_NORMED >= 0.65`).
   - Keep `src/server.py` `/poll?since=<seq>` lossless monotonic ring buffer intact.
   - Keep explicit 64-bit `ctypes` `argtypes` / `restype` declarations on every Win32 API binding (`src/hotkey.py`, `src/capture.py`, `src/single_monitor.py`).
   - Keep the pinned RSA-2048 SPKI `"key"` in `extension/manifest.json` (ID `nfkijdoblmphmdljfogooajhbldkeojh`) synchronized with `installer/native_host_manifest.json` and `installer/GenshinHelper.iss`.
3. **License & IP Hygiene (Public MIT Repository):**
   - Submit only original MIT-compatible code, or properly attributed ports from MIT/BSD/Apache-2.0 repositories with a top-of-file attribution header (`# Ported from <repo>@<commit>:<path> (MIT License)`) and an entry in `THIRD_PARTY_NOTICES.md`.
   - **NEVER** copy code from AGPL, GPL, or unlicensed ("all rights reserved") repositories.
   - **NEVER** commit copyrighted HoYoverse artwork, game icons, HoYoLAB map tiles, `src/data/*.npz`, personal cookies/tokens, or `scratch/` diagnostic dumps.

---

## 3. Required Pre-PR Checks

Run all checks locally before opening a Pull Request:

```powershell
# 1. Bytecode compile check
Get-ChildItem -Recurse -Include *.py -Path main.py,src,scripts,tests | ForEach-Object {
    .\.venv\Scripts\python.exe -m py_compile $_.FullName
}

# 2. Core verification suites
.\.venv\Scripts\python.exe tests/verify_v45_release.py
.\.venv\Scripts\python.exe tests/verify_single_monitor.py
.\.venv\Scripts\python.exe tests/verify_public_readiness_v5.py

# 3. Release privacy & IP audit
.\.venv\Scripts\python.exe scripts/audit_release.py
```

For pre-public release pushes, also verify IP cleanliness with:

```powershell
.\.venv\Scripts\python.exe scripts/audit_release.py --strict-ip
```

---

## 4. Pull Request Guidelines

- Keep PRs focused, well-scoped, and accompanied by a deterministic offline verification check when adding non-trivial logic.
- Update both `README.md` (English) and `README.vi.md` (Vietnamese) when changing user-facing features, hotkeys, or compatibility behavior.
- Fill out `.github/PULL_REQUEST_TEMPLATE.md` honestly; never claim a test passed without running it.
