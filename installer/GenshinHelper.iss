; Genshin Helper Off-Store Public Installer (Inno Setup 6)
#define MyAppName "Genshin Helper"
#define MyAppVersion "5.5.0"
#define MyAppPublisher "Genshin-Helper Contributors"
#define MyAppExeName "GenshinHelper.exe"
#define MyHostExeName "GenshinHelperHost.exe"
#define FixedExtensionId "nfkijdoblmphmdljfogooajhbldkeojh"
#define NativeHostName "com.genshinhelper.host"
#define ElevatedTaskName "GenshinHelper_Elevated"

[Setup]
AppId={{8F6B7A43-4D7E-4C19-9A2B-1E4500B45000}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=..\dist\installer
OutputBaseFilename=GenshinHelper-Setup-v{#MyAppVersion}
SetupIconFile=..\assets\icons\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Additional options:"
Name: "autostart"; Description: "Launch quietly in System Tray on Windows logon"; GroupDescription: "Additional options:"

[Files]
Source: "..\dist\GenshinHelper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "install_extension.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "native_host_manifest.json"; DestDir: "{app}"; DestName: "{#NativeHostName}.json"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Browser Extension Setup Guide"; Filename: "{app}\install_extension.html"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Native Messaging Host manifest pointers for Chrome, Edge, and Brave (HKLM + HKCU)
Root: HKLM; Subkey: "Software\Google\Chrome\NativeMessagingHosts\{#NativeHostName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#NativeHostName}.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\{#NativeHostName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#NativeHostName}.json"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\{#NativeHostName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#NativeHostName}.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\{#NativeHostName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#NativeHostName}.json"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{#NativeHostName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#NativeHostName}.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{#NativeHostName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#NativeHostName}.json"; Flags: uninsdeletekey

[Run]
; Elevated Scheduled Task allows Extension Native Host to wake/start GenshinHelper silently with zero UAC prompt
Filename: "schtasks.exe"; Parameters: "/create /f /rl HIGHEST /sc ONLOGON /tn ""{#ElevatedTaskName}"" /tr ""\""{app}\{#MyAppExeName}\"" --minimized"""; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "schtasks.exe"; Parameters: "/create /f /rl HIGHEST /sc ONCE /sd 01/01/2000 /st 00:00 /tn ""{#ElevatedTaskName}"" /tr ""\""{app}\{#MyAppExeName}\"" --minimized"""; Tasks: not autostart; Flags: runhidden waituntilterminated

; Post-install actions on Finish page
Filename: "schtasks.exe"; Parameters: "/run /tn ""{#ElevatedTaskName}"""; Description: "Launch Genshin Helper in System Tray now"; Flags: runhidden nowait postinstall skipifsilent
Filename: "{app}\install_extension.html"; Description: "Open Browser Extension setup guide (Chrome / Edge / Brave)"; Flags: shellexec postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/f /im {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillMainApp"
Filename: "taskkill.exe"; Parameters: "/f /im {#MyHostExeName}"; Flags: runhidden; RunOnceId: "KillHostApp"
Filename: "schtasks.exe"; Parameters: "/delete /f /tn ""{#ElevatedTaskName}"""; Flags: runhidden; RunOnceId: "DeleteElevTask"

[UninstallDelete]
Type: files; Name: "{app}\extension_path.js"

[Code]
function PostMessageW(hWnd: HWND; Msg: Cardinal; wParam, lParam: LongInt): Integer;
  external 'PostMessageW@user32.dll stdcall';

function IsVietnameseUI(): Boolean;
begin
  Result := (GetUILanguage and $03FF) = $2A;
end;

function IsExeRunningWmi(const ExeName: string): Boolean;
var
  FSWbemLocator, FWMIService, FWbemObjectSet: Variant;
begin
  Result := False;
  try
    FSWbemLocator := CreateOleObject('WbemScripting.SWbemLocator');
    FWMIService := FSWbemLocator.ConnectServer('', 'root\CIMV2', '', '');
    FWbemObjectSet := FWMIService.ExecQuery(
      'SELECT ProcessId FROM Win32_Process WHERE Name = ''' + ExeName + ''''
    );
    Result := (FWbemObjectSet.Count > 0);
  except
    Result := False;
  end;
end;

function IsAnyAppProcessRunning(): Boolean;
begin
  if (FindWindowByWindowName('GenshinHelperTray') <> 0) or
     CheckForMutexes('Local\GenshinHelper_SingleInstance_v4') then
  begin
    Result := True;
    Exit;
  end;
  Result := IsExeRunningWmi('{#MyAppExeName}') or IsExeRunningWmi('{#MyHostExeName}');
end;

procedure TryGracefulHttpShutdown();
var
  HttpReq: Variant;
  TrayHwnd: HWND;
begin
  try
    HttpReq := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    HttpReq.SetTimeouts(400, 400, 800, 1200);
    HttpReq.Open('POST', 'http://127.0.0.1:8765/shutdown', False);
    HttpReq.SetRequestHeader('Content-Type', 'application/json');
    HttpReq.Send('{}');
  except
  end;

  TrayHwnd := FindWindowByWindowName('GenshinHelperTray');
  if TrayHwnd <> 0 then
    PostMessageW(TrayHwnd, $0010, 0, 0);
end;

function StopRunningAppProcesses(): Boolean;
var
  I, ResultCode: Integer;
begin
  TryGracefulHttpShutdown();
  for I := 1 to 12 do
  begin
    if not IsAnyAppProcessRunning() then
    begin
      Result := True;
      Exit;
    end;
    Sleep(200);
  end;

  if IsExeRunningWmi('{#MyAppExeName}') or
     (FindWindowByWindowName('GenshinHelperTray') <> 0) or
     CheckForMutexes('Local\GenshinHelper_SingleInstance_v4') then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  if IsExeRunningWmi('{#MyHostExeName}') then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#MyHostExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;

  for I := 1 to 12 do
  begin
    if not IsAnyAppProcessRunning() then
    begin
      Result := True;
      Exit;
    end;
    Sleep(200);
  end;

  Result := not IsAnyAppProcessRunning();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PromptMsg, AbortMsg, RetryMsg, SilentFailMsg: string;
begin
  Result := '';
  if IsVietnameseUI() then
  begin
    PromptMsg :=
      '{#MyAppName} đang chạy nền (hoặc trong khay hệ thống).' + #13#10 + #13#10 +
      'Bạn có muốn tự động đóng ứng dụng để tiếp tục cài đặt phiên bản {#MyAppVersion} không?';
    AbortMsg := 'Đã hủy cài đặt vì {#MyAppName} vẫn đang chạy. Hãy thoát ứng dụng rồi chạy lại bộ cài.';
    RetryMsg := 'Không thể tự đóng hoàn toàn {#MyAppName}. Hãy thoát ứng dụng từ khay hệ thống rồi bấm Thử lại.';
    SilentFailMsg := 'Không thể đóng tiến trình {#MyAppName} đang chạy khi cài đặt im lặng.';
  end
  else
  begin
    PromptMsg :=
      '{#MyAppName} is currently running in the background (or system tray).' + #13#10 + #13#10 +
      'Would you like to close it automatically to continue installing v{#MyAppVersion}?';
    AbortMsg := 'Installation cancelled because {#MyAppName} is still running. Please quit the app and re-run Setup.';
    RetryMsg := 'Could not automatically stop {#MyAppName}. Please close it manually from the system tray and click Retry.';
    SilentFailMsg := 'Unable to close running {#MyAppName} process during silent install.';
  end;

  while IsAnyAppProcessRunning() do
  begin
    if WizardSilent() then
    begin
      if not StopRunningAppProcesses() then
      begin
        Result := SilentFailMsg;
      end;
      Break;
    end;

    if MsgBox(PromptMsg, mbConfirmation, MB_YESNO) <> IDYES then
    begin
      Result := AbortMsg;
      Exit;
    end;

    if StopRunningAppProcesses() then
      Break;

    if MsgBox(RetryMsg, mbError, MB_RETRYCANCEL) <> IDRETRY then
    begin
      Result := AbortMsg;
      Exit;
    end;
  end;
end;

function InitializeUninstall(): Boolean;
var
  PromptMsg, RetryMsg: string;
begin
  Result := True;
  if IsVietnameseUI() then
  begin
    PromptMsg :=
      '{#MyAppName} đang chạy nền.' + #13#10 + #13#10 +
      'Bạn có muốn tự động đóng ứng dụng trước khi gỡ cài đặt không?';
    RetryMsg := 'Không thể tự đóng {#MyAppName}. Hãy thoát ứng dụng từ khay hệ thống rồi bấm Thử lại.';
  end
  else
  begin
    PromptMsg :=
      '{#MyAppName} is currently running in the background.' + #13#10 + #13#10 +
      'Would you like to close it automatically before uninstalling?';
    RetryMsg := 'Could not automatically stop {#MyAppName}. Please close it from the system tray and click Retry.';
  end;

  while IsAnyAppProcessRunning() do
  begin
    if UninstallSilent() then
    begin
      StopRunningAppProcesses();
      Break;
    end;

    if MsgBox(PromptMsg, mbConfirmation, MB_YESNO) <> IDYES then
    begin
      Result := False;
      Exit;
    end;

    if StopRunningAppProcesses() then
      Break;

    if MsgBox(RetryMsg, mbError, MB_RETRYCANCEL) <> IDRETRY then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function EscapeJsonPath(const S: string): string;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir, HostExe, ExtDir, ManifestPath, PathJsPath, JsonText, JsText: string;
begin
  if CurStep = ssPostInstall then
  begin
    AppDir := ExpandConstant('{app}');
    HostExe := AppDir + '\{#MyHostExeName}';
    ExtDir := AppDir + '\extension';
    ManifestPath := AppDir + '\{#NativeHostName}.json';
    PathJsPath := AppDir + '\extension_path.js';

    JsonText :=
      '{' + #13#10 +
      '  "name": "{#NativeHostName}",' + #13#10 +
      '  "description": "Genshin Helper Native Messaging Host (Wake & Control Desktop Sync Server)",' + #13#10 +
      '  "path": "' + EscapeJsonPath(HostExe) + '",' + #13#10 +
      '  "type": "stdio",' + #13#10 +
      '  "allowed_origins": [' + #13#10 +
      '    "chrome-extension://{#FixedExtensionId}/"' + #13#10 +
      '  ]' + #13#10 +
      '}' + #13#10;
    SaveStringToFile(ManifestPath, JsonText, False);

    JsText := 'window.__GH_EXT_PATH = "' + EscapeJsonPath(ExtDir) + '";' + #13#10;
    SaveStringToFile(PathJsPath, JsText, False);
  end;
end;
