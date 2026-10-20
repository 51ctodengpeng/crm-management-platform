# -*- coding: utf-8 -*-
"""Windows 服务包装（pywin32）

把 CRM 的 Web 服务注册为真正的 Windows 服务，而不是留一个黑框控制台：

- 开机自启（SERVICE_AUTO_START），无需登录桌面即可运行
- 无窗口后台运行，可在 services.msc 里启动 / 停止 / 查看状态
- 崩溃后由 SCM 按失败策略处理，日志落到数据目录 logs\\service.log

命令行（由 desktop.py 转发）：
  --install-service   注册服务（需管理员权限）
  --remove-service    删除服务（需管理员权限）
  --run-service       以服务身份运行（由 SCM 调用，不要手工执行）
"""
import os
import sys
import threading
import time

import pywintypes
import servicemanager
import win32event
import win32service
import win32serviceutil

SERVICE_NAME = "CRMManager"
DISPLAY_NAME = "CRM 客户管理系统"
DESCRIPTION = "CRM 客户管理系统 Web 服务（客户台账 / 项目 / 合同 / 回款 / 附件）"


def _data_dir() -> str:
    """服务运行时的数据目录（与安装版保持一致，优先环境变量）"""
    d = os.environ.get("CRM_DATA_DIR")
    if d:
        return d
    progdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return os.path.join(progdata, "CRM客户管理系统")


def _service_log(msg: str) -> None:
    """服务安装/删除日志，便于交付后排查（写数据目录 logs\\service.log）"""
    try:
        d = os.path.join(_data_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "service.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


class CRMService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    # ---- SCM 回调 ----
    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        try:
            self._run()
        except Exception as exc:                       # 启动失败要进事件日志，否则只能看“服务已停止”
            import traceback
            servicemanager.LogErrorMsg("CRM 服务启动失败：%s" % exc)
            _service_log("启动失败：%s\n%s" % (exc, traceback.format_exc()))
            raise

    def _run(self):
        # 兜底：正常情况下 desktop.py 已在导入 config 之前套用了安装版配置
        from installinfo import prepare_env_from_install
        prepare_env_from_install()

        from config import Config           # noqa: E402  (必须在环境变量就绪后导入)
        from waitress.server import create_server
        from app import app                 # noqa: E402

        host = os.environ.get("CRM_HOST") or "0.0.0.0"
        port = int(os.environ.get("CRM_PORT") or 8081)

        Config.ensure_dirs()
        os.chdir(str(Config.DATA_DIR))

        # 用可关闭的 server 对象（而不是阻塞的 serve()），配合停止事件做优雅停机
        self.server = create_server(app, host=host, port=port, threads=8, ident="CRM-Service")
        _service_log("服务已启动：http://%s:%d  数据目录 %s" % (host, port, Config.DATA_DIR))
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)

        t = threading.Thread(target=self.server.run, daemon=True)
        t.start()

        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

        _service_log("收到停止信号，正在关闭")
        try:
            self.server.close()
        except Exception:
            pass
        t.join(timeout=8)
        if t.is_alive():
            # 还有长连接没释放时直接结束进程（SQLite WAL 下强杀不会损坏数据）
            os._exit(0)


def _scm():
    return win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)


def install_service() -> int:
    exe = os.path.abspath(sys.executable)
    binpath = '"%s" --run-service' % exe
    try:
        scm = _scm()
    except Exception as e:
        _service_log("打开服务控制管理器失败（需管理员权限）：%s" % e)
        raise
    try:
        try:
            hs = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            win32service.ChangeServiceConfig(
                hs, win32service.SERVICE_NO_CHANGE, win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL, binpath, None, 0, None, None, None,
                DISPLAY_NAME)
            _service_log("服务已存在，配置已更新为开机自启（binpath=%s）" % binpath)
        except pywintypes.error as e:
            if getattr(e, "winerror", None) != 1060:      # 1060 = 服务不存在，首次安装的正常路径
                raise
            # 注意参数顺序：loadOrderGroup, tagId, dependencies, userName, password
            # tagId 必须是整数 0（传 None 会 TypeError）
            hs = win32service.CreateService(
                scm, SERVICE_NAME, DISPLAY_NAME, win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS, win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL, binpath,
                None, 0, None, None, None)
            _service_log("服务已创建并设为开机自启（binpath=%s）" % binpath)
        try:
            win32service.ChangeServiceConfig2(hs, win32service.SERVICE_CONFIG_DESCRIPTION, DESCRIPTION)
        except Exception:
            pass
        win32service.CloseServiceHandle(hs)
    finally:
        win32service.CloseServiceHandle(scm)
    return 0


def remove_service() -> int:
    try:
        scm = _scm()
    except Exception as e:
        _service_log("打开服务控制管理器失败（需管理员权限）：%s" % e)
        raise
    try:
        try:
            hs = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            win32service.DeleteService(hs)
            win32service.CloseServiceHandle(hs)
            _service_log("服务已删除")
        except pywintypes.error as e:
            if getattr(e, "winerror", None) == 1060:
                _service_log("服务不存在，无需删除")
            else:
                raise
    finally:
        win32service.CloseServiceHandle(scm)
    return 0


def service_exists() -> bool:
    try:
        scm = _scm()
    except Exception:
        return False
    try:
        hs = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        win32service.CloseServiceHandle(hs)
        return True
    except pywintypes.error:
        return False
    finally:
        win32service.CloseServiceHandle(scm)


def run_service():
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(CRMService)
    servicemanager.StartServiceCtrlDispatcher()
