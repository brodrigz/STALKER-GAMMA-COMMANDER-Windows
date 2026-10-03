#ifndef AppVersion
  #error AppVersion must be supplied by Build-Windows.ps1
#endif
#ifndef BundleDir
  #error BundleDir must be supplied by Build-Windows.ps1
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by Build-Windows.ps1
#endif

[Setup]
AppId={{B6289433-BC25-45B1-AF72-B909C3EFC90A}
AppName=STALKER GAMMA Commander
AppVersion={#AppVersion}
AppPublisher=STALKER GAMMA Commander contributors
DefaultDirName={localappdata}\Programs\STALKER GAMMA Commander
DefaultGroupName=STALKER GAMMA Commander
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22000
OutputDir={#OutputDir}
OutputBaseFilename=STALKER-GAMMA-COMMANDER-{#AppVersion}-windows-x64-setup
SetupIconFile={#BundleDir}\commander.ico
UninstallDisplayIcon={app}\STALKER-GAMMA-COMMANDER.exe
LicenseFile={#BundleDir}\LICENSE.txt
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\STALKER GAMMA Commander"; Filename: "{app}\STALKER-GAMMA-COMMANDER.exe"; WorkingDir: "{app}"
Name: "{group}\Commander Assistant"; Filename: "{app}\STALKER-GAMMA-COMMANDER.exe"; Parameters: "--assistant"; WorkingDir: "{app}"
Name: "{autodesktop}\STALKER GAMMA Commander"; Filename: "{app}\STALKER-GAMMA-COMMANDER.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\STALKER-GAMMA-COMMANDER.exe"; Description: "Launch STALKER GAMMA Commander"; Flags: nowait postinstall skipifsilent
