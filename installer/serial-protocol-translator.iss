; Build after scripts\build-windows.bat using Inno Setup 6.
#define AppName "Serial Protocol Translator"
#define AppVersion "0.2.0"
#define AppPublisher "Serial Protocol Translator"
#define AppExeName "GasWorks-ProLab-Serial-Translator.exe"

[Setup]
AppId={{3D27FEA8-365F-4460-96A6-95D9EAA1B0F8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Serial Protocol Translator
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist-installer
OutputBaseFilename=Serial-Protocol-Translator-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}

[Files]
Source: "..\dist\GasWorks-ProLab-Serial-Translator\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
