#define MyAppName "Lora POS Returns"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "Tahsinga"
#define MyAppExeName "POSViewer.exe"

[Setup]
AppId={{B6A8C9B4-50BE-4FA0-9BA8-A4F24C8C70F5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Lora POS Returns
DefaultGroupName={#MyAppName}
OutputDir=..\..\installer-output
OutputBaseFilename=LoraPOSReturns-Setup-1.0.1
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\publish\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent