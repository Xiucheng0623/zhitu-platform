#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 Windows 客户端 · 共用底层。

为什么单独一层
----------------------------------------------------------------------------
安装程序（zhitu_setup.py）和监控台（zhitu_monitor.py）要做同一批事：
判断宿主机能不能跑、调 wsl/docker、探测 HTTP 健康、读写配置、拿安装包目录。
这些逻辑抄两遍必然漂移 —— 一个改了判定口径另一个没改，就会出现"安装说
能跑、监控说环境异常"。所以统一放这里，两边只负责画界面。

三条硬约束（本机的血泪教训，换台机器同样成立）
----------------------------------------------------------------------------
1. **所有外部命令都带超时**。wsl.exe 在没装发行版、被组策略拦、或 docker
   daemon 没起来时会**静默挂住**，没有超时的 subprocess 会把界面冻死，
   用户只能强杀进程 —— 看起来像"软件崩了"，其实是等一个永不返回的调用。
2. **输出按 utf-8 + errors=replace 解码**。中文 Windows 上 wsl/docker 的输出
   混合了 UTF-8 与 GBK 片段，用默认编码会抛 UnicodeDecodeError，或者把
   错误信息解成一堆问号，等于没有信息。
3. **失败要能说清是哪一步、下一句该干什么**。绝不把异常栈直接怼给用户，
   也绝不"看起来成功" —— 每一步的返回值都要能被上层判定。
"""
from __future__ import annotations

import ctypes
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_NAME = "智途"
APP_ID = "Zhitu"
# 装到哪个目录。C:\Zhitu 而不是 Program Files：容器相关路径里出现空格会让
# WSL 侧的 shell 拼接变脏，也方便运维直接进去看 .env。
DEFAULT_INSTALL_DIR = r"C:\Zhitu"
DEFAULT_DISTRO = "Ubuntu-22.04"
# 需要从宿主映射进 WSL 的端口（去重后）。对齐 docker-compose 的真实发布：
#   caddy  ${HTTP_PORT:-80} / ${HTTPS_PORT:-443}   → 平台唯一入口
#   LS     ${LS_PORT:-8100}                        → 标注工作台直达
#   web/orchestrator/minio 都是 expose，**不发布到宿主**，不需要映射。
HOST_PORTS = [80, 8100]

# 健康探测点。注意这里必须按 Docker 部署的真实拓扑写，不能沿用开发机端口：
# 开发机是「LS 8080 / 编排层 8300 / 前端 9121」，而容器栈是
# 「Caddy 80 → web:3000 与 orchestrator:8300，LS 单独 8100」。
# 写错端口不会报错，只会让界面一直显示"平台不可达"——人就会去重启服务，
# 越查越偏。
STACK_PORTS = [
    ("平台入口", 80, "http://127.0.0.1/"),
    ("平台 API", 80, "http://127.0.0.1/healthz"),
    ("标注引擎 LS", 8100, "http://127.0.0.1:8100/health"),
]

# 平台与 LS 的浏览器可达地址。这两个值必须写进 .env（PLATFORM_PUBLIC_URL /
# LS_PUBLIC_URL），留空时：LS 的 LABEL_STUDIO_HOST 为空、SSO 票据里的 next
# 是空地址，表现是"能登录但点不进标注工作台"。
DEFAULT_LS_PORT = 8100
DEFAULT_PLATFORM_PORT = 80

# 平台要求的 9 个密钥（与 deploy/restore.sh 的 SECRET_KEYS 同一份口径）
SECRET_KEYS = [
    "FORGE_JWT_SECRET", "ORCH_DB_PASSWORD", "FORGE_SSO_CLIENT_SECRET",
    "LS_SSO_SECRET", "LS_REFRESH_TOKEN", "LS_WEBHOOK_SECRET",
    "LS_DB_PASSWORD", "MINIO_PASSWORD", "LS_ADMIN_PASSWORD",
]


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def app_dir() -> Path:
    """exe 所在目录（打包后）/ 源码目录（开发时）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """PyInstaller 解包出的临时目录；开发时等于源码目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else app_dir()


def payload_dir() -> Path:
    """离线载荷目录。

    交付形态刻意是「EXE + payload/ 同目录」而不是把 3 GB 镜像塞进单个 exe：
      * 单文件 exe 超过 2 GB 在 FAT32 U 盘上根本放不下，客户拷不过来；
      * 每次升版都要重打整包，运维没法只换镜像；
      * 杀软对超大单文件的扫描/隔离行为不可预测。
    想做成"一个文件"时用 7-Zip 自解压壳套这个目录即可，不必改这里的逻辑。
    """
    return app_dir() / "payload"


def config_path() -> Path:
    """客户端状态文件。默认 %LOCALAPPDATA%\\Zhitu\\client.json。

    ZHITU_CONFIG_DIR 是给**自动化校验/测试**用的：校验脚本会在临时目录里跑
    一遍安装流程，如果不隔离，它会把测试用的假 IP、假安装目录写进用户真实
    的 client.json —— 之后监控台显示的全是那个假地址，排查时极易误判。
    """
    override = os.environ.get("ZHITU_CONFIG_DIR")
    if override:
        return Path(override) / "client.json"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID / "client.json"


def load_config() -> Dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # 配置坏了不能让客户端打不开：按空配置跑，界面上提示重新保存一次
        return {}


def save_config(cfg: Dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# 进程调用
# ---------------------------------------------------------------------------
def run(cmd: List[str], timeout: int = 30, cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """跑一个外部命令，永远返回 (returncode, 合并输出)。

    约定三个特殊返回码，不能和"跑了但失败"混为一谈 —— 混了就会给出完全
    错误的建议（比如 wsl.exe 被组策略拦，却提示"发行版名字写错了"）：
      9009  命令根本不存在（FileNotFoundError）
      9008  命令存在但起不来（WinError 5 拒绝访问 / 被安全策略拦）
      9998  超时被我们杀掉
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, cwd=cwd, env=full_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return 9009, "找不到命令：%s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 9998, "命令超时（%ds）：%s" % (timeout, " ".join(cmd))
    except OSError as exc:
        return 9008, "无法执行 %s：%s（常见原因：需要管理员权限，或被安全策略/组策略禁止）" % (
            cmd[0], exc)
    out = (proc.stdout or b"") + (proc.stderr or b"")
    text = out.decode("utf-8", errors="replace")
    # Windows 工具常输出 UTF-16 片段（wsl --status 就是），出现大量 \x00
    # 时按 utf-16 重解一次，否则日志里全是乱码方块。
    if text.count("\x00") > len(text) // 4:
        text = out.decode("utf-16-le", errors="replace")
    return proc.returncode, text.strip()


def fix_console() -> None:
    """让中文在 cmd / PowerShell / 管道 / 日志重定向下都不乱码。

    两个坑同时存在：
      * 中文 Windows 控制台默认代码页 936(GBK)，Python 按 locale 编码输出，
        重定向到管道/文件时对方按 UTF-8 解 → 一片问号；
      * 直接把 UTF-8 字节写进 936 的控制台 → 同样乱码。
    所以两头都改：输出流固定 UTF-8，控制台代码页一起切到 65001。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境没有 reconfigure，忽略即可
            pass
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 - 无控制台（noconsole 打包）时会失败，正常
        pass


def setup_dpi() -> float:
    """开启 DPI 感知并返回缩放比例（96 dpi → 1.0）。

    不开感知的后果不是"字小"，而是**整窗被 Windows 位图拉伸**：125%/150%
    的笔记本上界面糊成一层毛边，截图工具抓到的坐标和实际像素也对不上
    （本次实测：winfo 返回 1020 逻辑像素，物理是 1530，截图直接裁掉右侧）。
    售后场景里客户只会说"你们的软件很模糊"，很难联想到 DPI。

    返回值给界面用：窗口尺寸与字号都乘它，视觉比例才和系统一致。
    """
    try:
        # 1 = PROCESS_SYSTEM_DPI_AWARE。用 1 而不是 2（per-monitor）：
        # 多屏拖拽时 2 需要处理 WM_DPICHANGED 重新布局，tkinter 没有原生支持。
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - Win7/Server2008 没有 shcore
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    scale = 1.0
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi:
            scale = dpi / 96.0
    except Exception:  # noqa: BLE001
        pass
    return max(1.0, min(3.0, scale))


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 非 Windows 或无 windll 时按非管理员
        return False


def elevate_self() -> bool:
    """以管理员重新启动自己。返回 True 表示已发起提权（本进程应退出）。"""
    try:
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe, params = sys.executable, '"%s"' % os.path.abspath(sys.argv[0])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 1)
        return rc > 32
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 宿主环境判定
# ---------------------------------------------------------------------------
def windows_build() -> Tuple[int, str]:
    """返回 (build 号, 产品名)。拿不到时返回 (0, '')。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            build = int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0])
            name = str(winreg.QueryValueEx(k, "ProductName")[0])
            return build, name
    except Exception:  # noqa: BLE001
        return 0, ""


def is_server(name: str) -> bool:
    return "Server" in (name or "")


def host_report() -> Dict[str, Any]:
    """宿主三态判定，与 deploy/windows-docker/check-host.ps1 同一套口径。

    READY       能直接装（WSL2 + 虚拟化都就绪）
    INSTALLABLE 需要先启功能/装 WSL（可以自动做，但要重启）
    BLOCKED     这台机器跑不了，装了也是白装（必须先说清楚，不能装到一半才报错）
    """
    build, product = windows_build()
    rep: Dict[str, Any] = {
        "build": build, "product": product, "server": is_server(product),
        "admin": is_admin(), "blockers": [], "todo": [], "wsl": False,
        "distro": False, "docker": False, "state": "BLOCKED",
    }

    # 1) 版本门槛：WSL2 需要 build 18362+；Server 侧 2019(17763) 只有 WSL1。
    if build and build < 19041:
        rep["blockers"].append(
            "系统版本过低（build %d）：WSL 2 需要 build 19041 及以上，"
            "Windows Server 2019（17763）只有 WSL 1，跑不了 Linux 容器" % build)

    # 2) WSL 本体与版本
    rc, out = run(["wsl.exe", "--status"], timeout=15)
    if rc == 9009:
        # 没有 wsl.exe = 功能没启用。这是**可安装**，不是跑不了 —— 装不上才
        # 叫跑不了。判成 BLOCKED 会误导用户放弃一台其实能用的机器。
        rep["todo"].append("启用 WSL 功能（需要管理员 + 重启）")
    elif rc == 9008:
        rep["todo"].append("wsl.exe 无法启动（%s）—— 请用管理员运行；"
                           "若仍失败，检查是否被组策略/安全软件拦截" % out[:120])
    elif rc == 9998:
        rep["blockers"].append(
            "wsl.exe 有响应但 15 秒不返回：WSL 服务异常，先重启这台机器再试")
    else:
        rep["wsl"] = rc == 0 or "Default Version" in out or "默认版本" in out
        if not rep["wsl"]:
            rep["todo"].append("把 WSL 默认版本设为 2 并重启")

    # 3) 发行版
    rc_l, out_l = run(["wsl.exe", "-l", "-q"], timeout=15)
    distros = [x.strip().strip("\x00") for x in out_l.splitlines() if x.strip()]
    rep["distros"] = distros
    rep["distro"] = any(DEFAULT_DISTRO.split("-")[0].lower() in d.lower()
                        for d in distros)

    # 4) docker（宿主机看不到是正常的：容器引擎在 WSL 里）
    rep["docker"] = shutil.which("docker") is not None

    if rep["blockers"]:
        rep["state"] = "BLOCKED"
    elif rep["wsl"] and rep["distro"]:
        rep["state"] = "READY"
    else:
        rep["state"] = "INSTALLABLE"
        if not rep["distro"]:
            rep["todo"].append("安装 %s 发行版" % DEFAULT_DISTRO)
    return rep


def wsl_distro_ready(distro: str = DEFAULT_DISTRO) -> bool:
    rc, out = run(["wsl.exe", "-d", distro, "-u", "root", "--", "true"], timeout=25)
    return rc == 0


def docker_in_wsl(distro: str = DEFAULT_DISTRO) -> Tuple[bool, str]:
    rc, out = run(["wsl.exe", "-d", distro, "-u", "root", "--",
                   "docker", "version", "--format", "{{.Server.Version}}"], timeout=40)
    return (rc == 0 and bool(out.strip()), out.strip())


def compose(distro: str, project_dir: str, args: List[str],
            timeout: int = 180) -> Tuple[int, str]:
    """在 WSL 里对这个 compose 项目执行子命令。"""
    cmd = ["wsl.exe", "-d", distro, "-u", "root", "--",
           "docker", "compose",
           "--project-directory", project_dir,
           "-f", project_dir + "/docker-compose.yml"]
    return run(cmd + args, timeout=timeout)


def compose_ps(distro: str, project_dir: str) -> List[Dict[str, str]]:
    """容器清单。用 --format json 逐行解析，比抠表格稳。"""
    rc, out = compose(distro, project_dir,
                      ["ps", "--format", "json"], timeout=60)
    rows: List[Dict[str, str]] = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        rows.append({
            "name": str(obj.get("Name") or obj.get("Service") or "?"),
            "service": str(obj.get("Service") or "?"),
            "state": str(obj.get("State") or "?"),
            "health": str(obj.get("Health") or ""),
            "status": str(obj.get("Status") or ""),
            "image": str(obj.get("Image") or ""),
        })
    return rows


# ---------------------------------------------------------------------------
# 健康探测
# ---------------------------------------------------------------------------
def probe_http(url: str, timeout: float = 4.0) -> Dict[str, Any]:
    """探测一个健康端点。**必须绕过系统代理**：本机/内网都常见 HTTP_PROXY，
    走代理去探 127.0.0.1 会得到一个和真实状态无关的 502。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    try:
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read(400)
            return {"ok": 200 <= resp.status < 400, "code": resp.status,
                    "ms": int((time.time() - t0) * 1000),
                    "body": body.decode("utf-8", errors="replace")[:200]}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "code": exc.code, "ms": int((time.time() - t0) * 1000),
                "body": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": 0, "ms": int((time.time() - t0) * 1000),
                "body": str(exc)[:200]}


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def stack_health() -> List[Dict[str, Any]]:
    out = []
    for name, port, url in STACK_PORTS:
        p = probe_http(url)
        out.append({"name": name, "port": port, "url": url,
                    "ok": p["ok"], "code": p["code"], "ms": p["ms"]})
    return out


# ---------------------------------------------------------------------------
# 载荷校验
# ---------------------------------------------------------------------------
def read_manifest() -> Optional[Dict[str, Any]]:
    p = payload_dir() / "manifest.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def validate_payload() -> Dict[str, Any]:
    """载荷自检。缺什么就说缺什么，绝不"看起来可装"。

    问题分三档，因为**两种安装方式的要求不同**：
      problems     —— 不管联网还是离线都必须有（缺了根本装不成）
      need_build   —— 只有联网构建需要（源码树 + Dockerfile）
      need_images  —— 只有离线载入需要（docker save 的 tar）
    混成一堆的话，联网安装会被"缺镜像包"误拦，离线安装会被"缺源码"放行。
    """
    pdir = payload_dir()
    rep: Dict[str, Any] = {"dir": str(pdir), "exists": pdir.is_dir(),
                           "manifest": None, "problems": [], "need_build": [],
                           "need_images": [], "images": [], "compose": False,
                           "caddy": False, "build_src": False, "migrations": 0}
    if not pdir.is_dir():
        rep["problems"].append(
            "找不到载荷目录：%s（EXE 必须和 payload/ 放在同一层）" % pdir)
        return rep

    man = read_manifest()
    rep["manifest"] = man
    if man is None:
        rep["problems"].append("payload/manifest.json 缺失或不是合法 JSON")

    comp = pdir / "compose" / "docker-compose.yml"
    rep["compose"] = comp.is_file()
    if not rep["compose"]:
        rep["problems"].append("payload/compose/docker-compose.yml 缺失")

    # caddy 挂载 ./caddy/Caddyfile，官方镜像不带这个文件。缺了不是"打开是
    # 默认页"，而是 caddy 反复重启、80 端口整条入口没了。
    rep["caddy"] = (pdir / "compose" / "caddy" / "Caddyfile").is_file()
    if not rep["caddy"]:
        rep["problems"].append(
            "payload/compose/caddy/Caddyfile 缺失（caddy 起不来，平台 80 端口不通）")

    mig = pdir / "compose" / "migrations"
    if mig.is_dir():
        rep["migrations"] = len(list(mig.glob("*.sql")))
    if rep["migrations"] == 0:
        rep["problems"].append("payload/compose/migrations/ 里没有 .sql（库结构建不起来）")

    # 联网构建要的源码树：Dockerfile 引用的东西一个都不能少
    cs = pdir / "compose"
    build_need = [
        ("docker/web.Dockerfile", cs / "docker" / "web.Dockerfile"),
        ("docker/orchestrator.Dockerfile", cs / "docker" / "orchestrator.Dockerfile"),
        ("docker/labelstudio.Dockerfile", cs / "docker" / "labelstudio.Dockerfile"),
        ("src/web/package.json", cs / "src" / "web" / "package.json"),
        ("src/web/public/brand/sy-mark-dark.png",
         cs / "src" / "web" / "public" / "brand" / "sy-mark-dark.png"),
        ("src/orchestrator/requirements.txt",
         cs / "src" / "orchestrator" / "requirements.txt"),
        ("src/orchestrator/app", cs / "src" / "orchestrator" / "app"),
        ("src/labelstudio/brand/apply_brand.py",
         cs / "src" / "labelstudio" / "brand" / "apply_brand.py"),
    ]
    missing = [name for name, p in build_need if not p.exists()]
    rep["build_src"] = not missing
    if missing:
        rep["need_build"].append(
            "payload/compose 下缺联网构建所需的源码：" + "、".join(missing))

    img_dir = pdir / "images"
    if img_dir.is_dir():
        tars = sorted(img_dir.glob("*.tar"))
        rep["images"] = [{"name": t.name, "mb": round(t.stat().st_size / 1048576, 1)}
                         for t in tars]
    if not rep["images"]:
        rep["need_images"].append(
            "payload/images/ 里没有 .tar 镜像包（离线安装必须自带 docker save 产物）")
    return rep


def local_ip() -> str:
    """本机对外 IP（局域网地址，不是 127.0.0.1）。

    为什么需要：`.env` 里 PLATFORM_PUBLIC_URL / LS_PUBLIC_URL 必须是**浏览器
    能打开**的地址。填 127.0.0.1 时，员工在自己电脑上点进标注工作台会指向
    他自己的机器 → 打不开；SSO 票据里的 next 也会错。

    做法：连一个外部地址但不发包（UDP connect 只选路由），拿到的就是默认
    出口网卡的地址。拿不到就返回空串，由调用方决定降级成 127.0.0.1。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.5)
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


def platform_url(host: Optional[str] = None) -> str:
    ip = host or load_config().get("host_ip") or local_ip() or "127.0.0.1"
    return "http://%s/" % ip


def ls_url(host: Optional[str] = None) -> str:
    ip = host or load_config().get("host_ip") or local_ip() or "127.0.0.1"
    return "http://%s:%d/" % (ip, DEFAULT_LS_PORT)


def env_example() -> Path:
    return payload_dir() / "compose" / "env.example"


def _apply_env(text: str, values: Dict[str, str]) -> str:
    """按 key 覆盖 env 模板里的同名行，没有的追加。

    为什么不直接 append：模板里已经有 `PLATFORM_PUBLIC_URL=` 这种空行，
    再追加一遍会得到两行同名 key。docker compose 取**后者**，看似没事，
    但人打开 .env 会看到两个值不同的同名项，改错一个就"改了没生效"。
    """
    out: List[str] = []
    seen = set()
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            k = m.group(1)
            out.append('%s="%s"' % (k, values[k]))
            seen.add(k)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append('%s="%s"' % (k, v))
    return "\n".join(out)


def gen_env(target: Path, host_ip: str = "") -> Dict[str, str]:
    """生成 .env。密钥一律现场随机，公开地址按本机 IP 填好。

    两个都必须写死的项，否则装完"看着能用、实际断链"：

    * `PLATFORM_PUBLIC_URL` / `LS_PUBLIC_URL`：留空时 LS 的 LABEL_STUDIO_HOST
      为空、SSO 票据里 next 是空地址 —— 员工登录后点「进入标注工作台」打不开，
      而三个服务的健康检查全是绿的。
    * `LS_REFRESH_TOKEN`：这里故意留空，必须等 LS 起来后登录生成（由安装程序
      第 09 步自动抓取并写回）。空着不影响服务启动，只影响"标注完平台数字
      动不动"——最难查的那一类。
    """
    import secrets
    ip = host_ip or local_ip() or "127.0.0.1"
    values: Dict[str, str] = {
        # —— 9 个密钥 ——
        "FORGE_JWT_SECRET": secrets.token_urlsafe(48),
        "ORCH_DB_PASSWORD": secrets.token_urlsafe(24),
        "FORGE_SSO_CLIENT_SECRET": secrets.token_urlsafe(32),
        "LS_SSO_SECRET": secrets.token_urlsafe(48),
        "LS_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "LS_DB_PASSWORD": secrets.token_urlsafe(24),
        "MINIO_PASSWORD": secrets.token_urlsafe(24),
        "LS_ADMIN_PASSWORD": secrets.token_urlsafe(18),
        # —— 公开地址（浏览器可达）——
        "DOMAIN": "",
        "PLATFORM_PUBLIC_URL": "http://%s" % ip,
        "LS_PUBLIC_URL": "http://%s:%d" % (ip, DEFAULT_LS_PORT),
        "HTTP_PORT": str(DEFAULT_PLATFORM_PORT),
        "LS_PORT": str(DEFAULT_LS_PORT),
        # LS 管理员账号：LS 不发邮件，这个邮箱只是登录名，用本地域名避免被
        # 人误当成真实可收信的邮箱。
        "LS_ADMIN_EMAIL": "admin@zhitu.local",
    }
    src = env_example()
    base = src.read_text(encoding="utf-8").rstrip() if src.is_file() else ""
    header = (
        "# 由智途安装程序生成 %s\n"
        "# 九个密钥为随机值：跨部署复制等于共用同一把钥匙，绝对不要复制。\n"
        "# 公开地址按本机 IP 自动填；换 IP 后改 PLATFORM_PUBLIC_URL / LS_PUBLIC_URL 再重启。\n"
        % time.strftime("%Y-%m-%d %H:%M:%S")
    )
    text = header + "\n" + _apply_env(base, values) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return values


def read_env(path: Path) -> Dict[str, str]:
    """读 .env 成字典（去引号）。不解析 shell 语义，够用即可。"""
    if not path.is_file():
        return {}
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*"?([^"\n]*)"?\s*$', line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def write_env_key(path: Path, key: str, value: str) -> bool:
    """就地把 .env 里某个 key 的值改掉（保留其余内容与注释）。"""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    pat = re.compile(r'^%s\s*=.*$' % re.escape(key), re.M)
    line = '%s="%s"' % (key, value)
    if pat.search(text):
        text = pat.sub(line, text, count=1)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def check_env_file(path: Path) -> List[str]:
    """检查 .env 的**必填项**（缺任一项都不是能用的部署）。"""
    if not path.is_file():
        return [".env 不存在：%s" % path]
    env = read_env(path)
    problems: List[str] = []
    for k in SECRET_KEYS:
        if k == "LS_REFRESH_TOKEN":
            continue          # 故意留空，第 09 步自动补
        if not env.get(k):
            problems.append("%s 为空" % k)
    for k in ("PLATFORM_PUBLIC_URL", "LS_PUBLIC_URL"):
        if not env.get(k):
            problems.append("%s 为空（员工浏览器打不开对应入口）" % k)
    return problems


def fetch_ls_refresh(base: str, email: str, password: str,
                     timeout: int = 40) -> Tuple[bool, str]:
    """登录 LS 换 refresh token。返回 (ok, token 或错误说明)。

    与 deploy/tools/fetch_ls_refresh.py 同一套流程（CSRF → 表单登录 →
    POST /api/token/），内联进客户端是为了不依赖 payload 里的脚本文件：
    联网安装时 payload 可能不完整，而这一步恰恰是"装完能不能用"的关键。

    必须绕过系统代理：本机/内网常有 HTTP_PROXY，走代理去登 127.0.0.1 会
    拿到一个和真实状态无关的 502。
    """
    base = base.rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar))
    try:
        html = opener.open(base + "/user/login/", timeout=timeout).read().decode(
            "utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return False, "打不开 LS 登录页（%s/user/login/）：%s" % (base, exc)
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    if not m:
        return False, "LS 登录页里找不到 csrf 字段（版本不一致？）"
    csrf = m.group(1)
    body = urllib.parse.urlencode(
        {"csrfmiddlewaretoken": csrf, "email": email, "password": password}).encode()
    req = urllib.request.Request(base + "/user/login/", data=body,
                                 headers={"Referer": base + "/user/login/"})
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return False, "登录请求失败 HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return False, "登录请求异常：%s" % exc
    # 登录失败会回到 /user/login/ 渲染错误；成功才跳走
    if "/user/login" in resp.geturl():
        return False, "登录失败：核对 .env 里的 LS_ADMIN_EMAIL / LS_ADMIN_PASSWORD"
    csrf2 = next((c.value for c in jar if c.name == "csrftoken"), csrf)
    req2 = urllib.request.Request(
        base + "/api/token/", data=b"{}",
        headers={"Referer": base + "/", "X-CSRFToken": csrf2,
                 "Content-Type": "application/json"})
    try:
        data = json.load(opener.open(req2, timeout=timeout))
    except Exception as exc:  # noqa: BLE001
        return False, "POST /api/token/ 失败：%s" % exc
    refresh = data.get("refresh") or (data.get("data") or {}).get("refresh")
    if not refresh:
        return False, "/api/token/ 没返回 refresh：%s" % json.dumps(data)[:200]
    return True, refresh


def logs_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID / "logs"


def cli_sink(mode: str) -> Callable[..., None]:
    """命令模式的输出口：**同时**打到 stdout 和日志文件。

    为什么必须落文件：交付的 exe 是 --noconsole 打的（双击不该弹黑框）。
    这种 exe 从计划任务或 cmd 里跑 `--auto` / `--selftest` 时，print 的内容
    没有任何地方能看见 —— 出了问题等于零线索。所以两条都写，文件路径回填
    到返回函数的 .path 上，界面和文档都指得过去。
    """
    path = logs_dir() / ("cli-%s-%s.log" % (mode, time.strftime("%Y%m%d")))

    def emit(msg: str = "") -> None:
        try:
            print(msg, flush=True)
        except Exception:  # noqa: BLE001 - 无控制台时 print 会抛，不能因此中断
            pass
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(str(msg) + "\n")
        except OSError:
            pass

    emit.path = path  # type: ignore[attr-defined]
    return emit


# ---------------------------------------------------------------------------
# 自检（本机可跑，不需要管理员/不需要 WSL）
# ---------------------------------------------------------------------------
def selftest() -> int:
    out = cli_sink("selftest")
    print = out  # noqa: A001 - 让下面的代码统一走落盘通道
    out("=" * 72)
    out("%s 客户端 · 自检" % APP_NAME)
    out("=" * 72)
    out("app_dir      : %s" % app_dir())
    out("payload_dir  : %s" % payload_dir())
    out("config       : %s" % config_path())
    out("日志          : %s" % out.path)
    out("管理员        : %s" % is_admin())
    b, n = windows_build()
    out("Windows      : %s build %s" % (n, b))
    out("")
    out("--- 载荷 ---")
    rep = validate_payload()
    out("目录存在      : %s" % rep["exists"])
    out("compose      : %s / 迁移 %s 个" % (rep["compose"], rep["migrations"]))
    out("镜像         : %s" % ([i["name"] for i in rep["images"]] or "无"))
    out("caddy 入口    : %s" % ("有" if rep["caddy"] else "缺"))
    out("构建源码      : %s" % ("有" if rep["build_src"] else "缺"))
    if rep["manifest"]:
        out("manifest     : v%s" % rep["manifest"].get("version"))
    out("安装方式      : %s" % ("联网构建（无需镜像包）" if not rep["images"]
                                else "离线载入（%d 个镜像包）" % len(rep["images"])))
    for p in rep["problems"]:
        out("  [!] %s" % p)
    if not rep["images"]:
        for p in rep["need_images"]:
            out("  [提示] %s" % p)
    for p in rep["need_build"]:
        out("  [提示] %s" % p)
    out("")
    out("--- 服务健康 ---")
    for h in stack_health():
        out("  %-12s :%-5d %s (%dms)" % (h["name"], h["port"],
                                         "OK" if h["ok"] else "不可达", h["ms"]))
    out("")
    out("--- 宿主 ---")
    hr = host_report()
    out("判定         : %s" % hr["state"])
    for x in hr["blockers"]:
        out("  [阻断] %s" % x)
    for x in hr["todo"]:
        out("  [待办] %s" % x)
    out("发行版       : %s" % (hr.get("distros") or "无"))
    out("结果         : 自检完成（所有探测都已执行，不代表安装一定会成功）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
