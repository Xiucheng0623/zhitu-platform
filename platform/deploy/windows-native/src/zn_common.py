#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 · 原生 Windows 安装程序底层库（不碰 Docker / WSL）。

这个模块和 `windows-client/src/zhitu_common.py` 是**两套东西**，不要混：
  * zhitu_common  —— 容器路线：调 wsl.exe、docker compose、docker load；
  * zn_common     —— 原生路线：调 psql / pip / schtasks / caddy，全是 Windows 进程。

原生路线为什么存在
----------------------------------------------------------------------------
容器路线的镜像是 Linux 的，Windows 上要么开 WSL 2（要求嵌套虚拟化，云主机
常没有），要么用 Docker Desktop（Windows Server 不支持）。所以"服务器就是一台
Windows"时，最稳的做法是每个组件都用原生 Windows 版：
    PostgreSQL for Windows   → 数据库
    Python venv + pip        → 编排层（本项目自己的代码）
    Python venv + pip        → Label Studio（官方 pip 包，Django 应用）
    caddy.exe for Windows    → 入口反代 + 静态托管
    schtasks                 → 开机自启与崩溃拉起
全程没有 Linux、没有 WSL、没有容器。

几个刻意的取舍
----------------------------------------------------------------------------
1. **PostgreSQL 用 binaries zip 而不是 EDB 安装器**：安装器要交互、要超级用户
   密码、捆绑 pgAdmin；zip 解压 + initdb + pg_ctl register 全程可脚本化且确定。
2. **计划任务而不是 Windows 服务**：`sc.exe` 配 python.exe 会因 30 秒内没调
   StartServiceCtrlDispatcher 被 SCM 以 1053 杀掉，绕开它就得引入 NSSM/pywin32
   这类外部二进制。计划任务 + AtStartup + SYSTEM + 1 分钟重复触发，等价于开机
   服务，且零第三方依赖。
3. **PowerShell 一律走 -EncodedCommand**：把脚本按 UTF-16LE base64 塞进去，
   彻底绕开「无 BOM 的 .ps1 被按 GBK 解码导致语法失败」和中文乱码，这一条在
   本机踩过不止一次。
"""
from __future__ import annotations

import atexit
import base64
import ctypes
import datetime
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

APP_NAME = "智途"
APP_ID = "Zhitu"
DEFAULT_APP_DIR = r"C:\Zhitu\app"        # 代码
DEFAULT_DATA_DIR = r"C:\Zhitu\data"      # 配置 / 日志 / 数据库 / LS 数据
DEFAULT_PG_PORT = 5432
DEFAULT_API_PORT = 8300
DEFAULT_LS_PORT = 8100
DEFAULT_HTTP_PORT = 80

TASKS = {
    "pg": "ZhituPostgreSQL",
    "orch": "ZhituOrchestrator",
    "ls": "ZhituLabelStudio",
    "caddy": "ZhituCaddy",
    "backup": "ZhituBackupDaily",
}

# 国内可达的下载源和镜像（服务器在国内）
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PG_ZIP_URL = ("https://get.enterprisedb.com/postgresql/"
              "postgresql-17.2-1-windows-x64-binaries.zip")
# 备用源：Maven Central 上的 Windows 版 PostgreSQL 17.2 二进制（io.zonky.test 发布，
# 给嵌入式测试用的）。只有 23 MB，国内可达性好得多，但**不含 psql / pg_dump**
# 这些客户端工具 —— 所以只作为兜底，SQL 改由 venv 里的 psycopg 执行。
PG_ALT_URL = ("https://repo1.maven.org/maven2/io/zonky/test/postgres/"
              "embedded-postgres-binaries-windows-amd64/17.2.0/"
              "embedded-postgres-binaries-windows-amd64-17.2.0.jar")
CADDY_ZIP_URL = ("https://ghproxy.net/https://github.com/caddyserver/caddy/"
                 "releases/download/v2.8.4/caddy_2.8.4_windows_amd64.zip")
PY_DOWNLOAD_HINT = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
# 同一个地址，安装程序会自己下下来静默装 —— 服务器上 winget 经常根本没有
# （Server 2016/2019 默认没装 App Installer），只给一行人话让运维自己装，
# 等于把「能不能装」推给现场。能自动做的就别让人做。
PY_INSTALLER_URL = PY_DOWNLOAD_HINT

# 刻意**不**把 WorkBuddy / 其它工具自带的 Python 当候选：
# 那种解释器的路径跟着宿主软件版本走（.../versions/3.13.12/python.exe），
# 宿主一升级，venv 里的 pyvenv.cfg 就指向空路径，整个平台的依赖全废。
# 生产环境的 venv 必须建在系统级 Python 上。


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def app_dir() -> Path:
    """EXE 所在目录（PyInstaller onefile 下是 exe 的目录，不是 _MEI）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def payload_dir() -> Path:
    """载荷目录（前端 / 编排层源码 / 迁移 / 品牌补丁 / Caddy 模板）。

    三种形态都要支持：
      * 单文件 exe：PyInstaller 把 `--add-data` 的东西解到 sys._MEIPASS，
        交付时**只需要一个 exe**，不用同时给一个 payload 目录；
      * 目录形态（开发期 / 老交付物）：exe 同层的 payload/；
      * 纯源码安装（install_from_source.py）：载荷由 make_payload.py 现场生成，
        位置在仓库的 windows-native/payload，不在 src/ 下面 —— 用环境变量
        ZHITU_PAYLOAD_DIR 指过去，避免把 1 MB 源码树复制到 src/ 里污染工作树。
    """
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        p = Path(meipass) / "payload"
        if p.is_dir():
            return p
    override = os.environ.get("ZHITU_PAYLOAD_DIR")
    if override:
        return Path(override)
    return app_dir() / "payload"


def cache_dirs() -> Tuple[Path, ...]:
    """预置大安装包（PostgreSQL / Caddy zip）可能被放的位置，按优先级。

    单文件 exe 内嵌的 payload 是只读的，客户没法往里塞 330 MB 的包，所以
    「放 exe 旁边」也必须能认（用正斜杠写字面量，免得 docstring 里的
    反斜杠触发 SyntaxWarning）：
        <exe 旁>/cache/            ← 离线部署时最顺手的位置
        <exe 旁>/payload/cache/    ← 目录形态的老位置
        <载荷内>/cache/            ← 打包时就塞进去的
    """
    here = app_dir()
    return (here / "cache", here / "payload" / "cache", payload_dir() / "cache")


def config_path() -> Path:
    """安装期状态文件。ZHITU_CONFIG_DIR 供自动化校验隔离使用。"""
    override = os.environ.get("ZHITU_CONFIG_DIR")
    if override:
        return Path(override) / "native.json"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID / "native.json"


def load_config() -> Dict[str, object]:
    p = config_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: Dict[str, object]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def logs_dir() -> Path:
    """日志目录。装完之后在 <data>\\logs；装之前落在 %LOCALAPPDATA%。

    装之前不能用 C:\\Zhitu\\data —— 那时还没有管理员权限，创建目录会静默失败，
    日志就等于没写。
    """
    data = load_config().get("data_dir")
    if data:
        d = Path(str(data)) / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            pass
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / APP_ID / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 控制台与进程
# ---------------------------------------------------------------------------
def fix_console() -> None:
    """中文控制台三通道不乱码：流按 UTF-8 + 代码页 65001。

    不做这一步，`--selftest` 重定向到管道时输出是「ÖÇÍ¾」这种乱码，
    运维会以为是程序坏了。
    """
    for name in ("stdout", "stderr"):
        st = getattr(sys, name, None)
        try:
            if st is not None and hasattr(st, "reconfigure"):
                st.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass


def cli_sink(mode: str) -> Callable[..., None]:
    """同时写控制台与文件。--noconsole 的 exe 里 print 是无处可看的。"""
    logs_dir().mkdir(parents=True, exist_ok=True)
    path = logs_dir() / ("cli-%s-%s.log" % (mode, time.strftime("%Y%m%d")))
    fh = open(path, "a", encoding="utf-8", buffering=1)

    def emit(*args: object) -> None:
        text = " ".join(str(a) for a in args)
        try:
            print(text, flush=True)
        except (OSError, ValueError):
            pass
        try:
            fh.write(text + "\n")
        except (OSError, ValueError):
            pass

    emit.path = str(path)  # type: ignore[attr-defined]
    return emit


def tee_sink(mode: str, screen: Optional[Callable[..., None]] = None
             ) -> Callable[..., None]:
    """GUI 日志：同时写窗口控件和文件。

    图形界面之前只往 Text 控件里塞 —— 窗口一关就什么都不剩。真机出故障时
    用户只能截图，长报错被截断就再也查不到了（第 03 步 initdb 那次就是这样：
    initdb 的完整输出比弹窗能显示的多得多，最关键的几行恰好在截断之外）。
    文件名带秒级时间戳，装几次就有几份，互不覆盖。
    """
    logs_dir().mkdir(parents=True, exist_ok=True)
    path = logs_dir() / ("install-%s-%s.log"
                         % (mode, time.strftime("%Y%m%d-%H%M%S")))
    fh = open(path, "a", encoding="utf-8", buffering=1)
    # 进程退出时关掉；否则装完想立刻打开/搬走日志会被句柄占着。
    atexit.register(fh.close)

    def emit(*args: object) -> None:
        text = " ".join(str(a) for a in args)
        if screen is not None:
            try:
                screen(text)
            except Exception:  # noqa: BLE001 - 控件已销毁时不能连累安装
                pass
        try:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))
        except (OSError, ValueError):
            pass

    emit.path = str(path)  # type: ignore[attr-defined]
    emit.close = fh.close  # type: ignore[attr-defined]
    return emit


SPECIAL_RC = {"missing": 9009, "denied": 9008, "timeout": 9998}

def run(cmd: List[str], timeout: int = 60, cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None
        ) -> Tuple[int, str]:
    """跑外部命令，永远返回 (rc, 合并输出)。

    三个特殊返回码不能和"跑了但失败"混为一谈，混了就会给出完全错误的建议：
      9009  命令不存在        → 该装的东西没装
      9008  存在但起不来      → 权限/安全策略拦了（本机 wsl.exe 就是这种）
      9998  超时被我们杀掉    → 网络或卡死
    """
    exe = cmd[0]
    resolved = shutil.which(exe) or exe
    try:
        p = subprocess.run(
            [resolved] + cmd[1:], capture_output=True, text=False,
            timeout=timeout, cwd=cwd, env={**os.environ, **(env or {})},
            input=(input_text.encode("utf-8") if input_text else None))
    except FileNotFoundError:
        return SPECIAL_RC["missing"], "找不到命令：%s" % exe
    except PermissionError as exc:
        return SPECIAL_RC["denied"], "无法执行 %s：%s" % (exe, exc)
    except OSError as exc:
        # WinError 5 之类会以 OSError 形式抛，不是 PermissionError
        num = getattr(exc, "winerror", None)
        if num in (5, 740):
            return SPECIAL_RC["denied"], ("无法执行 %s：%s（需要管理员，"
                                          "或被安全策略/组策略禁止）" % (exe, exc))
        return 1, "执行 %s 失败：%s" % (exe, exc)
    except subprocess.TimeoutExpired:
        return SPECIAL_RC["timeout"], "%s 超时（%ds）已终止" % (exe, timeout)
    raw = (p.stdout or b"") + (b"\n" if p.stdout and p.stderr else b"") + (p.stderr or b"")
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return p.returncode, raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return p.returncode, raw.decode("utf-8", errors="replace").strip()


def run_daemon(cmd: List[str], timeout: int = 180) -> Tuple[int, str]:
    """启动"会一直活着"的进程（pg_ctl start / caddy run）。

    为什么不能用 run()：Windows 上 pg_ctl 起的 postgres 会继承父进程的
    stdout/stderr 句柄，pg_ctl 退出后那两个管道**仍被 postgres 持有**，
    subprocess.run(capture_output=True) 会一直等 EOF —— 表现就是"卡住不动"，
    而且超时杀掉 pg_ctl 也没用，管道还在 postgres 手里。
    这里改成 DEVNULL，pg_ctl 一退就返回，输出只去它自己的日志文件。
    """
    exe = cmd[0]
    resolved = shutil.which(exe) or exe
    try:
        p = subprocess.Popen([resolved] + cmd[1:],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             env={**os.environ})
    except FileNotFoundError:
        return SPECIAL_RC["missing"], "找不到命令：%s" % exe
    except OSError as exc:
        num = getattr(exc, "winerror", None)
        if num in (5, 740):
            return SPECIAL_RC["denied"], "无法执行 %s：%s（需要管理员）" % (exe, exc)
        return 1, "执行 %s 失败：%s" % (exe, exc)
    try:
        return p.wait(timeout=timeout), ""
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except OSError:
            pass
        return SPECIAL_RC["timeout"], "%s 超时（%ds）已终止" % (exe, timeout)


_XESC = (("_x000D_", "\r"), ("_x000A_", "\n"), ("_x0009_", "\t"))
_XENT = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
         ("&apos;", "'"), ("&amp;", "&"))


def unwrap_clixml(text: str) -> str:
    """把 PowerShell 的 CLIXML 错误流还原成人能读的文本。

    ps() 的 stderr 合并进输出后长这样：
        #< CLIXML
        <Objs Version="1.1.0.1" ...><S S="Error">Register-ScheduledTask : 拒绝访问。_x000D__x000A_</S>
    换行被转义成 `_x000D__x000A_`，中文和路径挤成一坨，真正的错误原因看不见。
    这里抽出所有 <S S="Error"> 段并反转义，其它 CLIXML 噪声直接丢掉。
    不是 CLIXML 就原样返回。
    """
    if not text or ("<Objs" not in text and "#< CLIXML" not in text):
        return text
    chunks = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
    if not chunks:
        chunks = re.findall(r"<S>(.*?)</S>", text, re.S)
    out = "".join(chunks)
    for k, v in _XESC:
        out = out.replace(k, v)
    for k, v in _XENT:
        out = out.replace(k, v)
    return out.strip() or text.strip()


def ps(script: str, timeout: int = 120) -> Tuple[int, str]:
    """跑 PowerShell 脚本片段。

    走 -EncodedCommand 而不是 -File / -Command：脚本按 UTF-16LE base64 传进去，
    完全不经过命令行编码与 .ps1 文件编码这两道坎。写中文注释的 .ps1 在
    PowerShell 5.1 下被按 GBK 解码而语法失败，这个坑踩过不止一次。

    ★ 脚本一律包 try/catch：不包的话，异常以 terminating error 落到 stderr，
    而 PowerShell 的 stderr 是 CLIXML（换行转义成 `_x000D__x000A_`），
    run() 又把 stderr 合并进输出 —— 于是报错变成一坨乱码，「拒绝访问」
    这四个字都被吃掉。真机注册计划任务失败时就是这样，用户只能发一张
    谁都看不懂的截图。包起来之后错误变成一行干净的中文。
    """
    body = ("$ProgressPreference='SilentlyContinue';"
            "try { " + script + " } catch {"
            " Write-Output ('__PS_ERROR__' + $_.Exception.Message);"
            " exit 1 }")
    b64 = base64.b64encode(
        # $ProgressPreference 必须关：PowerShell 会往 stderr 写 CLIXML 进度记录，
        # 和 stdout 混在一起后输出变成一坨 <Objs ...>，解析状态全错。
        body.encode("utf-16-le")
    ).decode("ascii")
    rc, out = run(["powershell.exe", "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", "-EncodedCommand", b64],
                  timeout=timeout)
    out = unwrap_clixml(out or "")
    lines = []
    for ln in out.splitlines():
        if ln.startswith("__PS_ERROR__"):
            lines.append(ln[len("__PS_ERROR__"):].strip())
        elif ln.startswith("__PS_TYPE__"):
            continue
        else:
            lines.append(ln)
    return rc, "\n".join(lines).strip()


def setup_dpi() -> float:
    """开 DPI 感知并返回缩放比（1.0 / 1.25 / 1.5 …）。

    不开的话，150% 缩放时整个窗口会被系统当位图拉伸：字发虚、控件互相压，
    而且 winfo 报的逻辑尺寸和屏幕上的物理尺寸对不上，布局怎么调都不对。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            return 1.0
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def elevate_self() -> bool:
    """以管理员身份重启自己。成功返回 True（此时应立刻退出当前进程）。"""
    try:
        params = " ".join('"%s"' % a for a in sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(app_dir()), 1)
        return rc > 32
    except Exception:  # noqa: BLE001
        return False


def total_ram_gb() -> float:
    """物理内存总量（GB）。拿不到返回 0，调用方按"不判"处理。

    为什么不用 wmic：Windows 11 25H2 起 wmic 已被彻底移除（实测
    C:\\Windows\\System32\\wbem\\WMIC.exe 不存在），拿它做判断会在新机器上
    静默失效。GlobalMemoryStatusEx 是 kernel32 的稳定接口，没有这个风险。
    """
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return 0.0
        return round(st.ullTotalPhys / (1024.0 ** 3), 1)
    except Exception:  # noqa: BLE001
        return 0.0


def windows_build() -> Tuple[int, str]:
    try:
        v = sys.getwindowsversion()
        name = "Windows %d.%d" % (v.major, v.minor)
        return int(v.build), name
    except Exception:  # noqa: BLE001
        return 0, "未知"


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------
def probe_http(url: str, timeout: float = 4.0, bypass_proxy: bool = True) -> Dict[str, object]:
    """探一个 HTTP 端点。默认绕开系统代理。

    本机有 HTTP_PROXY 时，发往 127.0.0.1 的请求会被代理接走并返回 502，
    和真实状态毫无关系 —— 排查时最容易误判"服务挂了"。
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({} if bypass_proxy else None))
    t0 = time.time()
    try:
        with opener.open(url, timeout=timeout) as r:
            r.read(512)
            return {"ok": 200 <= r.status < 400, "code": r.status,
                    "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "code": exc.code, "ms": int((time.time() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": type(exc).__name__,
                "ms": int((time.time() - t0) * 1000)}


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.2) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def local_ip() -> str:
    """本机局域网地址（员工浏览器要用的那个）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.2)
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
            if ip and not ip.startswith(("127.", "169.254.")):
                return ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")):
                return ip
    except OSError:
        pass
    return ""


def download(url: str, dest: Path, log: Callable[[str], None],
             min_bytes: int = 1 << 20, timeout: float = 60.0) -> Tuple[bool, str]:
    """下载到 dest。有 .part 断点续传，失败不留下半个文件冒充成功。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "zhitu-installer/1.0"})
    if have:
        req.add_header("Range", "bytes=%d-" % have)
        log("  续传：已有 %.1f MB" % (have / 1048576))
    t0 = time.time()
    last = 0.0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0) + have
            mode = "ab" if have and r.status == 206 else "wb"
            if mode == "wb":
                have = 0
            with open(tmp, mode) as fh:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    fh.write(chunk)
                    have += len(chunk)
                    now = time.time()
                    if now - last > 2:
                        last = now
                        pct = (" %.0f%%" % (have * 100.0 / total)) if total else ""
                        log("  已下载 %.1f MB%s" % (have / 1048576, pct))
        if have < min_bytes:
            return False, "下载内容只有 %d 字节，明显不完整" % have
        os.replace(tmp, dest)
        return True, "%.1f MB，用时 %.0f 秒" % (have / 1048576, time.time() - t0)
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s（%s）" % (exc.code, url)
    except Exception as exc:  # noqa: BLE001
        return False, "下载失败：%s" % exc


# ---------------------------------------------------------------------------
# Python / venv / pip
# ---------------------------------------------------------------------------
def find_python(lo: Tuple[int, int] = (3, 11), hi: Tuple[int, int] = (3, 13)
                ) -> Tuple[str, str]:
    """找一个在 [lo, hi] 区间内的 Python。返回 (路径, 版本)。

    为什么要限区间：Label Studio 1.23 的依赖（numpy/pandas 等）在新版本 Python
    上经常没有预编译 wheel，pip 会退化成从源码编译并失败。本机装了 3.15，
    直接用它会装不上 LS —— 所以不能"挑最新的"，要挑**兼容的**。

    优先 py 启动器（能挑版本），其次 PATH，最后扫安装目录（服务器上装完
    Python 没勾 "Add to PATH" 极常见）。
    """
    cands: List[str] = []
    for ver in ("-3.12", "-3.11", "-3.13", "-3"):
        rc, out = run(["py", ver, "-c", "import sys;print(sys.executable)"], timeout=30)
        if rc == 0 and out.strip():
            cands.append(out.strip().splitlines()[-1].strip())
    rc, out = run(["python", "-c", "import sys;print(sys.executable)"], timeout=30)
    if rc == 0 and out.strip():
        cands.append(out.strip().splitlines()[-1].strip())
    for pat in ("Programs/Python/Python3*/python.exe", "Python3*/python.exe"):
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""),
                     "C:\\"):
            if not base:
                continue
            cands += [str(p) for p in sorted(Path(base).glob(pat), reverse=True)]

    seen = set()
    best: Tuple[str, str] = ("", "")
    best_rank = 99
    for c in cands:
        if c in seen or not Path(c).is_file():
            continue
        seen.add(c)
        rc, out = run([c, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                      timeout=30)
        if rc != 0:
            continue
        ver = (out.strip().splitlines() or [""])[-1].strip()
        try:
            major, minor = (int(x) for x in ver.split("."))
        except ValueError:
            continue
        if (major, minor) < lo or (major, minor) > hi:
            continue
        # 区间内优先 3.12 → 3.11 → 3.13（LS 1.23 在 3.12 上验证最多）
        rank = {12: 0, 11: 1, 13: 2}.get(minor, 3)
        if not best[0] or rank < best_rank:
            best, best_rank = (c, ver), rank
    return best


def python_install_official(log: Callable[..., None]) -> Tuple[bool, str]:
    """下官方安装包静默装一个 Python 3.12（装完返回是否找到可用的）。

    为什么不是只有 winget：Windows Server 2016/2019 默认没有 App Installer，
    `winget.exe` 直接不存在；云主机的精简镜像也常常被裁掉。原来那一步的判断是
    「winget 装不上 → 让用户自己去下」，等于把能不能装推给现场。
    官方 exe 支持 `/quiet InstallAllUsers=1 PrependPath=1`，一条命令搞定，
    比 winget 更快也更可预期（25 MB，且不需要任何商店组件）。
    """
    dst = Path(tempfile.gettempdir()) / "python-3.12.8-amd64.exe"
    log("下载 Python 3.12 官方安装包（约 25 MB）…")
    ok, msg = download(PY_INSTALLER_URL, dst, log, min_bytes=20 * 1048576,
                       timeout=120.0)
    if not ok:
        return False, "下载 Python 安装包失败：%s" % msg
    log("静默安装（InstallAllUsers=1 PrependPath=1，约 1-2 分钟）…")
    rc, out = run([str(dst), "/quiet", "InstallAllUsers=1", "PrependPath=1",
                   "Include_test=0", "Include_launcher=1", "Include_pip=1"],
                  timeout=900)
    # Python 的安装程序会先起一个子进程再退出，rc=0 时也可能还在装，
    # 所以不能装完就立刻 find_python —— 要给它一点时间落地。
    for i in range(20):
        py, ver = find_python((3, 11), (3, 13))
        if py:
            return True, "已静默安装 Python %s（%s）" % (ver, py)
        time.sleep(3)
    return False, ("静默安装没成功（rc=%s）：%s"
                   % (rc, (out or "").strip()[-300:] or "安装包没有给出输出"))


def venv_python(venv_dir: Path) -> Path:
    """venv 里的 python.exe，两种形态都要认。

      * 标准 venv   -> <venv>/Scripts/python.exe
      * 便携运行时  -> <venv>/python.exe（离线包把 Python embeddable 铺开，
        python.exe 与 python313.dll 必须同层才互相找得到，塞进 Scripts 就废了）
    """
    cand = venv_dir / "Scripts" / "python.exe"
    if cand.is_file():
        return cand
    root = venv_dir / "python.exe"
    if root.is_file():
        return root
    return cand


def venv_create(py: str, venv_dir: Path) -> Tuple[bool, str]:
    if venv_python(venv_dir).is_file():
        return True, "已存在，复用"
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    rc, out = run([py, "-m", "venv", str(venv_dir)], timeout=600)
    if rc != 0:
        return False, "建 venv 失败：%s" % (out[-400:] or rc)
    return True, "已创建 %s" % venv_dir


def pip_install(venv_dir: Path, args: List[str], log: Callable[[str], None],
                timeout: int = 3600, attempts: int = 3) -> Tuple[bool, str]:
    """在指定 venv 里装包，走国内镜像，失败自动重试。

    为什么必须重试：Windows 上 pip 装大依赖树时，最后一步把 `*.dist-info\\RECORD`
    改名经常撞上 `WinError 5 拒绝访问`（杀软正在扫那个文件 / 句柄没释放）。实测
    Label Studio 装到 django 那一步就撞过。**这不是包坏了**，隔几秒重跑一次
    就过去了 —— 一次不重试就把整台服务器判成"装不上"，代价太大。

    也不用 `pip install -q`：装 LS 要十几分钟，没有输出等于卡死也看不出来。
    """
    py = venv_python(venv_dir)
    if not py.is_file():
        return False, "venv 里没有 python.exe：%s" % venv_dir
    cmd = [str(py), "-m", "pip", "install", "--no-input",
           "--disable-pip-version-check", "-i", PIP_INDEX] + args
    last = ""
    for i in range(max(1, attempts)):
        if i:
            log("  pip 失败，6 秒后重试（第 %d/%d 次）…" % (i + 1, attempts))
            time.sleep(6)
        rc, out = run(cmd, timeout=timeout)
        if rc == 0:
            tail = "\n".join((out or "").strip().splitlines()[-3:])
            return True, tail or "已安装"
        last = out or str(rc)
        # 只有"文件被占用/拒绝访问"这种可恢复错误才值得重试；
        # 版本不存在、找不到包这类重试多少次都一样，直接返回。
        if not any(k in last for k in ("WinError 5", "拒绝访问", "Access is denied",
                                       "另一个程序正在使用", "being used by another")):
            break
    tail = "\n".join((last or "").strip().splitlines()[-12:])
    return False, "pip 失败：\n%s" % tail


def run_py(venv_dir: Path, module: str, args: List[str], cwd: Optional[str] = None,
           env: Optional[Dict[str, str]] = None, timeout: int = 600
           ) -> Tuple[int, str]:
    py = venv_python(venv_dir)
    return run([str(py), "-m", module] + args, timeout=timeout, cwd=cwd, env=env)


# ---------------------------------------------------------------------------
# PostgreSQL（binaries zip + initdb + pg_ctl register）
# ---------------------------------------------------------------------------
def pg_bin(pg_root: Path) -> Path:
    return pg_root / "bin"


def pg_exe(pg_root: Path, name: str) -> Path:
    return pg_bin(pg_root) / (name + ".exe")


# ---------------------------------------------------------------------------
# 离线预置运行时（整包交付时用）
# ---------------------------------------------------------------------------
# 解压目录 runtime/ 与安装目录的对应关系。铺好之后，后面每一步都会因为
# 「已就位」而自动跳过下载与 pip —— 服务器全程不需要联网。
RUNTIME_MAP = (
    ("pgsql", "pgsql"),                    # PostgreSQL 17.2 二进制（含 psql）
    ("py-orch", "venv"),                   # 编排层运行时（Python + 依赖）
    ("py-ls", "lsvenv"),                   # 标注引擎运行时（Python + LS 全依赖）
    ("caddy/caddy.exe", "bin/caddy.exe"),  # 入口反代
)


def runtime_dir() -> Optional[Path]:
    """离线包预置的运行时目录。没有就返回 None（走在线安装）。"""
    cands = []
    try:
        cands.append(payload_dir() / 'runtime')
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path(sys.executable).parent / 'runtime')
    cands.append(Path(sys.executable).parent / 'payload' / 'runtime')
    for d in cands:
        if (d / 'pgsql' / 'bin' / 'postgres.exe').is_file():
            return d
    return None


def sources_dir() -> Optional[Path]:
    """载荷里的源码树（编排层 / 迁移 / 品牌补丁 / 前端产物）。

    这是离线整包里**唯一**必须铺到安装目录的东西：后面第 04 步要
    `services/orchestrator/requirements.txt`、第 05 步要 `deploy/migrations/*.sql`、
    第 07 步要 `labelstudio/brand/apply_brand.py`，全都从 <安装目录> 下读。
    用 requirements.txt 当探针：它在，整棵树就都在。
    """
    cands = []
    try:
        cands.append(payload_dir() / "app")
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path(sys.executable).parent / "payload" / "app")
    for d in cands:
        if d.is_dir():
            return d
    return None


def _merge_tree(src: Path, dst: Path) -> Tuple[int, int]:
    """把 src 合并进 dst：只补缺的/大小不一致的文件，绝不删 dst 里已有的东西。

    为什么不是 rmtree + copytree：重跑安装时 dst 里已经躺着 pgsql/、venv/、
    lsvenv/（几个 GB），删了就白装了。源码这棵树只有 1 MB 出头，逐个比对很快。
    """
    n_copy = n_skip = 0
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if "__pycache__" in f.parts or f.suffix in (".pyc", ".pyo"):
            continue
        rel = f.relative_to(src)
        t = dst / rel
        if t.is_file():
            try:
                if t.stat().st_size == f.stat().st_size:
                    n_skip += 1
                    continue
            except OSError:
                pass
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        n_copy += 1
    return n_copy, n_skip


def stage_sources(app_dir: Path, log: Callable[[str], None]):
    """把载荷里的源码树铺到安装目录。没有载荷返回 None（exe + payload 目录老形态）。

    这一步以前是缺的：载荷里明明有 `payload/app/…`，但没有任何一步把它铺到
    安装目录，于是第 04 步必然报「载荷里缺编排层源码」。现在改成第 02 步
    一开始就铺，铺完当场校验三个关键文件，缺一个立刻说清缺哪棵树。
    """
    src = sources_dir()
    if src is None:
        # 载荷里没有源码树，但安装目录里已经有——上次已经铺过了。
        # 不能返回 None 就完事：None 表示「没有载荷」，会让人误判成装不上。
        if (app_dir / "services" / "orchestrator" / "requirements.txt").is_file():
            return True, "源码已在安装目录（上次已铺开）"
        return None
    n_files = sum(1 for f in src.rglob("*") if f.is_file())
    log("铺开源码树：%s（%d 个文件）" % (src, n_files))
    n_copy, n_skip = _merge_tree(src, app_dir)
    log("  写入 %d 个、已在位 %d 个" % (n_copy, n_skip))
    fix = ('手动补齐：robocopy "<解压目录>/智途/payload/app" "%s" /E' % app_dir)
    must = (
        ("services/orchestrator/requirements.txt", "编排层依赖清单"),
        ("labelstudio/brand/apply_brand.py", "品牌补丁脚本"),
    )
    for rel, what in must:
        if not (app_dir / rel).is_file():
            return False, ("%s 没铺成功：%s%s%s" % (what, app_dir / rel, NL, fix))
    migs = list((app_dir / "deploy" / "migrations").glob("0*.sql"))
    if not migs:
        return False, ("迁移脚本没铺成功：%s%s%s"
                       % (app_dir / "deploy" / "migrations", NL, fix))
    return True, "源码已就绪（%d 个迁移脚本）" % len(migs)


def _dir_stats(p: Path) -> Tuple[int, int]:
    n = size = 0
    for f in p.rglob('*'):
        if f.is_file():
            n += 1
            try:
                size += f.stat().st_size
            except OSError:
                pass
    return n, size


def _stamp_ok(src: Path, dst: Path) -> bool:
    """目标是否已经是这一份（文件数 + 总字节数比对）。

    不比时间戳：解压/复制会改 mtime，拷完必然旧于源，永远判不过。
    """
    if not dst.is_dir():
        return False
    return _dir_stats(src) == _dir_stats(dst)


def _runtime_ready(rel_dst: str, d: Path) -> bool:
    """目标位置是否已经是可用的运行时（不比对源，因为源可能已被 move 走）。

    判据刻意取得窄：只看那个「没有它就一定跑不起来」的文件。
      pgsql          -> bin/postgres.exe
      venv/lsvenv    -> python.exe（便携运行时）或 Scripts/python.exe（标准 venv）
      bin/caddy.exe  -> 文件本身
    """
    if rel_dst.endswith(".exe"):
        return d.is_file() and d.stat().st_size > 0
    if rel_dst == "pgsql":
        return (d / "bin" / "postgres.exe").is_file()
    return ((d / "python.exe").is_file()
            or (d / "Scripts" / "python.exe").is_file())


def stage_runtime(app_dir: Path, log: Callable[[str], None]):
    """把 runtime/ 铺到安装目录。没有预置运行时返回 None（走在线安装）。"""
    src = runtime_dir()
    if src is None:
        # 离线包里的 runtime/ 被搬空（同盘是 move），但安装目录里已经铺好：
        # 这是「重跑安装」的正常情况，必须认成已就位，否则会掉回在线装 Python。
        if (_runtime_ready("venv", app_dir / "venv")
                and _runtime_ready("pgsql", app_dir / "pgsql")):
            return True, "离线运行时已就位（上次已铺开）"
        return None
    log("检测到离线运行时：%s" % src)
    for rel_src, rel_dst in RUNTIME_MAP:
        s = src / rel_src
        d = app_dir / rel_dst
        # 先看目标是否已经就位。必须放在「源是否存在」前面：同盘是 move
        # 而不是 copy，装过一次之后离线包里的 runtime/ 就空了；此时重跑
        # 如果先判源，会误报「离线包不完整」，用户得重新解压 351 MB。
        if _runtime_ready(rel_dst, d):
            log("  %s 已就位，跳过" % rel_dst)
            continue
        if not s.exists():
            # caddy 允许缺席：它是单文件，第 09 步会自己下（45 MB）。
            # 直接把是否预置 caddy 变成硬阻断，会让整个离线包因为一个可下载
            # 的小组件而完全没法用。
            if rel_src.startswith("caddy/"):
                log("  caddy 未预置，第 09 步会自动下载（约 45 MB）")
                continue
            return False, ("离线包不完整：缺 runtime/%s%s"
                           "重新解压完整的 zip（不要只拷 exe 出来）。"
                           % (rel_src, NL))
        if s.is_file():
            if d.is_file() and d.stat().st_size == s.stat().st_size:
                log("  %s 已就位，跳过" % rel_dst)
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            log("  %s -> %s" % (rel_src, rel_dst))
            continue
        if _stamp_ok(s, d):
            log("  %s 已就位，跳过" % rel_dst)
            continue
        n, size = _dir_stats(s)
        log("  铺开 %s（%d 个文件 / %.0f MB）…" % (rel_src, n, size / 1048576))
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        # 同盘直接 move（改目录项，秒级）；跨盘只能拷。装完这份就不需要了。
        try:
            same_drive = s.drive.lower() == app_dir.drive.lower()
        except Exception:  # noqa: BLE001
            same_drive = False
        if same_drive:
            shutil.move(str(s), str(d))
        else:
            shutil.copytree(s, d)
        log("  %s 完成" % rel_dst)
    return True, "离线运行时已铺开（全程不联网、不跑 pip）"


def pg_installed(pg_root: Path) -> bool:
    """只要有 initdb 和 pg_ctl 就算就位。

    不要求 psql：备用源（Maven 包）里没有客户端工具，但服务端完全可用。
    """
    return (pg_exe(pg_root, "initdb").is_file()
            and pg_exe(pg_root, "pg_ctl").is_file()
            and pg_exe(pg_root, "postgres").is_file())


def pg_full_tools(pg_root: Path) -> bool:
    """有没有 psql / pg_dump（只有官方 zip 带）。"""
    return pg_exe(pg_root, "psql").is_file() and pg_exe(pg_root, "pg_dump").is_file()


def pg_toolset(pg_root: Path) -> str:
    return "full" if pg_full_tools(pg_root) else "server"


def unzip(zip_path: Path, dest: Path, log: Callable[[str], None]) -> Tuple[bool, str]:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            log("  解压 %d 个条目…" % len(names))
            zf.extractall(dest)
        return True, "已解压到 %s" % dest
    except Exception as exc:  # noqa: BLE001
        return False, "解压失败：%s" % exc


PG_SOURCES = (
    {"kind": "zip", "label": "EnterpriseDB 官方包（含 psql / pg_dump）",
     "file": "postgresql-17.2-1-windows-x64-binaries.zip",
     "url": PG_ZIP_URL, "min": 80 * 1048576},
    {"kind": "jar", "label": "Maven Central 备用包（仅服务端，23 MB）",
     "file": "embedded-postgres-windows-amd64-17.2.0.jar",
     "url": PG_ALT_URL, "min": 15 * 1048576},
)


def _pg_extract(pkg: Path, kind: str, pg_root: Path,
                log: Callable[[str], None]) -> Tuple[bool, str]:
    """把下载到的包摊平到 pg_root（bin/ lib/ share/ 直接在下面）。"""
    tmp = pg_root.parent / "_pg_unpack"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "zip":
            with zipfile.ZipFile(pkg) as zf:
                log("  解压 %d 个条目…" % len(zf.namelist()))
                zf.extractall(tmp)
            inner = tmp / "pgsql"
            src = inner if (inner / "bin").is_dir() else tmp
        else:
            # jar 里就一个 txz：postgres-windows-x86_64.txz
            with zipfile.ZipFile(pkg) as zf:
                names = [n for n in zf.namelist() if n.endswith(".txz")]
                if not names:
                    return False, "备用包里没有 .txz，包结构变了"
                txz = tmp / names[0]
                with zf.open(names[0]) as fh, open(txz, "wb") as out:
                    shutil.copyfileobj(fh, out)
            tx_dir = tmp / "_tx"
            tx_dir.mkdir()
            log("  解压 txz（约 1500 个条目）…")
            with tarfile.open(txz, "r:xz") as tf:
                try:
                    tf.extractall(tx_dir, filter="data")     # py3.12+
                except TypeError:
                    tf.extractall(tx_dir)
            src = tx_dir
        if not (src / "bin" / "initdb.exe").is_file():
            return False, "解压后没找到 bin\\initdb.exe（包结构变了）"
        if pg_root.exists():
            shutil.rmtree(pg_root, ignore_errors=True)
        shutil.move(str(src), str(pg_root))
        return True, "已就位"
    except Exception as exc:  # noqa: BLE001
        return False, "解压失败：%s" % exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pg_fetch(pg_root: Path, cache_dir: Path, log: Callable[[str], None],
             extra_dirs: Tuple[Path, ...] = (), allow_download: bool = True
             ) -> Tuple[bool, str]:
    """按顺序试多个源把 PostgreSQL 装到 pg_root，返回 (ok, 说明)。

    为什么要多源：官方 zip 只有 EDB 的 CDN 一家在发（国内外各大镜像站都**没有**
    `postgresql/binary/` 目录，实测 404），国内拉 330 MB 偶尔会卡死。卡死时
    自动退到 Maven Central 的 23 MB 备用包 —— 服务端、initdb 全都有，只是没有
    psql / pg_dump，SQL 改用 venv 里的 psycopg 执行（见 pg_apply）。
    "装得上"比"工具全"重要：工具缺了能用代码补，装不上就整台机器白跑。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    tried: List[str] = []
    for src in PG_SOURCES:
        pkg: Optional[Path] = None
        for d in tuple(extra_dirs) + (cache_dir,):
            cand = Path(d) / src["file"]
            if cand.is_file() and cand.stat().st_size >= src["min"]:
                log("  用已就位的包：%s" % cand)
                pkg = cand
                break
        if pkg is None:
            if not allow_download:
                tried.append("%s：没有预置包" % src["label"])
                continue
            dst = cache_dir / src["file"]
            log("下载 %s …" % src["label"])
            # timeout 是"多久没收到字节就放弃"，不是总时长：卡死 45 秒即换下一个源
            ok, msg = download(src["url"], dst, log, min_bytes=src["min"], timeout=45.0)
            log("  " + msg)
            if not ok:
                tried.append("%s：%s" % (src["label"], msg))
                continue
            pkg = dst
        ok, msg = _pg_extract(pkg, src["kind"], pg_root, log)
        if ok and pg_installed(pg_root):
            return True, "%s → %s" % (src["label"], pg_root)
        tried.append("%s：%s" % (src["label"], msg))
    return False, "所有 PostgreSQL 安装包来源都失败：\n" + "\n".join(
        "   · " + t for t in tried)


def pg_data_state(data_dir: Path) -> Tuple[str, int]:
    """数据目录处于哪一档：absent / empty / leftover / cluster，附带文件数。

    `leftover` 是要命的那一档：目录非空但**没有 PG_VERSION**。
    initdb 成功做的第一件实事就是写 PG_VERSION，所以这种目录必然是「上次
    initdb 中途失败」留下的残骸（网络断、杀软拦、磁盘满、用户点了取消）。
    直接在它上面重跑 initdb，Windows 会走到 `fixing permissions on existing
    directory` 那一步，靠改 ACL 收权 → `Permission denied`，而且每次重跑
    都是同一处同一句，永远过不去。
    """
    if not data_dir.is_dir():
        return "absent", 0
    # 有效集群不数文件：PG 数据目录动辄上万个条目，数了也只为打印，白花时间。
    if (data_dir / "PG_VERSION").is_file():
        return "cluster", 0
    n = sum(1 for p in data_dir.rglob("*") if p.is_file())
    return ("leftover" if n else "empty"), n


def force_rmtree(path: Path) -> Tuple[bool, str]:
    """删目录，专门对付 Windows 的三个失败点。

    1. **只读属性**：shutil.rmtree 直接 PermissionError，必须先 os.chmod(S_IWRITE)；
    2. **文件被 postgres.exe 独占**：先 taskkill，否则表文件删不掉；
    3. **路径过长 / 特殊字符**：用 cmd 的 `rmdir /S /Q` 兜底（它认长路径）。
    每轮之间 sleep 1 秒给句柄释放的时间，这是实测能过的最小间隔。
    """
    import stat as _stat

    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, _stat.S_IWRITE)
            func(target)
        except Exception:
            pass

    if not path.exists():
        return True, "目录本来就不存在"
    for attempt in range(3):
        try:
            shutil.rmtree(path, onerror=_on_error)
        except OSError:
            pass
        if not path.exists():
            return True, "已清空"
        if attempt == 0:
            run(["taskkill.exe", "/F", "/IM", "postgres.exe"], timeout=30)
        elif attempt == 1:
            run(["cmd.exe", "/c", "rmdir", "/S", "/Q", str(path)], timeout=120)
        time.sleep(1)
    if not path.exists():
        return True, "已清空"
    left = sum(1 for p in path.rglob("*") if p.is_file()) if path.exists() else 0
    return False, "还剩 %d 个文件删不掉（权限或被占用）" % left


def pg_initdb(pg_root: Path, data_dir: Path, superpass: str,
              log: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """初始化数据目录。

    -A scram-sha-256 + --pwfile：不给密码的空口令集群在 Windows 上等于把库敞开，
    而且 pg_hba 后续要按 scram 写。

    这里有三层防御，对应真机踩过的坑：
      1. 有 PG_VERSION → 有效集群，**绝不碰**（哪怕服务没起来，那也是数据）；
      2. 无 PG_VERSION 但非空 → initdb 残骸，先清干净再建（否则永远卡在 chmod）；
      3. 第一次 initdb 失败 → 清空再试一次（杀软实时扫描新目录时 chmod 会偶发失败）。
    """
    say = log or (lambda _m: None)

    def _stop_pg() -> None:
        """清理前先停服务。

        只 taskkill 不够：服务是 auto 启动的，SCM 会按恢复策略把 postgres 又拉起来，
        于是刚删掉的文件立刻被重新独占 —— 表现为「删了一半，报 Permission denied」。
        """
        svc = TASKS["pg"]
        if service_exists(svc):
            say("先停掉服务 %s（否则文件被占用删不干净）" % svc)
            run(["sc.exe", "stop", svc], timeout=120)
            time.sleep(2)

    state, n = pg_data_state(data_dir)
    if state == "cluster":
        return True, "数据目录已存在（PG_VERSION 在），跳过初始化"
    if state == "leftover":
        say("发现上次 initdb 没成功的残留（%d 个文件、没有 PG_VERSION），先清掉再重建" % n)
        _stop_pg()
        ok, msg = force_rmtree(data_dir)
        say("清理结果：%s" % msg)
        if not ok:
            return False, (
                "清不掉上次留下的数据目录：%s\n%s\n\n"
                "用管理员 PowerShell 执行下面四行，再重新运行安装程序"
                "（已完成的步骤会自动跳过）：\n"
                "  sc.exe stop ZhituPostgreSQL\n"
                "  taskkill /F /IM postgres.exe\n"
                "  takeown /F \"%s\" /R /D Y\n"
                "  Remove-Item -LiteralPath \"%s\" -Recurse -Force"
                % (data_dir, msg, data_dir, data_dir))

    if state == "empty":
        # 空目录也删掉：initdb 对「已存在的目录」（哪怕空的）都会先走
        # `fixing permissions on existing directory`，而那一步就是改 ACL。
        # 干脆让它自己创建 —— 新建出来的目录直接继承父目录权限，不需要改。
        try:
            data_dir.rmdir()
        except OSError:
            pass

    # 关键：**不要预先建出 data_dir**。让 initdb 自己创建，避开改权限那条路径。
    # 父目录必须先有，pwfile 要写在那里。
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    last = ""
    for attempt in (1, 2):
        pwfile = data_dir.parent / ".initpw"
        pwfile.write_text(superpass, encoding="utf-8")
        try:
            rc, out = run([str(pg_exe(pg_root, "initdb")), "-D", str(data_dir),
                           "-U", "postgres", "-A", "scram-sha-256",
                           "--pwfile", str(pwfile), "-E", "UTF8",
                           "--locale=C"], timeout=600)
        finally:
            try:
                pwfile.unlink()
            except OSError:
                pass
        if rc == 0:
            return True, "数据目录已初始化"
        last = (out or "").strip()[-600:] or str(rc)
        if attempt == 1:
            say("initdb 第 1 次失败，清空目录后再试一次"
                "（Windows 上常见于杀毒软件正在扫描这个新目录）…")
            _stop_pg()
            force_rmtree(data_dir)
            data_dir.parent.mkdir(parents=True, exist_ok=True)

    return False, (
        "initdb 失败：\n%s\n\n"
        "数据目录：%s\n"
        "如果重跑还是同一句，用管理员 PowerShell 执行下面四行把目录清干净，"
        "再重新运行安装程序：\n"
        "  sc.exe stop ZhituPostgreSQL\n"
        "  taskkill /F /IM postgres.exe\n"
        "  takeown /F \"%s\" /R /D Y\n"
        "  Remove-Item -LiteralPath \"%s\" -Recurse -Force"
        % (last, data_dir, data_dir, data_dir))


def pg_register_service(pg_root: Path, data_dir: Path, service: str,
                        port: int = DEFAULT_PG_PORT) -> Tuple[bool, str]:
    rc, out = run([str(pg_exe(pg_root, "pg_ctl")), "register",
                   "-N", service, "-D", str(data_dir), "-S", "auto"], timeout=120)
    if rc != 0:
        return False, "注册服务失败：%s" % (out[-400:] or rc)
    run(["sc.exe", "start", service], timeout=120)
    for _ in range(20):
        if port_open(port):
            return True, "服务 %s 已启动，%d 在监听" % (service, port)
        time.sleep(1)
    return False, "服务已注册但 %d 没起来，看 %s\\log 里的启动日志" % (port, data_dir)


def pg_port_owner(port: int = DEFAULT_PG_PORT) -> Tuple[int, str]:
    """监听该端口的进程 PID 与命令行。拿不到返回 (0, "")。

    用途只有一个：确认端口上跑的是**我们自己**那个 data 目录的实例。
    不是的话绝不能去改口令 —— 那会把用户自己装的 PostgreSQL 口令改掉。
    """
    script = (
        "$c = Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue"
        " | Select-Object -First 1;"
        " if ($c) { $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess);"
        " Write-Output ('PID=' + $c.OwningProcess);"
        " Write-Output ('CMD=' + $p.CommandLine) }" % port)
    rc, out = ps(script, timeout=60)
    if rc != 0:
        return 0, ""
    pid, cmd = 0, ""
    for line in (out or "").splitlines():
        s = line.strip()
        if s.startswith("PID="):
            try:
                pid = int(s[4:].strip())
            except ValueError:
                pid = 0
        elif s.startswith("CMD="):
            cmd = s[4:].strip()
    return pid, cmd


def pg_owns_port(data_dir: Path, port: int = DEFAULT_PG_PORT) -> Tuple[bool, str]:
    """这个端口是不是由 data_dir 这个集群在跑。

    判据从强到弱：postmaster.pid 的 PID == 端口占用 PID；否则看命令行里有没有
    这个 data 目录。两条都拿不到信息时**放行**（不能因为查询失败就断定是别人的库，
    那会让正常安装永远过不去），但只要拿到任何一条能证伪的证据就拒绝。
    """
    pid, cmd = pg_port_owner(port)
    lock = data_dir / "postmaster.pid"
    our_pid = 0
    if lock.is_file():
        try:
            our_pid = int(lock.read_text(encoding="utf-8", errors="replace")
                          .splitlines()[0].strip())
        except (OSError, ValueError, IndexError):
            our_pid = 0
    if pid and our_pid and pid == our_pid:
        return True, "端口 %d 属于 %s（PID %d）" % (port, data_dir, pid)
    needle = str(data_dir).lower().rstrip("\\")
    if cmd and needle and needle in cmd.lower():
        return True, "端口 %d 的进程命令行里有 %s" % (port, data_dir)
    if not pid and not cmd:
        return True, "查不到端口 %d 的占用进程，按自己的集群处理" % port
    return False, ("端口 %d 上跑的不是这个数据目录的实例"
                   "（PID %s，命令行 %s）" % (port, pid or "?", (cmd or "未知")[:150]))


def pg_verify_super(pg_root: Path, port: int, superpass: str,
                    py: Optional[str] = None,
                    timeout: int = 60) -> Tuple[bool, str, int]:
    """用给定口令连一下。连得上才说明口令是真的。

    rc == 9 表示这台机器既没有 psql 也没有可用的 psycopg（在线+备用源那套组合，
    第 03 步时 venv 还没装依赖）——这种情况不是口令错，调用方要能区分，
    否则会误判成「口令不对」而去改一个没问题的库。
    """
    rc, out = pg_apply(pg_root, "postgres", "SELECT 1", superpass,
                       py=py, port=port, timeout=timeout)
    return rc == 0, (out or ""), rc


def pg_restart(pg_root: Path, data_dir: Path, service: str, port: int,
               log: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """停再起 PostgreSQL。改了 pg_hba.conf 必须重启才会重新读。"""
    say = log or (lambda _m: None)
    if service_exists(service):
        run(["sc.exe", "stop", service], timeout=180)
        for _ in range(30):
            if not port_open(port):
                break
            time.sleep(1)
        run(["sc.exe", "start", service], timeout=180)
    else:
        ctl = str(pg_exe(pg_root, "pg_ctl"))
        run([ctl, "stop", "-D", str(data_dir), "-m", "fast"], timeout=180)
        logdir = data_dir / "log"
        try:
            logdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # postgres 会继承管道，用 run() 会等 EOF 假死 —— 必须 run_daemon
        run_daemon([ctl, "start", "-D", str(data_dir),
                    "-l", str(logdir / "forge-reset.log")], timeout=180)
    for _ in range(30):
        if port_open(port):
            return True, "PostgreSQL 已重启，%d 在监听" % port
        time.sleep(1)
    say("[提示] 重启后 %d 仍没监听，看 %s\\log 下的启动日志" % (port, data_dir))
    return False, "PostgreSQL 重启失败：%d 没监听" % port


# 重置口令时临时用的 pg_hba：只放开本机回环，几秒后立刻换回去。
HBA_TRUST = (
    "# 智途安装程序临时放宽本机认证（重置 postgres 口令用），装完自动恢复\n"
    "local   all             all                                     trust\n"
    "host    all             all             127.0.0.1/32            trust\n"
    "host    all             all             ::1/128                 trust\n"
)


def pg_reset_super_password(pg_root: Path, data_dir: Path, service: str,
                            newpass: str, port: int = DEFAULT_PG_PORT,
                            py: Optional[str] = None,
                            log: Optional[Callable[[str], None]] = None
                            ) -> Tuple[bool, str]:
    """把超管口令改成 newpass。

    为什么需要：数据目录里有 PG_VERSION 就是一份有效集群，绝不能重新 initdb
    （那是毁数据）。可口令一旦丢了（换机器、清过 %LOCALAPPDATA%、上次装到
    一半），就只能改口令。标准做法是临时把本机回环放成 trust → 重启 →
    ALTER USER → 把原 pg_hba 写回去 → 再重启。

    改之前必须先确认端口上是自己的集群：对手的库改口令 = 破坏用户数据。
    """
    say = log or (lambda _m: None)
    mine, why = pg_owns_port(data_dir, port)
    if not mine:
        return False, ("不敢动这个库：%s。\n"
                       "请确认 C:\\Zhitu 之外有没有另装的 PostgreSQL；"
                       "如果有，先停掉它再重跑安装程序。" % why)
    hba = data_dir / "pg_hba.conf"
    if not hba.is_file():
        return False, "找不到 %s，无法重置口令" % hba
    orig = hba.read_text(encoding="utf-8", errors="replace")
    err = ""
    try:
        hba.write_text(HBA_TRUST, encoding="utf-8", newline="\n")
        say("临时放开本机认证（仅 127.0.0.1，几秒）以重置口令…")
        ok, msg = pg_restart(pg_root, data_dir, service, port, log=log)
        if not ok:
            err = msg
        else:
            sql = "ALTER USER postgres PASSWORD '%s'" % newpass.replace("'", "''")
            rc, out = pg_apply(pg_root, "postgres", sql, "", py=py,
                               port=port, timeout=90)
            if rc != 0:
                err = "改口令失败：%s" % ((out or "").strip()[-400:] or rc)
    except Exception as exc:  # noqa: BLE001
        err = "改口令时异常：%s" % exc
    finally:
        # 无论成败都必须把 pg_hba 恢复回去：留在 trust 等于本机免密登录
        try:
            hba.write_text(orig, encoding="utf-8", newline="\n")
        except OSError as exc:
            say("[严重] pg_hba.conf 恢复失败：%s —— 请手工把 trust 改回 scram-sha-256" % exc)
    ok, msg = pg_restart(pg_root, data_dir, service, port, log=log)
    if err:
        return False, err
    if not ok:
        return False, "口令已改但重启失败：%s" % msg
    good, out, _rc = pg_verify_super(pg_root, port, newpass, py=py)
    if not good:
        return False, "口令改完仍连不上：%s" % (out or "").strip()[-300:]
    return True, "超级用户口令已重置并验证通过"


def psql(pg_root: Path, dbname: str, sql: str, superpass: str,
         user: str = "postgres", timeout: int = 300) -> Tuple[int, str]:
    """执行 SQL。密码走 PGPASSWORD 环境变量，不落命令行（命令行会被进程列表看到）。"""
    return run([str(pg_exe(pg_root, "psql")), "-h", "127.0.0.1",
                "-p", str(DEFAULT_PG_PORT), "-U", user, "-d", dbname,
                "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
               timeout=timeout, env={"PGPASSWORD": superpass},
               input_text=sql)


def psql_file(pg_root: Path, dbname: str, path: Path, superpass: str,
              user: str = "postgres", timeout: int = 300) -> Tuple[int, str]:
    return run([str(pg_exe(pg_root, "psql")), "-h", "127.0.0.1",
                "-p", str(DEFAULT_PG_PORT), "-U", user, "-d", dbname,
                "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
               timeout=timeout, env={"PGPASSWORD": superpass})


def sql_split(text: str) -> List[str]:
    """把一段 .sql 切成单条语句。

    为什么不用 `text.split(';')`：迁移里有 `$$ ... $$` 函数体、`'...'` 字面量、
    `--` 与 `/* */` 注释，里面都有分号。按字符扫一遍、跟踪当前所处状态才是对的。
    psql 的元命令（行首 `\\`）在这里直接丢掉 —— 它们只有 psql 认，备用源没有 psql。
    """
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("\\")]
    s = "\n".join(lines)
    out: List[str] = []
    buf: List[str] = []
    i, n = 0, len(s)
    while i < n:
        if s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j
            buf.append("\n")
            continue
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            buf.append(" ")
            continue
        ch = s[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(s[i:j])
            i = j
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if s[j] == '"':
                    if j + 1 < n and s[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(s[i:j])
            i = j
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_0-9]*\$", s[i:])
            if m:
                tag = m.group(0)
                j = s.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                buf.append(s[i:j])
                i = j
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


_PG_RUNNER = r'''
import json, os, sys
cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
try:
    import psycopg
except ImportError:
    print("venv 里没有 psycopg", flush=True)
    raise SystemExit(9)
dsn = "host=%s port=%s dbname=%s user=%s password=%s" % (
    cfg["host"], cfg["port"], cfg["db"], cfg["user"],
    os.environ.get("PGPASSWORD", ""))
n = 0
with psycopg.connect(dsn, autocommit=True) as conn:
    for i, stmt in enumerate(cfg["statements"], 1):
        try:
            cur = conn.execute(stmt)
            n += 1
            # 把 SELECT 的结果打出来：安装程序靠它判断"对象是否已存在"，
            # 没有 psql 时这就是唯一的信息来源。
            if cur.description:
                rows = cur.fetchall()
                cols = "|".join(d.name for d in cur.description)
                print("RESULT %d %s" % (len(rows), cols), flush=True)
                for r in rows[:20]:
                    print("ROW " + "|".join(
                        "" if v is None else str(v) for v in r), flush=True)
        except Exception as exc:
            head = " ".join(str(stmt).split())
            print("第 %d/%d 条语句失败：%s" % (i, len(cfg["statements"]), exc), flush=True)
            print("  SQL: %s" % head[:220], flush=True)
            raise SystemExit(3)
print("OK 共 %d 条语句" % n, flush=True)
'''


def pg_apply(pg_root: Path, dbname: str, sql: str, superpass: str,
             py: Optional[str] = None, user: str = "postgres",
             timeout: int = 900, port: int = DEFAULT_PG_PORT) -> Tuple[int, str]:
    """执行一段 SQL。有 psql 走 psql，没有就走 venv 里的 psycopg。

    `py` 是装了 psycopg 的 python（编排层的 venv）。口令走 PGPASSWORD 环境变量，
    不落磁盘也不进命令行 —— 临时目录里只写语句本身和连接元信息。
    """
    if pg_exe(pg_root, "psql").is_file():
        return run([str(pg_exe(pg_root, "psql")), "-h", "127.0.0.1",
                    "-p", str(port), "-U", user, "-d", dbname,
                    "-v", "ON_ERROR_STOP=1", "-q", "-f", "-"],
                   timeout=timeout, env={"PGPASSWORD": superpass},
                   input_text=sql)
    if not py or not Path(py).is_file():
        return 9, "没有 psql，也没有可用的 Python 连接器，无法执行 SQL"
    stmts = sql_split(sql)
    if not stmts:
        return 0, "空 SQL"
    tmp = Path(tempfile.mkdtemp(prefix="zhitu-sql-"))
    try:
        runner = tmp / "runner.py"
        runner.write_text(_PG_RUNNER, encoding="utf-8")
        job = tmp / "job.json"
        job.write_text(json.dumps({
            "host": "127.0.0.1", "port": port, "db": dbname,
            "user": user, "statements": stmts}, ensure_ascii=False),
            encoding="utf-8")
        return run([str(py), str(runner), str(job)], timeout=timeout,
                   env={"PGPASSWORD": superpass})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pg_apply_file(pg_root: Path, dbname: str, path: Path, superpass: str,
                  py: Optional[str] = None, user: str = "postgres",
                  timeout: int = 900, port: int = DEFAULT_PG_PORT
                  ) -> Tuple[int, str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return pg_apply(pg_root, dbname, text, superpass, py=py, user=user,
                    timeout=timeout, port=port)


def pg_backup(pg_root: Path, data_dir: Path, out_file: Path, superpass: str,
              log: Callable[[str], None]) -> Tuple[bool, str]:
    """备份数据库。

    有 pg_dump 就逻辑备份（不停服务）；备用源没有 pg_dump，退化成**冷备**：
    停库 → 把整个数据目录打 zip → 再起库。单机十来人规模下停几十秒可接受，
    比"没有备份"好得多。
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if pg_exe(pg_root, "pg_dump").is_file():
        log("pg_dump 逻辑备份 …")
        rc, out = run([str(pg_exe(pg_root, "pg_dump")), "-h", "127.0.0.1",
                       "-p", str(DEFAULT_PG_PORT), "-U", "postgres",
                       "-d", "forge", "-Fc", "-f", str(out_file)],
                      timeout=3600, env={"PGPASSWORD": superpass})
        if rc != 0 or not out_file.is_file():
            return False, "pg_dump 失败：%s" % (out or rc)
        return True, str(out_file)
    log("没有 pg_dump，改用冷备（停库→压缩数据目录→起库）…")
    ctl = str(pg_exe(pg_root, "pg_ctl"))
    run([ctl, "stop", "-D", str(data_dir), "-m", "fast"], timeout=180)
    try:
        zpath = out_file.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in data_dir.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(data_dir))
        return True, str(zpath)
    finally:
        # 起库必须用 run_daemon：capture_output 会被 postgres 继承的管道卡死
        run_daemon([ctl, "start", "-D", str(data_dir), "-w", "-t", "60"], timeout=180)


# ---------------------------------------------------------------------------
# 计划任务（等价于开机服务）
# ---------------------------------------------------------------------------
def service_exists(name: str) -> bool:
    """Windows **服务**（SCM），和下面的计划任务是两套东西。

    PostgreSQL 走 `pg_ctl register` 注册成真正的服务，用 schtasks.exe 查永远查不到。
    这一点在「重跑安装」时会造成两个连锁误判：
      1. 自己的库已经在 5432 上跑着，却被判成「别的程序占用 5432」→ 自动换 5433，
         配置里写 5433，后面编排层按 5433 连 —— 装完就是连不上库；
      2. 判成「服务不存在」→ 再 register 一次 → `service already exists` 直接失败。
    `sc.exe query` 的**退出码**可靠（输出文本会本地化，但不要解析它）。
    """
    rc, _ = run(["sc.exe", "query", name], timeout=30)
    return rc == 0


def pg_log_tail(data_dir: Path, lines: int = 25) -> str:
    """读 postgres.log 末尾。**失败时必须能自己说清原因**，不能让用户发文件。"""
    for cand in (data_dir / "log" / "postgres.log",
                 data_dir / "postgres.log"):
        try:
            if not cand.is_file():
                continue
            raw = cand.read_bytes()
            if not raw:
                continue
            text = raw.decode("utf-8", "replace")
            tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
            return "\n".join(tail)
        except OSError:
            continue
    return ""


def _pg_classify(tail: str) -> Tuple[str, str]:
    """把 postgres.log 的报错翻成「一句人话 + 该怎么办」。

    这些是 Windows 上真机最常见的四种起不来，注释里写明触发条件，
    别再靠"让用户把日志发出来"来定位。
    """
    low = (tail or "").lower()
    if "lock file" in low and "already exists" in low:
        return ("stale_lock",
                "data 目录被上一个进程锁着（postmaster.pid 残留）。"
                "安装程序会尝试清掉残留锁后重试。")
    if "another postmaster" in low or "is another server using the same data" in low:
        return ("stale_lock", "另一个 postgres 正占着这个 data 目录。")
    if "could not bind" in low or "address already in use" in low \
            or "only one usage of each socket address" in low:
        return ("port_busy",
                "端口被别的程序占用（常见：旧版智途注册的 Windows 服务、"
                "用户自己装的 PostgreSQL）。换端口或先停掉占用方。")
    if "incompatible" in low or "initialized by" in low \
            or "database files are incompatible" in low:
        return ("version",
                "data 目录是**另一个 PostgreSQL 版本**建的（老安装残留）。"
                "本版本自带 17.x，起不了旧集群。")
    if "permission denied" in low or "could not open" in low \
            or "access is denied" in low:
        return ("perm",
                "文件权限被拒（杀软/组策略/目录属于别的账户）。")
    if "could not create shared memory" in low or "not enough" in low:
        return ("mem", "共享内存申请失败，通常是内存不足或页面文件被禁用。")
    if not low.strip():
        return ("nolog", "postgres.log 是空的 —— pg_ctl 没把 postgres 拉起来"
                         "（多半是 data 目录不可用，见下面 pg_ctl status 的原话）。")
    return ("other", "未识别的启动失败，见下面的日志原文。")


def pg_clear_stale_lock(data_dir: Path,
                        log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """清掉死进程留下的 postmaster.pid。

    只在「锁文件里的 PID 已经不存在」时删；PID 还活着说明真有人在跑，
    删了等于把别人的库从底下抽走。
    """
    say = log or (lambda _m: None)
    pidf = data_dir / "postmaster.pid"
    if not pidf.is_file():
        return None
    try:
        first = pidf.read_text(encoding="utf-8", errors="replace").splitlines()
        pid = int((first[0] if first else "0").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and pid_alive(pid):
        return "锁文件里的进程 %d 还活着，不动它" % pid
    try:
        pidf.unlink()
    except OSError as exc:
        return "想删残留锁 %s 但删不掉：%s" % (pidf, exc)
    say("清掉残留的 postmaster.pid（进程 %s 已不存在）" % (pid or "未知"))
    return "cleared"


# 公开别名：setup 里要拿它把日志翻成人话，别写成 C._pg_classify
pg_classify = _pg_classify


def pg_start_local(pg_root: Path, data_dir: Path, port: int = DEFAULT_PG_PORT,
                   log: Optional[Callable[[str], None]] = None
                   ) -> Tuple[bool, str]:
    """直接用 pg_ctl 起 PostgreSQL，**不注册 Windows 服务**。

    为什么可以不起服务：服务器常开，注册服务只为「开机自启」，代价是管理员
    权限、组策略、SCM 恢复策略一堆约束，还会占着 data 目录不放。
    pg_ctl 内部会 CreateRestrictedProcess 降权，所以管理员会话下能正常起 ——
    直接双击 postgres.exe 反而会被 PG 拒绝（不允许管理员权限跑服务端）。

    失败时**自己把原因读出来**（postgres.log 末尾 + pg_ctl 退出码），
    并对「残留锁」「端口被占」这两种能自愈的先自愈重试一次 ——
    真机上最常见的就是这两种，让用户去翻日志属于把问题甩回去。
    """
    say = log or (lambda _m: None)
    if port_open(port):
        return True, "PostgreSQL 已在 127.0.0.1:%d 运行" % port
    ctl = str(pg_exe(pg_root, "pg_ctl"))
    logdir = data_dir / "log"
    try:
        logdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    logfile = logdir / "postgres.log"

    def _once(tag: str) -> Tuple[bool, str, int]:
        say("启动 PostgreSQL（pg_ctl 直起，不注册服务）%s…" % tag)
        # -w 等它真的起来；不加的话 pg_ctl 立刻返回，端口轮询会白等。
        # 必须 run_daemon：postgres 会继承管道，run() 会一直等 EOF。
        rc, _ = run_daemon([ctl, "start", "-D", str(data_dir), "-w", "-t", "120",
                            "-l", str(logfile)], timeout=180)
        # pg_ctl 的退出码很有信息量：0=起来了；1=起不来（原因在日志）；
        # 4=数据目录不可用。轮询端口的同时也在等这个码。
        for i in range(30):
            if port_open(port):
                return True, "PostgreSQL 在 127.0.0.1:%d 监听（pg_ctl 直起）" % port, rc
            # pg_ctl -w 返回非 0 就表示它自己已判定失败，再干等 30 秒纯属让用户干瞪眼
            if rc != 0 and i >= 3:
                return False, "", rc
            time.sleep(1)
        return False, "", rc

    # 第一轮：先清残留锁
    lockmsg = pg_clear_stale_lock(data_dir, say)
    if lockmsg and lockmsg != "cleared":
        say(lockmsg)
    ok, msg, rc = _once("")
    if ok:
        return True, msg

    tail = pg_log_tail(data_dir)
    kind, hint = _pg_classify(tail)

    # 能自愈的第二种：锁还在（第一轮清的可能被新一次失败又写回来了）
    if kind == "stale_lock":
        again = pg_clear_stale_lock(data_dir, say)
        if again == "cleared":
            ok, msg, rc = _once("（清除残留锁后重试）")
            if ok:
                return True, msg
            tail = pg_log_tail(data_dir)
            kind, hint = _pg_classify(tail)

    detail = [("pg_ctl 退出码 %s；%d 端口没有监听。" % (rc, port)), hint]
    if tail:
        detail.append("postgres.log 最后几行：\n" + tail[-1200:])
    else:
        # 日志为空有两种：postgres 还没开口就被拒（目录不是集群/版本不符），
        # 或者 -l 那个文件根本没写进去。这时 `pg_ctl status` 的措辞最准
        # （"directory ... is not a database cluster directory"），而且它
        # 是秒退的短命命令，用 run() 不会有继承管道的假死问题。
        rc_s, out_s = run([ctl, "status", "-D", str(data_dir)], timeout=30)
        hint2 = (out_s or "").strip()[:400]
        detail.append("没读到 %s（文件不存在或为空）。" % logfile)
        if hint2:
            detail.append("pg_ctl status 的输出：\n" + hint2)
    if kind == "version":
        detail.append("处理办法：把 %s 改名留档（例如 pgdata-old），再重新运行安装程序，"
                      "它会用自带版本重新 initdb。里面有旧数据的话先备份。" % data_dir)
    elif kind == "port_busy":
        detail.append("处理办法：以管理员身份运行\n"
                      "  netstat -ano | findstr :%d\n"
                      "  tasklist /FI \"PID eq <上面那个PID>\"\n"
                      "认出来是旧版智途的服务就 sc.exe stop ZhituPostgreSQL，"
                      "是别人的库就直接重跑安装程序（它会自动改用 5433）。" % port)
    return False, "\n".join(detail)


def pg_stop_local(pg_root: Path, data_dir: Path, port: int = DEFAULT_PG_PORT,
                  log: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """停掉 pg_ctl 直起的实例。"""
    say = log or (lambda _m: None)
    ctl = str(pg_exe(pg_root, "pg_ctl"))
    say("停止 PostgreSQL …")
    run([ctl, "stop", "-D", str(data_dir), "-m", "fast"], timeout=180)
    for _ in range(20):
        if not port_open(port):
            return True, "PostgreSQL 已停止"
        time.sleep(1)
    return False, "PostgreSQL 仍在 %d 上监听" % port


def remove_service(name: str, log: Optional[Callable[[str], None]] = None,
                   wait_port: int = DEFAULT_PG_PORT) -> bool:
    """停掉并删除一个 Windows 服务（老版本遗留的）。

    不清掉的话它会跟 pg_ctl 直起的实例抢同一个端口和 data 目录，
    表现是「端口被占用」或者「库连上了但数据目录被锁」。

    停完**必须等端口真的关掉**再往下走：SCM 的 stop 是异步的，
    固定 sleep 2 秒在慢机器上不够，紧接着 pg_ctl start 就会撞上
    「lock file postmaster.pid already exists」——真机上第 03 步起不来的
    头号原因就是这个。
    """
    say = log or (lambda _m: None)
    run(["sc.exe", "stop", name], timeout=120)
    for _ in range(25):
        if not port_open(wait_port):
            break
        time.sleep(1)
    rc, out = run(["sc.exe", "delete", name], timeout=60)
    say("删除遗留服务 %s：%s" % (name, "成功" if rc == 0 else ((out or "").strip() or rc)))
    return rc == 0


def pg_cluster_version(data_dir: Path) -> str:
    """data 目录属于哪个 PG 主版本（读 PG_VERSION，如 "17"）。"""
    try:
        txt = (data_dir / "PG_VERSION").read_text(encoding="utf-8", errors="replace")
        return (txt.strip().splitlines() or [""])[0].strip()
    except (OSError, IndexError):
        return ""


def pg_binary_version(pg_root: Path) -> str:
    """自带二进制的主版本（跑 postgres --version，取第一个数字段）。"""
    try:
        exe = str(pg_exe(pg_root, "postgres"))
    except Exception:
        return ""
    rc, out = run([exe, "--version"], timeout=30)
    txt = out or ""
    for tok in txt.replace("(", " ").replace(")", " ").split():
        head = tok.split(".")[0]
        if head.isdigit():
            return head
    return ""


def kill_port(port: int, log: Optional[Callable[[str], None]] = None) -> bool:
    """停掉监听这个端口的进程。

    只杀「命令行里带我们安装目录」的进程 —— 80 端口上可能是 IIS，8080 可能是
    别人跑的别的东西，误杀等于破坏用户环境。认不出来的一律不动。
    """
    say = log or (lambda _m: None)
    needle = str(DEFAULT_APP_DIR).lower()
    for _ in range(3):
        pid, cmd = pg_port_owner(port)
        if not pid:
            return True
        low = (cmd or "").lower()
        if low and needle not in low and APP_ID.lower() not in low:
            say("端口 %d 上是别的程序（PID %d，%s），不动它"
                % (port, pid, (cmd or "")[:120]))
            return False
        run(["taskkill.exe", "/PID", str(pid), "/F", "/T"], timeout=60)
        time.sleep(1)
    return not port_open(port)


def spawn_detached(cmd: List[str], work_dir: Optional[Path] = None,
                   env: Optional[Dict[str, str]] = None) -> int:
    """起一个跟安装程序彻底解耦的常驻进程，返回 PID（失败返回 0）。

    不注册计划任务/服务也能让平台一直跑：服务器本来就常开，为了「开机自启」
    去写 SCM 与任务计划库，换来的是管理员权限、组策略冲突、CIM 通道故障
    （Register-ScheduledTask 报 CimException）这一堆麻烦。
    CREATE_NO_WINDOW + 新进程组：父进程退出后继续跑，也不弹黑窗口。
    """
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        p = subprocess.Popen([str(c) for c in cmd],
                             cwd=str(work_dir) if work_dir else None,
                             creationflags=flags,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             close_fds=True,
                             env={**os.environ, **(env or {})})
    except (OSError, ValueError):
        return 0
    return p.pid


def pid_alive(pid: int) -> bool:
    """进程是否还活着。不用 psutil，避免多一个依赖。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def task_exists(name: str) -> bool:
    rc, _ = run(["schtasks.exe", "/Query", "/TN", name], timeout=30)
    return rc == 0


# 计划任务用 XML 模板创建，不走 PowerShell 的 Register-ScheduledTask。
# 真机踩过：后者抛 CimException「拒绝访问」，错误还被 CLIXML 转义成乱码；
# 换成 schtasks /Create /XML 一次成功 —— XML 把 Principal / Trigger / Settings
# 全写死，不经过 Cmdlet 的对象转换，也没有 PowerShell 版本差异。
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>%(desc)s</Description>
  </RegistrationInfo>
  <Triggers>
%(trigger)s
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>%(limit)s</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c "%(cmd)s"</Arguments>
      <WorkingDirectory>%(wd)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

# 开机触发 + 每 1 分钟重复（崩溃后自动拉起）。Repetition 不写 Duration 即无限重复。
_TRIGGER_BOOT = """    <BootTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </BootTrigger>"""


def _xml_escape(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trigger_daily(hour: int, minute: int) -> str:
    """每日触发器。StartBoundary 用「下一个该来的时刻」。

    如果写成今天的 03:00 而现在已经过了 03:00，配合 StartWhenAvailable=true
    会在装完的瞬间立刻补跑一次备份（冷备要停库）—— 装机时吓人一跳。
    所以过了点就顺延到明天。
    """
    now = datetime.datetime.now()
    day = now.date()
    if (now.hour, now.minute) >= (hour, minute):
        day = day + datetime.timedelta(days=1)
    return """    <CalendarTrigger>
      <StartBoundary>%sT%02d:%02d:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>""" % (day.isoformat(), hour, minute)


def _task_xml_dir() -> Path:
    """任务 XML 的落盘位置。留在磁盘上是故意的：

    自动注册失败时，这是唯一确定能用的补救手段 —— 用户在管理员 PowerShell
    里一行 `schtasks /Create /TN x /XML <这个文件> /F` 就能补上。
    放 C:\\ProgramData\\<APP_ID>\\tasks：管理员可写、SYSTEM 读得到、用户找得到。
    """
    base = os.environ.get("ProgramData") or r"C:\ProgramData"
    d = Path(base) / APP_ID / "tasks"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return Path(tempfile.mkdtemp(prefix="zhitu-task-"))


def _task_create_xml(name: str, cmd_path: Path, work_dir: Path, desc: str,
                     trigger: str, limit: str) -> Tuple[bool, str]:
    """schtasks /Create /XML。成功返回 (True, msg)，失败返回 (False, 原因)。"""
    xml = _TASK_XML % {"desc": _xml_escape(desc), "trigger": trigger,
                       "limit": limit, "cmd": _xml_escape(cmd_path),
                       "wd": _xml_escape(work_dir)}
    xmlf = _task_xml_dir() / ("%s.xml" % name)
    try:
        # schtasks 只认 Unicode（UTF-16 LE + 带 BOM）的 XML 文件，
        # 写成 UTF-8 会报「无法解析 XML」。
        xmlf.write_text(xml, encoding="utf-16")
        rc, out = run(["schtasks.exe", "/Create", "/TN", name,
                       "/XML", str(xmlf), "/F"], timeout=120)
        if rc == 0:
            return True, "任务 %s 已注册" % name
        reason = (out or "").strip()[-300:] or "schtasks 返回 %d" % rc
        return False, ("%s%s（XML 已留在 %s，可手工补：%s）"
                       % (reason, NL, xmlf,
                          'schtasks /Create /TN %s /XML "%s" /F' % (name, xmlf)))
    except OSError as exc:
        return False, "写任务 XML 失败：%s" % exc


def task_register(name: str, cmd_path: Path, work_dir: Path,
                  description: str = "") -> Tuple[bool, str]:
    """注册开机任务：SYSTEM 身份、最高权限、无限时长、1 分钟重复触发。

    主路径是 schtasks /Create /XML；失败再退回 PowerShell Cmdlet。
    原来只有 Cmdlet 一条路，真机上报 CimException 拒绝访问就彻底卡死，
    错误还被 CLIXML 转义成 `_x000D__x000A_` 乱码，用户连原因都看不到。
    """
    desc = description or ("智途 " + name)
    ok, msg = _task_create_xml(name, cmd_path, work_dir, desc,
                               _TRIGGER_BOOT, "PT0S")
    if ok:
        return True, msg
    ok2, msg2 = _task_register_ps(name, cmd_path, work_dir, desc)
    if ok2:
        return True, "任务 %s 已注册（PowerShell 回退路径）" % name
    return False, ("注册计划任务 %s 失败：%s%s"
                   "PowerShell 回退也失败：%s"
                   "两条路都失败通常是这台机器不允许创建 SYSTEM 计划任务"
                   "（组策略/安全软件），或者是管理员身份没生效。"
                   % (name, msg, NL, msg2))


def _task_register_ps(name: str, cmd_path: Path, work_dir: Path,
                      description: str = "") -> Tuple[bool, str]:
    """PowerShell Cmdlet 版（回退用）。"""
    script = (
        "$ErrorActionPreference='Stop';"
        "$n='%s';"
        "if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {"
        "  Unregister-ScheduledTask -TaskName $n -Confirm:$false };"
        "$a=New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%s\"' "
        "-WorkingDirectory '%s';"
        "$t=New-ScheduledTaskTrigger -AtStartup;"
        "$t.Repetition=(New-ScheduledTaskTrigger -Once -At (Get-Date) "
        "-RepetitionInterval (New-TimeSpan -Minutes 1) "
        "-RepetitionDuration ([TimeSpan]::MaxValue)).Repetition;"
        "$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' "
        "-LogonType ServiceAccount -RunLevel Highest;"
        "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew;"
        "Register-ScheduledTask -TaskName $n -Action $a -Trigger $t "
        "-Principal $p -Settings $s -Description '%s' | Out-Null"
        % (name, str(cmd_path), str(work_dir), description or ("智途 " + name))
    )
    rc, out = ps(script, timeout=120)
    if rc != 0:
        return False, ((out or "").strip()[-400:] or "返回码 %d" % rc)
    return True, "任务 %s 已注册" % name


def task_register_daily(name: str, cmd_path: Path, work_dir: Path,
                        hour: int = 3, minute: int = 0,
                        description: str = "",
                        limit_minutes: int = 120) -> Tuple[bool, str]:
    """注册「每天几点跑一次」的任务（SYSTEM 身份，错过会补跑）。

    ★ 和 task_register 的区别必须分清，混用会出大事：
      task_register 是**服务型**任务 —— 开机启动 + 每 1 分钟重复触发，
      用来把崩掉的进程拉起来。备份要是用那个，一天会跑 1440 次，
      而且 MultipleInstances=IgnoreNew 会让它跟自己的前一次互相推挤。
      所以每日任务单独一个函数，触发器是 -Daily，执行时限 2 小时。

    -StartWhenAvailable：机器凌晨关着/重启错过了，开机后会补跑一次。
    -MultipleInstances IgnoreNew：上一份还没跑完就不再起第二份（冷备要停库）。
    StartBoundary 由 _trigger_daily() 算，不用 '03:00' 字符串 ——
      后者走区域设置解析，换台机器/换个语言就可能解析失败。
    """
    desc = description or ("智途 " + name)
    ok, msg = _task_create_xml(name, cmd_path, work_dir, desc,
                               _trigger_daily(hour, minute),
                               "PT%dM" % max(1, limit_minutes))
    if ok:
        return True, "每日任务 %s 已注册（每天 %02d:%02d）" % (name, hour, minute)
    ok2, msg2 = _task_register_daily_ps(name, cmd_path, work_dir, hour, minute,
                                        desc, limit_minutes)
    if ok2:
        return True, ("每日任务 %s 已注册（PowerShell 回退路径，每天 %02d:%02d）"
                      % (name, hour, minute))
    return False, ("注册每日任务 %s 失败：%s%s"
                   "PowerShell 回退也失败：%s" % (name, msg, NL, msg2))


def _task_register_daily_ps(name: str, cmd_path: Path, work_dir: Path,
                            hour: int, minute: int, description: str,
                            limit_minutes: int) -> Tuple[bool, str]:
    """PowerShell Cmdlet 版每日任务（回退用）。"""
    script = (
        "$ErrorActionPreference='Stop';"
        "$n='%s';"
        "if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {"
        "  Unregister-ScheduledTask -TaskName $n -Confirm:$false };"
        "$a=New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%s\"' "
        "-WorkingDirectory '%s';"
        "$t=New-ScheduledTaskTrigger -Daily -At "
        "([datetime]::Today.AddHours(%d).AddMinutes(%d));"
        "$p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' "
        "-LogonType ServiceAccount -RunLevel Highest;"
        "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes %d) "
        "-MultipleInstances IgnoreNew;"
        "Register-ScheduledTask -TaskName $n -Action $a -Trigger $t "
        "-Principal $p -Settings $s -Description '%s' | Out-Null"
        % (name, str(cmd_path), str(work_dir), hour, minute, limit_minutes,
           description)
    )
    rc, out = ps(script, timeout=120)
    if rc != 0:
        return False, ((out or "").strip()[-400:] or "返回码 %d" % rc)
    return True, "每日任务 %s 已注册" % name


def task_start(name: str) -> Tuple[bool, str]:
    rc, out = run(["schtasks.exe", "/Run", "/TN", name], timeout=60)
    return rc == 0, (out or "").strip() or ("已启动 %s" % name)


def task_stop(name: str) -> Tuple[bool, str]:
    rc, out = run(["schtasks.exe", "/End", "/TN", name], timeout=60)
    return rc == 0, (out or "").strip() or ("已停止 %s" % name)


def task_delete(name: str) -> Tuple[bool, str]:
    rc, out = run(["schtasks.exe", "/Delete", "/TN", name, "/F"], timeout=60)
    return rc == 0, (out or "").strip() or ("已删除 %s" % name)


def write_launcher(path: Path, env: Dict[str, str], work_dir: Path,
                   command: str, log_file: Path) -> None:
    """写 run-xxx.cmd 启动器。

    环境变量以字面 `set` 写死，不在 cmd 里解析 .env —— cmd 遇到值里的 `=` `&`
    会静默截断，而且计划任务本身带不了环境块。
    """
    lines = ["@echo off", "setlocal"]
    for k in sorted(env):
        lines.append('set "%s=%s"' % (k, env[k]))
    lines.append('cd /d "%s"' % work_dir)
    lines.append('%s 1>>"%s" 2>>"%s"' % (
        command, log_file, log_file.with_suffix(".err.log")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    if path.with_suffix(".err.log") != log_file:
        pass


def random_secret(nbytes: int = 48) -> str:
    import secrets
    return secrets.token_hex(nbytes)


# ---------------------------------------------------------------------------
# 自检（不需要管理员、不需要装任何东西）
# ---------------------------------------------------------------------------
def selftest() -> int:
    out = cli_sink("selftest")
    out("=" * 72)
    out("%s 原生版客户端 · 自检" % APP_NAME)
    out("=" * 72)
    out("app_dir      : %s" % app_dir())
    out("payload_dir  : %s" % payload_dir())
    out("config       : %s" % config_path())
    out("日志          : %s" % out.path)
    out("管理员        : %s" % is_admin())
    b, n = windows_build()
    out("Windows      : %s build %s" % (n, b))
    out("局域网地址    : %s" % (local_ip() or "未识别"))
    # 内存放在自检里，是为了"装之前先在服务器上跑一次 --selftest"就能判断够不够，
    # 而不是装到第 06 步被系统杀掉再回来查。
    ram = total_ram_gb()
    if ram:
        note = "" if ram >= 3.5 else "   ← 低于 4 GB，装到第 06 步会被系统杀掉"
        out("物理内存      : %.1f GB%s" % (ram, note))
    else:
        out("物理内存      : 读不到（不影响安装，自己确认一下 ≥ 4 GB）")
    out("")
    out("--- Python ---")
    py, ver = find_python()
    out("找到          : %s" % (py or "没有 3.10+ 的 Python"))
    if py:
        out("版本          : %s" % ver)
    out("")
    out("--- 端口 ---")
    for port in (DEFAULT_HTTP_PORT, DEFAULT_LS_PORT, DEFAULT_API_PORT, DEFAULT_PG_PORT):
        out("  %-5d %s" % (port, "已占用" if port_open(port) else "空闲"))
    out("")
    out("--- 载荷 ---")
    pdir = payload_dir()
    out("目录存在      : %s" % pdir.is_dir())
    for rel in ("frontend/index.html", "app/services/orchestrator/requirements.txt",
                "app/deploy/migrations/001_schema.sql",
                "app/labelstudio/brand/apply_brand.py",
                "app/web/public/brand", "caddy/Caddyfile.tpl"):
        out("  %-45s %s" % (rel, "有" if (pdir / rel).exists() else "缺"))
    out("")
    found = [q for d in cache_dirs() if d.is_dir()
             for q in sorted(d.glob("*")) if q.is_file()]
    out("缓存包        : %s" % ("、".join(q.name for q in found)
                                 or "无（安装时联网下载）"))
    out("结果         : 自检完成（所有探测都已执行，不代表安装一定会成功）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
