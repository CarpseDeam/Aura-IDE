; Aura Installer for Windows
; Requires Inno Setup 6+ (https://jrsoftware.org/isdl.php)
; Compile: iscc.exe /DMyAppVersion=1.4.5 /DSourceDir=..\..\build\Aura.dist Aura.iss

#define MyAppName "Aura"
#define MyAppVersion "1.0.0"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "CarpseDeam"
#define MyAppURL "https://github.com/CarpseDeam/Aura-IDE"
#define MyAppExeName "Aura.exe"
#ifndef SourceDir
  #define SourceDir "..\..\build\Aura.dist"
#endif

[Setup]
AppId={{8A7E4B2C-F3D9-4E1A-9B5C-7D2E8F1A3B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\..\build
OutputBaseFilename=AuraSetup-{#MyAppVersion}
SetupIconFile=..\..\media\AurA.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifdoesntexist; Check: AutoUpdateLaunch

[Code]
function AutoUpdateLaunch: Boolean;
var
  CmdValue: String;
begin
  CmdValue := ExpandConstant('{param:LAUNCHAFTERUPDATE|0}');
  Result := (CmdValue = '1');
end;
