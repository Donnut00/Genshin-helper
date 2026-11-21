; Genshin Helper Off-Store Public Installer (Inno Setup 6)
#define MyAppName "Genshin Helper"
#define MyAppVersion "5.3.0"
#define MyAppPublisher "Genshin Helper"
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Tao bieu tuong tren Desktop (Create a Desktop shortcut)"; GroupDescription: "Tuy chon:"
Name: "autostart"; Description: "Tu chay ngam o khay he thong khi khoi dong Windows (Auto-start in System Tray)"; GroupDescription: "Tuy chon:"

[Files]
Source: "..\dist\GenshinHelper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "install_extension.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "native_host_manifest.json"; DestDir: "{app}"; DestName: "{#NativeHostName}.json"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Huong dan cai dat Browser Extension"; Filename: "{app}\install_extension.html"
Name: "{group}\Go cai dat {#MyAppName}"; Filename: "{uninstallexe}"
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
Filename: "schtasks.exe"; Parameters: "/run /tn ""{#ElevatedTaskName}"""; Description: "Khoi chay Genshin Helper ngay (chay ngam o khay he thong)"; Flags: runhidden nowait postinstall skipifsilent
Filename: "{app}\install_extension.html"; Description: "Mo huong dan cai dat Browser Extension (Chrome / Edge / Brave)"; Flags: shellexec postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/f /im {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillMainApp"
Filename: "taskkill.exe"; Parameters: "/f /im {#MyHostExeName}"; Flags: runhidden; RunOnceId: "KillHostApp"
Filename: "schtasks.exe"; Parameters: "/delete /f /tn ""{#ElevatedTaskName}"""; Flags: runhidden; RunOnceId: "DeleteElevTask"

[UninstallDelete]
Type: files; Name: "{app}\extension_path.js"

[Code]
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
