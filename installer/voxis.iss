; Voxis — Inno Setup installer script.
; Compiled by app/build_official.py after the PyInstaller one-folder build.
; Overridable from the command line, e.g.:
;   ISCC.exe /DMyAppVersion=0.9.0 /DSourceDir="..\dist\VoxisLive" /DOutputDir="..\production_release" installer\voxis.iss

#define MyAppName "Voxis"
#define MyAppExeName "VoxisLive.exe"
#define MyAppPublisher "Voxis"
#define MyAppURL "https://voxislive.com"

#ifndef MyAppVersion
  #define MyAppVersion "0.9.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\VoxisLive"
#endif
#ifndef OutputDir
  #define OutputDir "..\production_release"
#endif

[Setup]
; AppId uniquely identifies the app for upgrades/uninstall — keep it constant across versions.
AppId={{B7B6C7E2-9F4A-4C2E-9A1F-5E3D2C1A8F40}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
SetupIconFile=..\app\assets\voxis.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-machine install into Program Files requires elevation.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=VoxisLive_v{#MyAppVersion}_Setup
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
; In-place upgrade (auto-update): close the running app via the Restart Manager so
; its files can be replaced. We relaunch it ourselves after a silent update (see [Run]).
CloseApplications=yes
RestartApplications=no

; --- Code signing (REQUIRED for release) -------------------------------------
; Setup.exe and the uninstaller are Authenticode-signed so the silent auto-update
; path (app/updater.py -> WinVerifyTrust) can prove the binary is ours before it
; runs elevated. SignTool=voxis below references a named sign tool that the
; MAINTAINER must register once, on the signing host, before running ISCC.
;
;   MAINTAINER setup (one-time, secrets NEVER stored in the repo):
;     1) Install the Voxis EV/OV code-signing certificate on the signing host.
;     2) Register a sign tool named "voxis" in the Inno Setup IDE
;        (Tools > Configure Sign Tools...), pointing at a signtool command such as:
;          signtool sign /fd sha256 /n "Voxis" /tr http://timestamp.digicert.com /td sha256 $f
;     3) BEFORE running ISCC, sign the PyInstaller output (VoxisLive.exe and any
;        other shipped .exe) with the same cert so the installed app is trusted by
;        the auto-updater's Authenticode check. build_official.py should perform
;        this step before invoking ISCC.
;
; With the "voxis" sign tool registered, the two directives below sign Setup.exe
; and the generated uninstaller. Building without the tool registered fails loudly
; rather than silently shipping an unsigned installer.
; SignTool=voxis        ; Authenticode imzasi icin: Inno Setup IDE > Tools > Configure Sign Tools
; SignedUninstaller=yes ; "voxis" adli sign tool kayitliyken ac
; ----------------------------------------------------------------------------

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; Default-off opt-in to wipe the stored Voxis session token (%APPDATA%\Voxis\.env)
; on uninstall so a handed-off machine does not retain a valid login. User config
; and transcripts are still preserved.
Name: "wipecreds"; Description: "{cm:WipeCredentialsTask}"; GroupDescription: "{cm:UninstallGroup}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Microsoft VC++ 2015-2022 x64 runtime, embedded for OFFLINE install. Why it's required
; even though _internal ships vcruntime/ucrtbase: python313.dll depends on the MS Visual
; C++ runtime; a Windows 10 that never received a recent VC++ runtime has that dependency
; unmet -> "Failed to load Python DLL ... LoadLibrary: <FormatMessageW failed.>" (seen in
; the field; installing this redist fixes it). The app-local ucrtbase under _internal does
; not help: on Win10 the UCRT is an OS component, so the SYSTEM ucrtbase is always used.
; A system-level install is the reliable fix. The redist supplies the VCRuntime
; (vcruntime140*.dll, 14.4x toolset) and the initial UCRT only where absent; it cannot
; upgrade an existing Win10 UCRT (that needs Windows Update). Embedding it (no install-time
; download) keeps the Store standalone/offline-installer rule met. Staged into
; installer\redist by build_official.py (fetched from aka.ms/vc14, not vendored).
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VCRedistNeeded

; User data (config, license token, transcripts) lives in %APPDATA%\Voxis and is
; intentionally preserved on uninstall so reinstalling keeps the user's settings.
; The stored session token (.env) is removed only when the user opts in via the
; "wipecreds" task above, so a handed-off machine does not keep a valid login.
[UninstallDelete]
Type: files; Name: "{userappdata}\Voxis\.env"; Tasks: wipecreds
; To wipe ALL user data on uninstall instead, uncomment:
; Type: filesandordirs; Name: "{userappdata}\Voxis"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[CustomMessages]
en.WipeCredentialsTask=Remove my saved Voxis login (sign-out) on uninstall
tr.WipeCredentialsTask=Kaldırırken kayıtlı Voxis oturumumu sil (çıkış yap)
de.WipeCredentialsTask=Beim Deinstallieren meine gespeicherte Voxis-Anmeldung entfernen (abmelden)
en.UninstallGroup=Uninstall options
tr.UninstallGroup=Kaldırma seçenekleri
de.UninstallGroup=Deinstallationsoptionen
en.InstallingRuntime=Installing the Microsoft Visual C++ runtime...
tr.InstallingRuntime=Microsoft Visual C++ çalışma zamanı kuruluyor...
de.InstallingRuntime=Microsoft Visual C++-Laufzeit wird installiert...

[Run]
; Install the VC++ runtime silently before the app launches. /quiet shows no UI (the
; Store "silent install" certification rule; a UAC prompt is allowed) and /norestart
; avoids forcing a reboot. Exit codes 0/1638/3010 are all success (3010 = reboot queued).
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; \
  StatusMsg: "{cm:InstallingRuntime}"; Check: VCRedistNeeded; Flags: waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; After a silent auto-update, relaunch the app (de-elevated) since the finish page is skipped.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: WizardSilent

[Code]
{ VC++ runtime gate. The x64 redist records its version under
  ...\VisualStudio\14.0\VC\Runtimes\x64. That key physically lives in the 32-bit
  (Wow6432Node) hive, so we probe both the 64- and 32-bit views. We require >= 14.40
  because Python 3.13 is built with the VS 2022 14.4x toolset; anything older (or
  absent) gets the bundled redist run over it (a no-op/in-place upgrade if current). }
function ReadVCRuntime(RootView: Integer; var Major, Minor: Cardinal): Boolean;
var
  Key: String;
  Installed: Cardinal;
begin
  Key := 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  Result := RegQueryDWordValue(RootView, Key, 'Installed', Installed) and (Installed = 1)
            and RegQueryDWordValue(RootView, Key, 'Major', Major)
            and RegQueryDWordValue(RootView, Key, 'Minor', Minor);
end;

function VCRedistNeeded: Boolean;
var
  Major, Minor: Cardinal;
begin
  if ReadVCRuntime(HKLM64, Major, Minor) or ReadVCRuntime(HKLM32, Major, Minor) then
    Result := not ((Major > 14) or ((Major = 14) and (Minor >= 40)))
  else
    Result := True;
end;
