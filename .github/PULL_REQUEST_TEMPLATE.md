## Summary

Briefly describe what this pull request changes and why.

## Related Issue

Closes #(issue)

## Safety, Privacy & License Checklist

- [ ] **Passive CV / Anti-Cheat Invariant:** No game memory reads/writes, no DLL injection, and no `SendInput` against the game window.
- [ ] **Protected Invariants Preserved:** Kept `DECISION:` blocks, explicit 64-bit Win32 `argtypes`/`restype`, lossless `/poll?since=<seq>` ring buffer, `TM_CCORR_NORMED` + `TM_CCOEFF_NORMED >= 0.65` zoom gate, and pinned Extension ID (`nfkijdoblmphmdljfogooajhbldkeojh`).
- [ ] **License & IP Hygiene:** Only original MIT code or properly attributed MIT/permissive ports (with top-of-file attribution + `THIRD_PARTY_NOTICES.md` update); zero copyrighted HoYoverse artwork or `src/data/*.npz` committed; zero AGPL/GPL/unlicensed code copied.
- [ ] **UTF-8 & Windows Compliance:** Passed `encoding="utf-8"` on all Python text reads/writes.

## Verification

Check all verification commands run locally via `.\.venv\Scripts\python.exe`:

- [ ] `py_compile` on all modified Python files
- [ ] `.\.venv\Scripts\python.exe tests/verify_v45_release.py`
- [ ] `.\.venv\Scripts\python.exe tests/verify_single_monitor.py`
- [ ] `.\.venv\Scripts\python.exe tests/verify_public_readiness_v5.py`
- [ ] `.\.venv\Scripts\python.exe scripts/audit_release.py`
- [ ] Updated `README.md` / `README.vi.md` if user-facing behavior changed
