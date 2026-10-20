; ============================================================================
;  CRM 客户管理系统 - Inno Setup 安装脚本（Windows 服务版）
;
;  编译（项目根目录执行）：
;      "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" deploy\installer.iss
;  产物：
;      deploy\output\CRM客户管理系统_安装程序_v2.0.0.exe
;
;  前置：先执行 build_exe.py --onedir --name "CRM客户管理系统"
;        （安装包直接引用 ..\dist\CRM客户管理系统\ 整个目录）
;
;  自启方案：pywin32 注册为 Windows 服务（服务名 CRMManager）
;   - 开机自启（SERVICE_AUTO_START）、无黑框后台运行
;   - 可在 services.msc 中启动 / 停止 / 查看日志
;   - 数据目录固定到 %ProgramData%\CRM客户管理系统（Program Files 不可写）
; ============================================================================

#define MyAppName "CRM 客户管理系统"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "PowerShell 中文社区"
#define MyAppURL "https://www.powershell.com.cn"
#define MyAppId "{{9E4B7C21-3D5A-4F88-A6B3-1C2D8E5F7A40}"
#define MyExeName "CRM客户管理系统.exe"
#define MyServiceName "CRMManager"
#define MyDataDir "{commonappdata}\CRM客户管理系统"
#define BuildDir "..\dist\CRM客户管理系统"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=CRM客户管理系统_安装程序_v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\build\app.ico
UninstallDisplayIcon={app}\{#MyExeName}
UninstallDisplayName={#MyAppName}
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "cn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
; 服务场景下不需要“重启计算机”这类提示，保持向导简洁
SetupAppRunningError=安装程序检测到「%1」正在运行。%n%n请先关闭它再继续安装，或点击「确定」强行继续（可能覆盖失败）。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（打开管理界面）"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Dirs]
; 数据目录放在 ProgramData：服务以 LocalSystem 运行，数据归系统账户，
; 但给普通用户读改权限，方便备份和手工拷贝数据库
Name: "{#MyDataDir}"; Permissions: users-modify
Name: "{#MyDataDir}\logs"; Permissions: users-modify
Name: "{#MyDataDir}\uploads"; Permissions: users-modify
Name: "{#MyDataDir}\avatars"; Permissions: users-modify

[INI]
; 安装版标记：EXE 启动时靠它决定数据目录与监听端口（见 installinfo.py）
Filename: "{app}\installed.ini"; Section: "CRM"; Key: "DataDir"; String: "{#MyDataDir}"; Flags: createkeyifdoesntexist uninsdeletesection
Filename: "{app}\installed.ini"; Section: "CRM"; Key: "Host"; String: "{code:GetHost}"
Filename: "{app}\installed.ini"; Section: "CRM"; Key: "Port"; String: "{code:GetPort}"

[Icons]
Name: "{group}\打开管理界面"; Filename: "{code:GetLocalUrl}"; Comment: "在浏览器中打开 {#MyAppName}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\数据目录"; Filename: "{#MyDataDir}"
Name: "{group}\服务管理（services.msc）"; Filename: "{sys}\services.msc"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{code:GetLocalUrl}"; Tasks: desktopicon

[Run]
; 1) 先按用户设定的口令建好管理员：放在服务启动之前，服务自举时看到账号已存在
;    就不会再随机生成口令，也就不会出现「向导里设的密码没生效」
Filename: "{app}\{#MyExeName}"; Parameters: "--reset-admin ""{code:GetAdminPassword}"""; Flags: runhidden waituntilterminated; StatusMsg: "正在初始化管理员账号..."; Check: ShouldResetAdmin
; 2) 注册服务（幂等：已存在则更新为开机自启）
Filename: "{app}\{#MyExeName}"; Parameters: "--install-service"; Flags: runhidden waituntilterminated; StatusMsg: "正在注册 Windows 服务..."
; 3) 启动服务
Filename: "{sys}\net.exe"; Parameters: "start {#MyServiceName}"; Flags: runhidden waituntilterminated; StatusMsg: "正在启动服务..."
; 4) 打开管理界面
Filename: "{code:GetLocalUrl}"; Description: "在浏览器中打开管理界面"; Flags: shellexec nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\net.exe"; Parameters: "stop {#MyServiceName}"; Flags: runhidden waituntilterminated; RunOnceId: "StopSvc"
Filename: "{app}\{#MyExeName}"; Parameters: "--remove-service"; Flags: runhidden waituntilterminated; RunOnceId: "DelSvc"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""{#MyExeName}"""; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; installed.ini 是 [INI] 段写出来的，卸载器不认为它是“安装的文件”，
; 只会清空内容而留下空文件，导致安装目录删不掉。这里显式删掉。
Type: files; Name: "{app}\installed.ini"
Type: filesandordirs; Name: "{app}\logs"
Type: dirifempty; Name: "{app}"
; 桌面那个 .url 偶尔会残留（URL 快捷方式的登记方式和 .lnk 不同），兜底删一次
Type: files; Name: "{autodesktop}\{#MyAppName}.url"

[Code]
var
  SvcPage: TInputQueryWizardPage;
  LanCheck: TNewCheckBox;
  PwdPage: TInputQueryWizardPage;
  LanAllowed: Boolean;
  ChosenPort: String;
  AdminPwd: String;

{ ---------- 供 [INI] / [Icons] / [Run] 调用的取值函数 ---------- }
function GetPort(Param: String): String;
begin
  Result := ChosenPort;
end;

function GetHost(Param: String): String;
begin
  if LanAllowed then
    Result := '0.0.0.0'
  else
    Result := '127.0.0.1';
end;

function GetLocalUrl(Param: String): String;
begin
  Result := 'http://127.0.0.1:' + ChosenPort + '/';
end;

function GetAdminPassword(Param: String): String;
begin
  Result := AdminPwd;
end;

{ 静默安装时没有向导页，AdminPwd 为空：此时不调用 --reset-admin，
  让程序自举生成随机口令并写入数据目录的「初始管理员密码.txt」，
  绝不能把管理员口令设成空串。 }
function ShouldResetAdmin(): Boolean;
begin
  { 口令非空就重置：交互安装由向导页赋值，静默安装由 /ADMINPWD 赋值。
    两者都为空时（纯静默、未给口令）跳过，交给程序自举生成随机口令并落盘。 }
  Result := AdminPwd <> '';
  Log('ShouldResetAdmin: silent=' + IntToStr(Ord(WizardSilent)) +
      ' pwdLen=' + IntToStr(Length(AdminPwd)) + ' result=' + IntToStr(Ord(Result)));
end;

{ ---------- 自定义向导页 ---------- }
procedure InitializeWizard();
var
  Lbl: TNewStaticText;
begin
  ChosenPort := '8081';
  AdminPwd := '';
  LanAllowed := True;

  { 第 1 页：端口 + 是否允许局域网访问 }
  SvcPage := CreateInputQueryPage(wpSelectDir,
    '服务设置', '设置访问端口与访问范围',
    '这些设置会写入安装目录的 installed.ini，安装完成后可在该文件中修改，改完重启服务生效。');
  SvcPage.Add('访问端口：', False);
  SvcPage.Values[0] := ChosenPort;

  LanCheck := TNewCheckBox.Create(SvcPage);
  LanCheck.Parent := SvcPage.Surface;
  LanCheck.Left := 0;
  LanCheck.Top := SvcPage.Edits[0].Top + SvcPage.Edits[0].Height + ScaleY(20);
  LanCheck.Width := SvcPage.SurfaceWidth;
  LanCheck.Height := ScaleY(17);
  LanCheck.Caption := '允许同一局域网内的其他电脑访问（勾选后监听 0.0.0.0，并自动放通防火墙端口）';
  LanCheck.Checked := True;

  Lbl := TNewStaticText.Create(SvcPage);
  Lbl.Parent := SvcPage.Surface;
  Lbl.Left := 0;
  Lbl.Top := LanCheck.Top + LanCheck.Height + ScaleY(10);
  Lbl.Width := SvcPage.SurfaceWidth;
  Lbl.WordWrap := True;
  Lbl.Caption := '提示：本系统使用 HTTP 明文传输，只建议在内网使用。' +
                 '如需从公网访问，请在前面加一层带 HTTPS 的反向代理（Nginx / IIS），不要直接映射端口。';

  { 第 2 页：管理员初始口令 }
  PwdPage := CreateInputQueryPage(SvcPage.ID,
    '管理员账号', '设置首次登录用的管理员密码',
    '账号固定为 admin。这里设置的口令只写入本机数据库，安装完成后可用「修改密码」再次更改。');
  PwdPage.Add('管理员密码：', True);
  PwdPage.Add('确认密码：', True);
  PwdPage.Values[0] := '';
  PwdPage.Values[1] := '';
end;

{ 口令强度：至少 8 位，且同时含字母与数字 }
function PasswordOk(const Pwd: String): Boolean;
var
  i: Integer;
  c: Char;
  HasAlpha, HasDigit: Boolean;
begin
  Result := False;
  if Length(Pwd) < 8 then
    Exit;
  HasAlpha := False;
  HasDigit := False;
  for i := 1 to Length(Pwd) do
  begin
    c := Pwd[i];
    if ((c >= 'a') and (c <= 'z')) or ((c >= 'A') and (c <= 'Z')) then
      HasAlpha := True
    else if (c >= '0') and (c <= '9') then
      HasDigit := True;
  end;
  Result := HasAlpha and HasDigit;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
begin
  Result := True;

  { 静默安装（/VERYSILENT）：向导页不显示，但 Inno 仍会逐页调用本函数做校验，
    所以这里必须直接给出取值并跳过校验，否则会弹出校验对话框把无人值守流程卡死。
    可用的命令行参数：
        /PORT=9000        访问端口，默认 8081
        /LAN=0            0 = 仅本机访问，1 = 允许局域网，默认 1
        /ADMINPWD=xxxx    管理员初始口令；不传则由程序自举生成随机口令并写入
                          数据目录的「初始管理员密码.txt」 }
  if WizardSilent then
  begin
    ChosenPort := ExpandConstant('{param:PORT|8081}');
    LanAllowed := ExpandConstant('{param:LAN|1}') <> '0';
    AdminPwd := ExpandConstant('{param:ADMINPWD|}');
    Port := StrToIntDef(ChosenPort, 0);
    if (Port < 1) or (Port > 65535) then
    begin
      Log('静默安装：端口参数无效，回落到 8081');
      ChosenPort := '8081';
    end;
    Log('静默安装参数：Port=' + ChosenPort + ' Lan=' + IntToStr(Ord(LanAllowed)) +
        ' 自定义口令=' + IntToStr(Ord(AdminPwd <> '')));
    Exit;
  end;

  if CurPageID = SvcPage.ID then
  begin
    ChosenPort := Trim(SvcPage.Values[0]);
    Port := StrToIntDef(ChosenPort, 0);
    if (Port < 1) or (Port > 65535) then
    begin
      MsgBox('端口必须是 1 - 65535 之间的整数。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    LanAllowed := LanCheck.Checked;
  end;

  if CurPageID = PwdPage.ID then
  begin
    AdminPwd := PwdPage.Values[0];
    if not PasswordOk(AdminPwd) then
    begin
      MsgBox('管理员密码至少 8 位，且必须同时包含字母和数字。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if AdminPwd <> PwdPage.Values[1] then
    begin
      MsgBox('两次输入的密码不一致，请重新输入。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    { 口令最终要塞进命令行参数，双引号会截断参数，直接拒绝 }
    if Pos('"', AdminPwd) > 0 then
    begin
      MsgBox('密码不能包含英文双引号。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

{ ---------- 防火墙：仅在允许局域网访问时放通端口 ---------- }
procedure OpenFirewall(const Port: String);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\netsh.exe'),
       'advfirewall firewall delete rule name="CRM 客户管理系统 (TCP ' + Port + ')"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\netsh.exe'),
       'advfirewall firewall add rule name="CRM 客户管理系统 (TCP ' + Port + ')" ' +
       'dir=in action=allow protocol=TCP localport=' + Port,
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CloseFirewall(const Port: String);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\netsh.exe'),
       'advfirewall firewall delete rule name="CRM 客户管理系统 (TCP ' + Port + ')"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ ---------- 安装前：检测旧版本，覆盖升级时先停服务 ---------- }
function InitializeSetup(): Boolean;
var
  UninstStr: String;
  ResultCode: Integer;
begin
  Result := True;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppId}_is1',
      'UninstallString', UninstStr) then
  begin
    { 静默安装（/VERYSILENT，企业批量部署常用）不能弹框，否则会一直卡住等人点 }
    if not WizardSilent then
    begin
      if MsgBox('检测到「{#MyAppName}」已安装。' + #13#10 + #13#10 +
                '是否覆盖升级？' + #13#10 +
                '· 选「是」：保留原有数据，替换程序文件' + #13#10 +
                '· 选「否」：取消本次安装',
                mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end
    else
      Log('检测到已安装版本，静默模式下按覆盖升级处理');

    { 覆盖升级：先停服务并结束进程，否则 exe 被占用导致文件替换失败 }
    Exec(ExpandConstant('{sys}\net.exe'), 'stop {#MyServiceName}', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#MyExeName}"', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Sleep(1200);
  end;
end;

{ ---------- 安装后：放通防火墙 + 校验服务是否真的起来了 ---------- }
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if LanAllowed then
      OpenFirewall(ChosenPort);

    if Exec(ExpandConstant('{sys}\sc.exe'), 'query {#MyServiceName}', '', SW_HIDE,
            ewWaitUntilTerminated, ResultCode) then
    begin
      if ResultCode <> 0 then
      begin
        { 静默安装下不能弹框（会卡住无人值守的装机流程），改写入安装日志 }
        if WizardSilent then
          Log('警告：Windows 服务未能注册成功，请检查管理员权限与 '
              + ExpandConstant('{#MyDataDir}') + '\logs\service.log')
        else
          MsgBox('警告：Windows 服务未能注册成功。' + #13#10 + #13#10 +
                 '请打开 services.msc 确认「{#MyAppName}」是否存在；' + #13#10 +
                 '排查日志：' + ExpandConstant('{#MyDataDir}') + '\logs\service.log',
                 mbError, MB_OK);
      end;
    end;
  end;
end;

{ ---------- 卸载：先停服务、删服务、关防火墙，再删文件 ---------- }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Port: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Port := GetIniString('CRM', 'Port', '8081', ExpandConstant('{app}\installed.ini'));

    Exec(ExpandConstant('{sys}\net.exe'), 'stop {#MyServiceName}', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{app}\{#MyExeName}'), '--remove-service', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#MyExeName}"', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    CloseFirewall(Port);
    Sleep(800);
  end;
end;
