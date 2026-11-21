<#
.SYNOPSIS
    Registers (or unregisters) the Genshin Helper Native Messaging Host (com.genshinhelper.host)
    for local dev / source-repo testing (when running via start.bat instead of the Setup Installer).

.DESCRIPTION
    - Writes a clean stdio wrapper (.cmd) and Chrome/Edge/Brave Native Messaging manifest (.json)
      into %LOCALAPPDATA%\GenshinHelper\dev_native_host\.
    - Registers HKCU NativeMessagingHosts keys for Chrome, Microsoft Edge, and Brave (no Admin needed).
    - Optional -WithElevatedTask (requires Administrator): creates the GenshinHelper_Elevated
      Scheduled Task pointing to .venv\Scripts\pythonw.exe main.py --minimized so the Chrome Popup
      Power Toggle wakes the Desktop Engine with Administrator privileges and zero UAC prompts.
    - Optional -Uninstall: removes HKCU keys, dev_native_host files, and GenshinHelper_Elevated task.

.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/register_dev_native_host.ps1
.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/register_dev_native_host.ps1 -WithElevatedTask
.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/register_dev_native_host.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$WithElevatedTask,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$HostName = "com.genshinhelper.host"
$TaskName = "GenshinHelper_Elevated"
$RootDir = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
$VenvPythonW = Join-Path $RootDir ".venv\Scripts\pythonw.exe"
$HostPy = Join-Path $RootDir "src\native_host.py"
$MainPy = Join-Path $RootDir "main.py"
$ExtManifestPath = Join-Path $RootDir "extension\manifest.json"

$DevHostDir = Join-Path $env:LOCALAPPDATA "GenshinHelper\dev_native_host"
$WrapperCmd = Join-Path $DevHostDir "native_host_dev.cmd"
$ManifestJsonPath = Join-Path $DevHostDir "$HostName.json"

$RegPaths = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName",
    "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\$HostName"
)

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Uninstall) {
    Write-Host "[Uninstall] Removing dev Native Messaging Host registration..." -ForegroundColor Yellow
    foreach ($rp in $RegPaths) {
        if (Test-Path $rp) {
            Remove-Item -Path $rp -Force
            Write-Host "  Removed registry key: $rp"
        }
    }
    if (Test-Path $DevHostDir) {
        Remove-Item -Path $DevHostDir -Recurse -Force
        Write-Host "  Removed directory: $DevHostDir"
    }
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        if (Test-IsAdmin) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "  Removed Scheduled Task: $TaskName"
        } else {
            Write-Warning "Scheduled Task '$TaskName' still exists; re-run with Administrator privileges to remove it."
        }
    }
    Write-Host "[OK] Dev Native Messaging Host uninstalled." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $VenvPython)) {
    throw "Missing virtualenv Python at: $VenvPython"
}
if (-not (Test-Path $HostPy)) {
    throw "Missing native_host.py at: $HostPy"
}
if (-not (Test-Path $ExtManifestPath)) {
    throw "Missing extension/manifest.json at: $ExtManifestPath"
}

# Derive pinned Extension ID from extension/manifest.json public key
$ExtId = (& $VenvPython -c @"
import base64, hashlib, json, sys
m = json.load(open(sys.argv[1], encoding='utf-8'))
der = base64.b64decode(m['key'])
print(''.join(chr(97 + (b >> 4)) + chr(97 + (b & 15)) for b in hashlib.sha256(der).digest()[:16]))
"@ $ExtManifestPath).Trim()

if ($LASTEXITCODE -ne 0 -or $ExtId.Length -ne 32) {
    throw "Failed to derive pinned Extension ID from $ExtManifestPath (got '$ExtId')"
}

New-Item -ItemType Directory -Path $DevHostDir -Force | Out-Null

# Write UTF-8 (no BOM) stdio wrapper (.cmd) - @echo off is mandatory to keep Chrome's 4-byte binary framing clean
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$WrapperContent = "@echo off`r`n`"$VenvPython`" -u `"$HostPy`" %*`r`n"
[System.IO.File]::WriteAllText($WrapperCmd, $WrapperContent, $Utf8NoBom)

$ManifestObj = [ordered]@{
    name = $HostName
    description = "Genshin Helper Dev Native Messaging Host (Wake & Control Desktop Sync Server)"
    path = $WrapperCmd
    type = "stdio"
    allowed_origins = @(
        "chrome-extension://$ExtId/"
    )
}
$ManifestJson = ($ManifestObj | ConvertTo-Json -Depth 4) + "`r`n"
[System.IO.File]::WriteAllText($ManifestJsonPath, $ManifestJson, $Utf8NoBom)

foreach ($rp in $RegPaths) {
    New-Item -Path $rp -Force | Out-Null
    Set-ItemProperty -Path $rp -Name "(Default)" -Value $ManifestJsonPath
    Write-Host "  Registered: $rp" -ForegroundColor Cyan
}

if ($WithElevatedTask) {
    if (-not (Test-IsAdmin)) {
        Write-Warning "Skipping Elevated Scheduled Task '$TaskName': please re-run this script as Administrator with -WithElevatedTask."
    } else {
        $PyLauncher = if (Test-Path $VenvPythonW) { $VenvPythonW } else { $VenvPython }
        $Action = New-ScheduledTaskAction -Execute $PyLauncher -Argument "`"$MainPy`" --minimized" -WorkingDirectory $RootDir
        $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
        $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0)
        Register-ScheduledTask -TaskName $TaskName -Action $Action -Principal $Principal -Settings $Settings -Force | Out-Null
        Write-Host "  Registered Elevated Scheduled Task: $TaskName -> $PyLauncher `"$MainPy`" --minimized" -ForegroundColor Green
    }
}

# Smoke-test 4-byte Native Messaging stdio ping against the generated wrapper
$PingOut = & $VenvPython -c @"
import json, struct, subprocess, sys
cmd = sys.argv[1]
payload = json.dumps({'cmd': 'ping'}).encode('utf-8')
proc = subprocess.Popen([cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = proc.communicate(struct.pack('<I', len(payload)) + payload, timeout=5)
assert len(out) >= 4, f'Short stdout ({len(out)} bytes), stderr={err!r}'
(n,) = struct.unpack('<I', out[:4])
resp = json.loads(out[4:4+n].decode('utf-8'))
assert resp.get('ok') is True, resp
print('NATIVE_HOST_PING_OK:', json.dumps(resp, ensure_ascii=False))
"@ $WrapperCmd

if ($LASTEXITCODE -ne 0) {
    throw "Native Host stdio ping self-test failed."
}

Write-Host "  $PingOut" -ForegroundColor Green
Write-Host "[OK] Dev Native Messaging Host ready (Extension ID: $ExtId)." -ForegroundColor Green
Write-Host "     Manifest: $ManifestJsonPath"
Write-Host "     Wrapper : $WrapperCmd"
