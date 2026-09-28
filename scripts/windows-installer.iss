#ifndef AppVersion
  #define AppVersion "1.0.4"
#endif

[Setup]
AppId={{1B33715D-9EB3-4F90-A9B7-7BE835C3476D}
AppName=Timelapse Manager
AppVersion={#AppVersion}
AppPublisher=Timelapse Manager
DefaultDirName={autopf}\TimelapseManager
DefaultGroupName=Timelapse Manager
OutputDir=..\build\installer
OutputBaseFilename=TimelapseManager-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
UninstallDisplayIcon={app}\TimelapseManager.exe

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Files]
Source: "..\build\pyinstaller\TimelapseManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\install-windows.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\start-windows.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Icons]
Name: "{group}\Desinstalar Timelapse Manager"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install-windows.ps1"""; Description: "Configurar e iniciar o serviço"; Flags: postinstall waituntilterminated

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Stop-ScheduledTask -TaskName 'Timelapse Manager' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'Timelapse Manager' -Confirm:$false -ErrorAction SilentlyContinue; Remove-NetFirewallRule -DisplayName 'Timelapse Manager HTTPS' -ErrorAction SilentlyContinue"""; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}"