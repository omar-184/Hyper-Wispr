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

[InstallDelete]
; Libraries from the previous version: left in place, old and new copies of a package can mix.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Hyper-Wispr\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Hyper-Wispr"; Filename: "{app}\Hyper-Wispr.exe"
Name: "{group}\Uninstall Hyper-Wispr"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Hyper-Wispr"; Filename: "{app}\Hyper-Wispr.exe"; Tasks: desktopicon
; --minimized: at login it waits in the tray instead of opening its window.
Name: "{userstartup}\Hyper-Wispr"; Filename: "{app}\Hyper-Wispr.exe"; Parameters: "--minimized"; Tasks: startup

[Run]
Filename: "{app}\Hyper-Wispr.exe"; Description: "Start Hyper-Wispr now"; Flags: nowait postinstall skipifsilent
; An update started from inside the app runs silently: start the new version when it's done.
Filename: "{app}\Hyper-Wispr.exe"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
; "Open at login" in Settings may have created this even if the installer task was unticked.
Type: files; Name: "{userstartup}\Hyper-Wispr.lnk"

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM Hyper-Wispr.exe /F"; Flags: runhidden; RunOnceId: "StopApp"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { The tray app and its window hold files open; close them so every file can be replaced. }
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM Hyper-Wispr.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { The history can hold patient information, so offer to remove it rather than leave it behind. }
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('Also delete your Hyper-Wispr settings and dictation history from this PC?',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\HyperWispr'), True, True, True);
end;
