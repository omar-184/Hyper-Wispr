; Inno Setup script. Built by .github/workflows/release.yml:
;   iscc /DAppVersion=0.1.0 /DVariant=GPU packaging\installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Variant
  #define Variant "CPU"
#endif
#if Variant == "GPU"
  #define Suffix "-NVIDIA"
#else
  #define Suffix ""
#endif

[Setup]
AppId={{6B0E8C59-3F0B-4C49-9A51-7C2C5D1E8A11}
AppName=Hyper-Wispr
AppVersion={#AppVersion}
AppPublisher=Hyper-Wispr
AppPublisherURL=https://github.com/omar-184/Hyper-Wispr
DefaultDirName={localappdata}\Programs\Hyper-Wispr
DefaultGroupName=Hyper-Wispr
DisableProgramGroupPage=yes
; Installs for the current user only: no administrator password needed.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Hyper-Wispr-Setup-{#AppVersion}{#Suffix}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Hyper-Wispr.exe
Compression=lzma2/max
SolidCompression=yes
DiskSpanning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=force

[Tasks]
Name: "startup"; Description: "Start Hyper-Wispr automatically when I log in"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\dist\Hyper-Wispr\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Hyper-Wispr"; Filename: "{app}\Hyper-Wispr.exe"
Name: "{group}\Uninstall Hyper-Wispr"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Hyper-Wispr"; Filename: "{app}\Hyper-Wispr.exe"; Tasks: desktopicon
Name: "{userstartup}\Hyper-Wispr"; Filename: "{app}\Hyper-Wispr.exe"; Tasks: startup

[Run]
Filename: "{app}\Hyper-Wispr.exe"; Description: "Start Hyper-Wispr now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM Hyper-Wispr.exe /F"; Flags: runhidden; RunOnceId: "StopApp"
