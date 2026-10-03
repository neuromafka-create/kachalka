; Inno Setup script — «Качалка»
; 1) Сначала: build_exe.bat  →  dist\Kachalka\
; 2) Открой этот файл в Inno Setup Compiler → Build
; 3) Готовый Setup: dist\Kachalka-Setup.exe
;
; Скачать Inno Setup: https://jrsoftware.org/isinfo.php

#define MyAppName "Качалка"
#define MyAppNameEn "Kachalka"
#define MyAppVersion "1.3.1"
#define MyAppPublisher "NeyroVibe"
#define MyAppURL "https://t.me/tekhnocafe"
#define MyAppExeName "Kachalka.exe"

[Setup]
AppId={{A7C3E9D1-4B2F-4E8A-9C1D-6F0A2B8E5D73}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppNameEn}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Kachalka-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
; Тот же AppId: повторный запуск Setup обновляет копию в %LocalAppData%\Kachalka.
; Папку «Скачанное» установщик не удаляет — её нет в списке файлов.
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
CloseApplications=yes
CloseApplicationsFilter=Kachalka.exe
; Запуск после установки — только галочка в мастере, без второго окна.
RestartApplications=no
VersionInfoVersion=1.3.1.0
VersionInfoProductVersion=1.3.1.0

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
; onedir-сборка PyInstaller
Source: "dist\Kachalka\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Отдельный .ico для ярлыков (на случай кэша Windows)
Source: "assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; IconIndex: 0
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; IconIndex: 0; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; папку загрузок пользователя не трогаем намеренно
Type: filesandordirs; Name: "{app}\__pycache__"
