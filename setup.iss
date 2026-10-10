; The Windows installer. Build the application first, then compile this
; script with Inno Setup 6:
;
;   cargo build --release -p loanmaster
;   iscc setup.iss
;
; The installer lands in build\LoanMasterSetup.exe.

#define MyAppName "LoanMaster"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "LoanMaster Team"
#define MyAppExeName "LoanMaster.exe"
#define MyBuild "target\release"

[Setup]
; 1.x's AppId: installing 2.0 upgrades an existing installation in place.
AppId={{A3560799-E8C6-4C3D-9457-375498852391}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=build
OutputBaseFilename=LoanMasterSetup
SetupIconFile=resources\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; What the 1.x (Python) build left in the program folder. Only program files
; are named here, never a wildcard over the folder, so a journal someone saved
; beside the program is left alone.
Type: files; Name: "{app}\*.dll"
Type: files; Name: "{app}\*.pyd"
Type: filesandordirs; Name: "{app}\PyQt6"
Type: filesandordirs; Name: "{app}\numpy"
Type: filesandordirs; Name: "{app}\pandas"
Type: filesandordirs; Name: "{app}\dateutil"
Type: filesandordirs; Name: "{app}\src"

[Files]
Source: "{#MyBuild}\loanmaster.exe"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "resources\nairobi_water_sacco_logo.png"; DestDir: "{app}\resources"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
