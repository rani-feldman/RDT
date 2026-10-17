; Inno Setup script for the RDT installer (INS-1 to INS-6). Built by tools/build_release.ps1:
;     iscc /DAppVersion=1.2.0 /DSourceDir=C:\...\dist\RDT /DOutputDir=C:\...\release packaging\rdt.iss
; Per user, no admin rights: %LOCALAPPDATA%\Programs\RDT. A new setup closes RDT, uninstalls the
; old version without asking, then installs. User files in %LOCALAPPDATA%\rdt are never touched,
; except when a manual uninstall is told to delete them.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\RDT"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

; Never change the AppId: it is how a new setup finds the installed RDT.
#define AppId "{{6E3F2A91-5C4B-4D1E-9B7A-2F8C0D41A7E3}"
#define AppName "RDT"
#define ExeName "rdt-gui.exe"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Rani Feldman
AppPublisherURL=https://github.com/rani-feldman/rdt
AppSupportURL=https://github.com/rani-feldman/rdt/issues
AppUpdatesURL=https://github.com/rani-feldman/rdt/releases
AppCopyright=Copyright (c) 2026 Rani Feldman
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription=RDT Remote Debugger Tool setup
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
DefaultDirName={autopf}\{#AppName}
DisableDirPage=auto
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
OutputDir={#OutputDir}
OutputBaseFilename=RDT-Setup-{#AppVersion}
SetupIconFile=..\rdt\gui\assets\rdt.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName} {#AppVersion}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
ChangesEnvironment=yes
AppMutex=RDT.RemoteDebuggerTool
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add rdt to PATH, so the command line works in any terminal"; GroupDescription: "Command line:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; the PyInstaller folder, with the example workspace and simulator files build_release.ps1 adds to it
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autoprograms}\{#AppName} simulator"; Filename: "{app}\rdt-sim.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Registry]
; .rdtw workspaces open in RDT, with its icon (INS-5)
Root: HKA; Subkey: "Software\Classes\.rdtw"; ValueType: string; ValueName: ""; ValueData: "RDT.Workspace"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\RDT.Workspace"; ValueType: string; ValueName: ""; ValueData: "RDT workspace"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\RDT.Workspace\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#ExeName},0"
Root: HKA; Subkey: "Software\Classes\RDT.Workspace\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#ExeName}"" ""%1"""

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\examples"
Type: dirifempty; Name: "{app}"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1';
  EnvKey = 'Environment';

{ --- the version already installed ------------------------------------------------------------ }

function InstalledValue(const Name: String): String;
begin
  Result := '';
  if not RegQueryStringValue(HKCU, UninstallKey, Name, Result) then
    RegQueryStringValue(HKLM, UninstallKey, Name, Result);
end;

{ -1, 0 or 1 as version A is older, the same or newer than B (numbers separated by dots) }
function CompareVersions(A, B: String): Integer;
var
  NA, NB, P: Integer;
begin
  Result := 0;
  while (Result = 0) and ((A <> '') or (B <> '')) do
  begin
    P := Pos('.', A);
    if P = 0 then begin NA := StrToIntDef(A, 0); A := ''; end
    else begin NA := StrToIntDef(Copy(A, 1, P - 1), 0); Delete(A, 1, P); end;
    P := Pos('.', B);
    if P = 0 then begin NB := StrToIntDef(B, 0); B := ''; end
    else begin NB := StrToIntDef(Copy(B, 1, P - 1), 0); Delete(B, 1, P); end;
    if NA < NB then Result := -1
    else if NA > NB then Result := 1;
  end;
end;

{ Installing an older version over a newer one warns, then lets you go on (INS-3) }
function InitializeSetup(): Boolean;
var
  Installed: String;
begin
  Result := True;
  Installed := InstalledValue('DisplayVersion');
  if (Installed <> '') and (CompareVersions('{#AppVersion}', Installed) < 0) and not WizardSilent() then
    Result := MsgBox('RDT ' + Installed + ' is installed, which is newer than this setup (RDT {#AppVersion}).' + #13#10#13#10 +
                     'You are installing an older version. It may not read settings the newer version saved.' + #13#10#13#10 +
                     'Install RDT {#AppVersion} anyway?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

{ A new setup removes the old version first, silently, so its settings are kept (INS-3) }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Uninstaller: String;
  Code: Integer;
begin
  Result := '';
  Uninstaller := RemoveQuotes(InstalledValue('UninstallString'));
  if (Uninstaller <> '') and FileExists(Uninstaller) then
    if not Exec(Uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      Result := 'The installed RDT could not be removed (code ' + IntToStr(Code) + '). Close RDT and try again.';
end;

{ --- rdt on the user's PATH (INS-4) ------------------------------------------------------------ }

function PathHas(const Paths, Dir: String): Boolean;
begin
  Result := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Paths) + ';') > 0;
end;

procedure AddToPath(const Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then Paths := '';
  if PathHas(Paths, Dir) then Exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then Paths := Paths + ';';
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(const Dir: String);
var
  Paths, Rest, Part, Kept: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then Exit;
  if not PathHas(Paths, Dir) then Exit;
  Rest := Paths;
  Kept := '';
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then begin Part := Rest; Rest := ''; end
    else begin Part := Copy(Rest, 1, P - 1); Delete(Rest, 1, P); end;
    if (Part <> '') and (Lowercase(Part) <> Lowercase(Dir)) then
    begin
      if Kept <> '' then Kept := Kept + ';';
      Kept := Kept + Part;
    end;
  end;
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Kept);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('addtopath') then
      AddToPath(ExpandConstant('{app}'))
    else
      RemoveFromPath(ExpandConstant('{app}'));
  end;
end;

{ --- uninstall: PATH out; the user's settings only when they say so (INS-2) ----------------------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Settings: String;
begin
  if CurUninstallStep = usUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
  if CurUninstallStep = usPostUninstall then
  begin
    Settings := ExpandConstant('{localappdata}\rdt');
    { an upgrade runs this uninstaller silently, so it never asks and always keeps them }
    if DirExists(Settings) and not UninstallSilent() then
      if MsgBox('Also delete your RDT settings and workspaces?' + #13#10#13#10 + Settings + #13#10#13#10 +
                'Choose No to keep them for a later install.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Settings, True, True, True);
  end;
end;
