<#
.SYNOPSIS
    Builds Genshin Helper v4.5.0 portable bundle (PyInstaller) and Off-Store Setup Installer (Inno Setup 6).
#>
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Missing virtual environment Python at $VenvPython"
}

Write-Host "[1/4] Verifying Extension Key <-> Native Host ID consistency..." -ForegroundColor Cyan
& $VenvPython -c @"
import base64, hashlib, json
m = json.load(open('extension/manifest.json', encoding='utf-8'))
assert m.get('version') == '4.5.0', m.get('version')
der = base64.b64decode(m['key'])
ext_id = ''.join(chr(97+(b>>4))+chr(97+(b&15)) for b in hashlib.sha256(der).digest()[:16])
nh = json.load(open('installer/native_host_manifest.json', encoding='utf-8'))
expected_origin = f'chrome-extension://{ext_id}/'
assert expected_origin in nh['allowed_origins'], f'{expected_origin} not in {nh}'
iss = open('installer/GenshinHelper.iss', encoding='utf-8').read()
assert ext_id in iss, f'{ext_id} missing from GenshinHelper.iss'
print('KEY_ID_MATCH_OK:', ext_id)
"@
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/4] Checking PyInstaller in .venv..." -ForegroundColor Cyan
& $VenvPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in .venv. Install with: .\.venv\Scripts\pip install pyinstaller"
}

Write-Host "[3/4] Running PyInstaller dual-EXE build (GenshinHelper.exe + GenshinHelperHost.exe)..." -ForegroundColor Cyan
& $VenvPython -m PyInstaller --clean --noconfirm (Join-Path $PSScriptRoot "GenshinHelper.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/4] Locating Inno Setup 6 (ISCC.exe)..." -ForegroundColor Cyan
$IsccCandidates = @(
    (Get-Command "iscc.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

$Iscc = $IsccCandidates | Select-Object -First 1
if (-not $Iscc) {
    Write-Warning "ISCC.exe (Inno Setup 6) not found. Portable PyInstaller bundle is ready at dist\GenshinHelper\."
    exit 0
}

& $Iscc (Join-Path $PSScriptRoot "GenshinHelper.iss")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$SetupExe = Join-Path $RootDir "dist\installer\GenshinHelper-Setup-v4.5.0.exe"
if (Test-Path $SetupExe) {
    $Hash = (Get-FileHash -Path $SetupExe -Algorithm SHA256).Hash
    $ShaFile = "$SetupExe.sha256"
    "$Hash  $(Split-Path -Leaf $SetupExe)" | Out-File -FilePath $ShaFile -Encoding utf8
    Write-Host "BUILD COMPLETE: $SetupExe" -ForegroundColor Green
    Write-Host "SHA-256: $Hash" -ForegroundColor Green
}
