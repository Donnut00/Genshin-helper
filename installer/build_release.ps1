<#
.SYNOPSIS
    Builds Genshin Helper Windows bundle (PyInstaller) and Setup Installer (Inno Setup 6) with SHA-256 checksums.
#>
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

if ($env:PYTHON_EXE -and (Test-Path $env:PYTHON_EXE)) {
    $PyExe = $env:PYTHON_EXE
} elseif (Test-Path (Join-Path $RootDir ".venv\Scripts\python.exe")) {
    $PyExe = Join-Path $RootDir ".venv\Scripts\python.exe"
} else {
    $PyCmd = Get-Command "python" -ErrorAction SilentlyContinue
    if (-not $PyCmd) {
        throw "Missing Python executable (set PYTHON_EXE or create .venv)"
    }
    $PyExe = $PyCmd.Source
}

Write-Host "[1/5] Verifying Extension Key <-> Native Host ID <-> App/Installer Version consistency..." -ForegroundColor Cyan
$AppVersion = (& $PyExe -c @"
import base64, hashlib, json, re
m = json.load(open('extension/manifest.json', encoding='utf-8'))
ver = str(m.get('version', '')).strip()
cfg_txt = open('src/config.py', encoding='utf-8').read()
cfg_match = re.search(r'APP_VERSION(?:\s*:\s*str)?\s*=\s*"([^"]+)"', cfg_txt)
assert cfg_match and cfg_match.group(1) == ver, f'src/config.py APP_VERSION {cfg_match.group(1) if cfg_match else None} != {ver}'
der = base64.b64decode(m['key'])
ext_id = ''.join(chr(97+(b>>4))+chr(97+(b&15)) for b in hashlib.sha256(der).digest()[:16])
nh = json.load(open('installer/native_host_manifest.json', encoding='utf-8'))
expected_origin = f'chrome-extension://{ext_id}/'
assert expected_origin in nh['allowed_origins'], f'{expected_origin} not in {nh}'
iss = open('installer/GenshinHelper.iss', encoding='utf-8').read()
assert ext_id in iss, f'{ext_id} missing from GenshinHelper.iss'
match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss)
assert match and match.group(1) == ver, f'Inno version {match.group(1) if match else None} != {ver}'
print(ver)
"@).Trim()
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "  Version & pinned Extension ID verified: v$AppVersion" -ForegroundColor Green

Write-Host "[2/5] Checking PyInstaller..." -ForegroundColor Cyan
& $PyExe -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Install with: $PyExe -m pip install pyinstaller"
}

Write-Host "[3/5] Running PyInstaller dual-EXE build (GenshinHelper.exe + GenshinHelperHost.exe)..." -ForegroundColor Cyan
& $PyExe -m PyInstaller --clean --noconfirm (Join-Path $PSScriptRoot "GenshinHelper.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/5] Bundling license notices and auditing built dist\GenshinHelper artifact..." -ForegroundColor Cyan
Copy-Item "LICENSE", "THIRD_PARTY_NOTICES.md" -Destination "dist\GenshinHelper" -Force
& $PyExe scripts/collect_licenses.py --output dist/GenshinHelper/THIRD_PARTY_LICENSES.md --copy-dir dist/GenshinHelper/licenses
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $PyExe scripts/audit_release.py --strict-ip --artifact dist/GenshinHelper
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[5/5] Locating Inno Setup 6 (ISCC.exe)..." -ForegroundColor Cyan
$IsccCandidates = @(
    (Get-Command "iscc.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

$Iscc = $IsccCandidates | Select-Object -First 1
if (-not $Iscc) {
    throw "ISCC.exe (Inno Setup 6) not found. Install Inno Setup 6 to build the Setup executable."
}

& $Iscc (Join-Path $PSScriptRoot "GenshinHelper.iss")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$SetupExe = Join-Path $RootDir "dist\installer\GenshinHelper-Setup-v$AppVersion.exe"
if (-not (Test-Path $SetupExe)) {
    throw "Expected installer output missing: $SetupExe"
}

$Hash = (Get-FileHash -Path $SetupExe -Algorithm SHA256).Hash.ToLowerInvariant()
$Leaf = Split-Path -Leaf $SetupExe
"$Hash  $Leaf" | Out-File -FilePath "$SetupExe.sha256" -Encoding utf8
"$Hash  $Leaf" | Out-File -FilePath (Join-Path $RootDir "dist\installer\SHA256SUMS.txt") -Encoding utf8
Write-Host "BUILD COMPLETE: $SetupExe" -ForegroundColor Green
Write-Host "SHA-256: $Hash" -ForegroundColor Green
