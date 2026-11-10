#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 · Windows 安装程序（GUI，非 BAT）。

它到底做什么、不做什么
----------------------------------------------------------------------------
本项目跑的是 **Linux 容器**（LS / PostgreSQL / MinIO / 编排层 / Caddy）。
Windows 上没有"双击就装好"的容器运行时：
  * Docker Desktop 不支持 Windows Server（官方文档 + 订阅协议都写明）；
  * 唯一的正路是 WSL 2 里装 Docker Engine，且要求 Server 2022+ 与嵌套虚拟化。

所以这个 exe 是**编排与监护外壳**，不是运行时本体：
  1. 先判定这台机器到底能不能跑（不能就直接说清楚，绝不留半装状态）；
  2. 把离线镜像 docker load 进 WSL 里的引擎；
  3. 落盘 compose / 迁移脚本 / 随机密钥 .env；
  4. 起栈、自检、开端口代理、注册自启、建快捷方式。

两种安装方式（第 02 步决定，之后不可换）
----------------------------------------------------------------------------
A. 联网构建（payload/images 为空时的默认）
   不 load 镜像，改用 `docker compose up -d` 现场拉基础镜像（postgres / caddy /
   minio）并构建 zhitu/{web,orchestrator,labelstudio}。要求这台机器能出外网，
   首装 20-40 分钟。交付目录因此只有几 MB，不用搬 2-3 GB 的 tar。

B. 离线载入（payload/images 里有 tar 时的默认）
   `docker load` 进 WSL 里的引擎，然后 `--no-build` 起栈。全程不访问外网，
   但交付目录要带上镜像 tar（FAT32 U 盘放不下单个 >4 GB 文件）。

两种模式都会在第 10 步登录 LS 取 refresh token 写回 .env —— 那一步不做，
平台数字永远不跟着标注动，而健康检查是全绿的。

用法
----------------------------------------------------------------------------
    智途安装程序.exe                       # 打开向导（推荐）
    智途安装程序.exe --auto                # 无界面按序执行（自动判断模式）
    智途安装程序.exe --auto --online       # 强制联网构建
    智途安装程序.exe --auto --offline      # 强制离线载入
    智途安装程序.exe --auto --host-ip 10.0.0.8   # 指定员工访问用的服务器地址
    智途安装程序.exe --selftest            # 只跑环境与载荷自检，不动系统
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zhitu_common as C  # noqa: E402

# 与平台一致的深色石墨配色（和 Web 端 tokens.css 同一套色）
BG = "#14161A"
BG2 = "#1A1D22"
BG3 = "#22262C"
FG = "#E8E6E3"
FG_DIM = "#9BA1AA"
ACCENT = "#9B82E8"
OK_C = "#5FBF8B"
WARN_C = "#E0A340"
ERR_C = "#E0685C"
LINE = "#333941"

DISTRO = C.DEFAULT_DISTRO
# WSL 里的部署目录。用 /opt 而不是用户家目录：这个栈要 root 起，跟着用户走
# 会在换账号登录后"项目消失"。
REMOTE_DIR = "/opt/zhitu/deploy"


# ---------------------------------------------------------------------------
# 步骤定义
# ---------------------------------------------------------------------------
class Step:
    def __init__(self, key: str, title: str, fn: Callable, needs_admin: bool = True):
        self.key = key
        self.title = title
        self.fn = fn
        self.needs_admin = needs_admin
        self.state = "pending"   # pending / running / ok / fail / skip
        self.detail = ""


class Installer:
    """安装流程。所有方法都返回 (ok, 说明)，由 GUI 或 --auto 统一呈现。"""

    def __init__(self, log: Callable[[str], None],
                 install_dir: str = C.DEFAULT_INSTALL_DIR,
                 host_ip: str = "", online: Optional[bool] = None):
        self.log = log
        self.install_dir = Path(install_dir)
        self.host_ip = (host_ip or "").strip() or C.local_ip() or "127.0.0.1"
        self.report: Dict[str, object] = {}
        # online=None 表示"自动"：有镜像包就走离线（更快更稳），没有才联网构建。
        # 用户能在界面上强制选，因为有人就是不想让服务器去拉外网。
        self._online_pref = online

    def online(self) -> bool:
        if self._online_pref is not None:
            return self._online_pref
        return not C.validate_payload()["images"]

    # -- 0 体检 -----------------------------------------------------------
    def step_host(self) -> Tuple[bool, str]:
        rep = C.host_report()
        self.report["host"] = rep
        self.log("系统：%s build %s" % (rep["product"], rep["build"]))
        self.log("管理员：%s / WSL：%s / 发行版：%s" %
                 (rep["admin"], rep["wsl"], rep.get("distros") or "无"))
        self.log("访问地址：%s   （员工用这个地址打开平台）" % C.platform_url(self.host_ip))
        if rep["state"] == "BLOCKED":
            for b in rep["blockers"]:
                self.log("[阻断] " + b)
            return False, "这台机器不满足运行条件（见日志），安装已停止。"
        if rep["state"] == "INSTALLABLE":
            for t in rep["todo"]:
                self.log("[待办] " + t)
            return True, "需要先启用 WSL / 安装发行版，向导会尝试自动完成（可能要重启）。"
        return True, "环境就绪。"

    # -- 1 载荷 -----------------------------------------------------------
    def step_payload(self) -> Tuple[bool, str]:
        rep = C.validate_payload()
        self.report["payload"] = rep
        self.log("载荷目录：%s" % rep["dir"])
        for img in rep["images"]:
            self.log("  镜像 %s  %.1f MB" % (img["name"], img["mb"]))
        if rep["migrations"]:
            self.log("  迁移脚本 %d 个" % rep["migrations"])
        self.log("  入口配置 caddy/Caddyfile：%s" % ("有" if rep["caddy"] else "缺"))
        self.log("  构建源码 src/**：%s" % ("有" if rep["build_src"] else "缺"))

        online = self.online()
        self.report["online"] = online
        self.log("安装方式：%s" % ("联网构建（首装 20-40 分钟，需外网）" if online
                                   else "离线载入（docker load 本地镜像）"))

        # 三档问题分别判：核心项两种模式都要，源码只有联网要，镜像只有离线要。
        for p in rep["problems"]:
            self.log("[缺件] " + p)
        if rep["problems"]:
            return False, "部署文件不完整（两种安装方式都缺这几样）。"

        if online:
            for p in rep["need_build"]:
                self.log("[缺件] " + p)
            if rep["need_build"]:
                return False, "联网构建缺少源码树，无法在现场 build 三个服务。"
            if not rep["images"]:
                self.log("[提示] 没有本地镜像包，将直接拉基础镜像并现场构建三个服务。")
            return True, "联网模式：部署文件与构建源码齐备。"

        for p in rep["need_images"]:
            self.log("[缺件] " + p)
        if rep["need_images"]:
            return False, "离线载荷不完整 —— 要么补齐 payload/images，要么改成联网安装。"
        return True, "离线载荷完整（%d 个镜像包）。" % len(rep["images"])

    # -- 2 WSL 与发行版 ---------------------------------------------------
    def step_wsl(self) -> Tuple[bool, str]:
        rep = self.report.get("host") or C.host_report()
        if rep["state"] == "BLOCKED":
            return False, "环境不满足，跳过 WSL 安装。"
        if rep["wsl"] and rep["distro"]:
            return True, "WSL 2 与 %s 已就绪。" % DISTRO

        self.log("启用 WSL 功能（需要管理员，通常要重启一次）…")
        rc, out = C.run(["wsl.exe", "--install", "-d", DISTRO, "--no-launch"], timeout=900)
        self.log(out[-800:] if out else "(无输出)")
        if rc == 9998:
            return False, "wsl --install 超时。请在管理员 PowerShell 里手动执行后重开向导。"
        if rc == 9009:
            return False, "找不到 wsl.exe，无法自动安装。"
        if rc != 0:
            return False, ("wsl --install 返回 %d。常见原因：功能被组策略禁用、"
                           "或需要先重启。请重启后再次运行安装程序。" % rc)
        # 首次安装后未初始化时 -u root 会失败，这里再确认一次
        if C.wsl_distro_ready(DISTRO):
            return True, "WSL 与 %s 安装完成。" % DISTRO
        return False, "WSL 已安装但 %s 尚未初始化，请重启这台机器后重开安装程序。" % DISTRO

    # -- 3 WSL 内 Docker Engine -------------------------------------------
    def step_docker(self) -> Tuple[bool, str]:
        ok, ver = C.docker_in_wsl(DISTRO)
        if ok:
            self.log("Docker Engine 已就绪：%s" % ver)
            return True, "Docker Engine %s" % ver

        debs = sorted((C.payload_dir() / "docker").glob("*.deb")) \
            if (C.payload_dir() / "docker").is_dir() else []
        if debs:
            self.log("离线安装 Docker Engine（%d 个 deb）…" % len(debs))
            # 把 deb 拷进 WSL 再 dpkg -i；先用 PowerShell 无关的方式：wsl cp
            host_dir = str(C.payload_dir() / "docker")
            win_path = subprocess.run(
                ["wsl.exe", "-d", DISTRO, "-u", "root", "--", "mkdir", "-p", "/opt/zhitu/docker"],
                capture_output=True, timeout=60)
            for deb in debs:
                C.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--",
                       "cp", _win_to_wsl(host_dir) + "/" + deb.name, "/opt/zhitu/docker/"],
                      timeout=120)
            rc, out = C.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--", "sh", "-c",
                             "dpkg -i /opt/zhitu/docker/*.deb || apt-get -f install -y"],
                            timeout=900)
            self.log(out[-800:] if out else "(无输出)")
            if rc != 0:
                return False, "离线 deb 安装失败（见日志）。"
        else:
            self.log("payload/docker/ 下没有 deb 包，改为在线安装（要求这台机器能访问外网）…")
            rc, out = C.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--", "sh", "-c",
                             "curl -fsSL https://get.docker.com | sh"], timeout=900)
            self.log(out[-600:] if out else "(无输出)")
            if rc != 0:
                return False, ("Docker 安装失败。离线部署请把 docker-ce 相关的 .deb 放进 "
                               "payload/docker/ 后重试。")

        # 起 systemd 拉起 docker（wsl.conf 里已开 systemd 时 service 可用）
        C.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--", "sh", "-c",
               "systemctl enable --now docker || service docker start || true"], timeout=180)
        ok, ver = C.docker_in_wsl(DISTRO)
        if not ok:
            return False, "Docker 装完了但引擎没起来，请在 WSL 里看 journalctl -u docker。"
        self.log("Docker Engine：%s" % ver)
        return True, "Docker Engine %s" % ver

    # -- 4 载入镜像 -------------------------------------------------------
    def step_images(self) -> Tuple[bool, str]:
        img_dir = C.payload_dir() / "images"
        tars = sorted(img_dir.glob("*.tar")) if img_dir.is_dir() else []
        if not tars:
            if self.online():
                # 联网模式：不载镜像，改由第 07 步 compose build 现场构建。
                # 这一步"跳过"是预期行为，不是失败 —— 界面上标 ok 但日志写清楚。
                self.log("联网模式：跳过镜像载入，第 07 步会拉基础镜像并构建。")
                return True, "已跳过（联网构建）。"
            return False, "没有镜像包，无法离线起栈。要么补齐 payload/images，要么选联网安装。"
        total = 0
        for tar in tars:
            self.log("载入 %s …" % tar.name)
            wsl_path = _win_to_wsl(str(tar))
            rc, out = C.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--",
                             "docker", "load", "-i", wsl_path], timeout=1800)
            if rc != 0:
                self.log(out[-500:] if out else "")
                return False, "载入 %s 失败。" % tar.name
            total += 1
        return True, "已载入 %d 个镜像包。" % total

    # -- 5 落盘 -----------------------------------------------------------
    def step_files(self) -> Tuple[bool, str]:
        """把 payload/compose 整棵树落到安装目录。

        为什么整棵复制而不是挑几个文件：compose 的 build.context 是 `.`，
        Dockerfile 按 `docker/…`、`src/web/…` 相对路径取文件，caddy 又要
        `./caddy/Caddyfile`。逐个挑文件很容易漏（本次就漏了 caddy 和整棵源码树），
        而漏一个的表现是容器起不来或 build 报找不到文件 —— 都不是"能跑但难看"。
        """
        src = C.payload_dir() / "compose"
        dst = self.install_dir / "deploy"
        if not src.is_dir():
            return False, "缺少 payload/compose 目录。"
        dst.mkdir(parents=True, exist_ok=True)
        import shutil
        n = 0
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            (dst / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copy2(Path(root) / f, dst / rel / f)
                n += 1
        self.log("已复制 %d 个部署文件 → %s" % (n, dst))
        if not (dst / "caddy" / "Caddyfile").is_file():
            return False, "caddy/Caddyfile 没落盘（平台 80 端口会不通）。"

        env = dst / ".env"
        if env.is_file():
            problems = C.check_env_file(env)
            refresh_missing = not C.read_env(env).get("LS_REFRESH_TOKEN")
            if not problems:
                # 已有完整配置：保留密钥，但把公开地址刷成本次检测到的 IP。
                # 换 IP（换机房/换网段）后不刷新，员工点开的就是旧地址。
                C.write_env_key(env, "PLATFORM_PUBLIC_URL", "http://%s" % self.host_ip)
                C.write_env_key(env, "LS_PUBLIC_URL",
                                "http://%s:%d" % (self.host_ip, C.DEFAULT_LS_PORT))
                self.log("已存在完整 .env：保留原密钥，只刷新访问地址为 %s" % self.host_ip)
                if refresh_missing:
                    self.log("LS_REFRESH_TOKEN 仍为空，第 09 步会自动补齐。")
                C.save_config({**C.load_config(), "install_dir": str(self.install_dir),
                               "host_ip": self.host_ip})
                return True, "沿用现有配置（地址已刷新）。"
            self.log("已有 .env 有 %d 处问题，重新生成：%s"
                     % (len(problems), "; ".join(problems[:4])))

        vals = C.gen_env(env, self.host_ip)
        self.log("生成 .env → %s" % env)
        self.log("  访问地址 PLATFORM_PUBLIC_URL = %s" % vals["PLATFORM_PUBLIC_URL"])
        self.log("  标注引擎 LS_PUBLIC_URL     = %s" % vals["LS_PUBLIC_URL"])
        self.log("  LS 管理员 %s / 密码 %s（首次登录标注引擎用，请记下）"
                 % (vals["LS_ADMIN_EMAIL"], vals["LS_ADMIN_PASSWORD"]))
        C.save_config({**C.load_config(), "install_dir": str(self.install_dir),
                       "host_ip": self.host_ip,
                       "ls_admin_email": vals["LS_ADMIN_EMAIL"]})
        return True, "部署文件与随机密钥已写入 %s" % dst

    # -- 6 起栈 -----------------------------------------------------------
    def step_up(self) -> Tuple[bool, str]:
        remote = REMOTE_DIR
        self.log("同步部署目录到 WSL:%s …" % remote)
        rc, out = _sync_to_wsl(self.install_dir / "deploy", remote, DISTRO)
        if rc != 0:
            self.log(out[-600:] if out else "")
            return False, "部署目录同步失败。"
        online = self.online()
        args = ["up", "-d", "--remove-orphans"]
        if not online:
            # 离线时必须 --no-build：build 会去拉基础镜像/pip 包而失败，而且
            # 失败点很深（3 分钟后 pip timeout）。镜像已在第 05 步 load 进来，
            # 只允许用现有镜像 —— 缺失就立刻报错，不假装在装。
            args.append("--no-build")
        self.log("docker compose %s（%s，首次启动要建库，可能 3-10 分钟）"
                 % (" ".join(args), "联网构建" if online else "离线镜像"))
        rc, out = C.compose(DISTRO, remote, args,
                            timeout=2400 if online else 1800)
        self.log(out[-1500:] if out else "(无输出)")
        if rc != 0:
            return False, "compose 启动失败（见日志）。"
        return True, "容器已拉起。"

    # -- 7 健康 -----------------------------------------------------------
    def step_health(self) -> Tuple[bool, str]:
        deadline = time.time() + 300
        last: List[Dict[str, object]] = []
        while time.time() < deadline:
            last = C.stack_health()
            if all(h["ok"] for h in last):
                for h in last:
                    self.log("  %s :%d OK (%dms)" % (h["name"], h["port"], h["ms"]))
                return True, "三个入口全部就绪。"
            time.sleep(4)
        for h in last:
            self.log("  %s :%d %s" % (h["name"], h["port"],
                                      "OK" if h["ok"] else "不可达 code=%s" % h["code"]))
        return False, ("服务在 5 分钟内没有全部就绪。看 monitor 的容器表与日志；"
                       "常见原因：80 端口被 IIS/其他站点占用，或 .env 里公开地址填错。")

    # -- 8 端口代理 / 防火墙 ----------------------------------------------
    def step_net(self) -> Tuple[bool, str]:
        ok, ip = _wsl_ip(DISTRO)
        if not ok:
            return False, "拿不到 WSL 的 IP，无法做端口映射。"
        self.log("WSL IP = %s" % ip)
        done = 0
        names = {p: n for n, p, _ in C.STACK_PORTS}
        for port in C.HOST_PORTS:
            rc, out = C.run(["netsh", "interface", "portproxy", "add", "v4tov4",
                             "listenport=%d" % port, "listenaddress=0.0.0.0",
                             "connectport=%d" % port, "connectaddress=%s" % ip],
                            timeout=30)
            if rc != 0:
                self.log("端口 %d 映射失败：%s" % (port, out[:200]))
                continue
            C.run(["netsh", "advfirewall", "firewall", "add", "rule",
                   "name=Zhitu %s %d" % (names.get(port, "port"), port),
                   "dir=in", "action=allow",
                   "protocol=TCP", "localport=%d" % port], timeout=30)
            done += 1
        if done == 0:
            return False, "端口映射一条都没成功（需要管理员权限）。"
        self.log("注意：WSL 的 IP 每次重启都会变，所以每次开机都要重跑这一步。"
                 "监控台的「修复网络」按钮就是干这个的。")
        return True, "已映射 %d 个端口。" % done

    # -- 9 LS 长期凭证 -----------------------------------------------------
    def step_token(self) -> Tuple[bool, str]:
        """登录 LS 抓 refresh token 写回 .env，然后重启编排层。

        为什么必须有这一步：LS 1.23 的 access token 只有 5 分钟，编排层靠
        refresh token 续。留空时**服务照样全绿、网页照样能开**，但标注员
        在 LS 里标完之后，平台的数字不会动 —— 只能靠定时对账慢慢追，而且
        日志里没有一条明确的错。这是整套部署里最难自己查出来的坑。
        """
        env = self.install_dir / "deploy" / ".env"
        cur = C.read_env(env)
        email = cur.get("LS_ADMIN_EMAIL") or "admin@zhitu.local"
        password = cur.get("LS_ADMIN_PASSWORD") or ""

        if cur.get("LS_REFRESH_TOKEN"):
            self.log("LS_REFRESH_TOKEN 已存在，跳过（如需更换请清空该项后重跑本步）。")
            return True, "沿用现有 LS 凭证。"

        if not password:
            return False, ".env 里没有 LS_ADMIN_PASSWORD，无法自动登录 LS。"

        ls_local = "http://127.0.0.1:%d" % C.DEFAULT_LS_PORT
        self.log("登录 LS（%s）取 refresh token，账号 %s …" % (ls_local, email))
        ok, result = C.fetch_ls_refresh(ls_local, email, password)
        if not ok:
            self.log("[失败] " + result)
            return False, ("拿不到 LS 凭证：%s。可稍后在监控台点「补凭证」，"
                           "或手工登录 %s 生成后填入 .env 的 LS_REFRESH_TOKEN。"
                           % (result, C.ls_url(self.host_ip)))

        C.write_env_key(env, "LS_REFRESH_TOKEN", result)
        self.log("已写入 LS_REFRESH_TOKEN（%s…，共 %d 字符）"
                 % (result[:8], len(result)))

        # .env 变了，必须重新同步进 WSL 再重启编排层 —— 只在 Windows 侧改文件，
        # 容器里读的还是旧值，表现是"改了没生效"。
        rc, out = _sync_to_wsl(self.install_dir / "deploy", REMOTE_DIR, DISTRO)
        if rc != 0:
            self.log(out[-400:] if out else "")
            return False, "部署目录重新同步失败，凭证没进容器。"
        rc, out = C.compose(DISTRO, REMOTE_DIR, ["up", "-d", "orchestrator"], timeout=300)
        self.log(out[-500:] if out else "(无输出)")
        if rc != 0:
            return False, "编排层重启失败，凭证可能未生效。"

        # 等编排层起来再确认一次（它要重新连库、读环境变量）
        for _ in range(20):
            if C.probe_http("http://127.0.0.1/healthz")["ok"]:
                return True, "LS 凭证已生效，编排层已重启。"
            time.sleep(2)
        return False, "凭证已写入，但编排层 40 秒内没恢复响应，请到监控台看日志。"

    # -- 11 快捷方式与收尾 ------------------------------------------------
    def step_finish(self) -> Tuple[bool, str]:
        mon = C.app_dir() / "智途监控.exe"
        made = []
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        # 两组快捷方式：桌面给运维（监控台），开始菜单/桌面给员工（平台网址）。
        # 网址用 host_ip 而不是 127.0.0.1 —— 员工在别的机器上点，127 指自己。
        www = C.platform_url(self.host_ip)
        for name, target in [("智途监控", mon), ("智途平台", None)]:
            lnk = desktop / (name + ".lnk")
            try:
                if target and target.is_file():
                    _make_shortcut(lnk, str(target), "", str(target.parent))
                    made.append(name)
                elif target is None:
                    _make_shortcut(lnk, www, "", "")
                    made.append(name)
            except Exception as exc:  # noqa: BLE001
                self.log("快捷方式 %s 创建失败：%s" % (name, exc))
        if not made:
            return False, "快捷方式没建成（不影响使用，可手动打开 %s）。" % www
        return True, "已创建快捷方式：%s（平台入口 %s）" % ("、".join(made), www)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _win_to_wsl(win_path: str) -> str:
    """C:\\a\\b → /mnt/c/a/b。WSL 里认这个路径，直接给盘符路径不认。"""
    p = win_path.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p


def _sync_to_wsl(local_dir: Path, remote_dir: str, distro: str) -> Tuple[int, str]:
    """把部署目录送进 WSL。

    走 tar 管道而不是拷到 \\\\wsl$\\：UNC 路径在部分 Server 版本 + 组策略下
    不可用，而 tar 管道只要 wsl.exe 能用就一定成功。
    """
    C.run(["wsl.exe", "-d", distro, "-u", "root", "--",
           "mkdir", "-p", remote_dir], timeout=60)
    tar_cmd = ["tar", "-cf", "-", "-C", str(local_dir), "."]
    try:
        p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(
            ["wsl.exe", "-d", distro, "-u", "root", "--", "tar", "-xf", "-", "-C", remote_dir],
            stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p1.stdout:
            p1.stdout.close()
        out2, err2 = p2.communicate(timeout=300)
        err1 = p1.stderr.read() if p1.stderr else b""
        p1.wait(timeout=30)
        text = (err1 + err2).decode("utf-8", errors="replace")
        return p2.returncode, text.strip() or "同步完成"
    except FileNotFoundError:
        # 没有 tar（极少见）：退回逐文件写 \\wsl$ 路径
        return 1, "系统没有 tar 命令，无法同步部署目录。"
    except subprocess.TimeoutExpired:
        return 9998, "同步超时。"


def _wsl_ip(distro: str) -> Tuple[bool, str]:
    for args in (["hostname", "-I"], ["sh", "-c", "ip -4 addr show eth0 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}'"]):
        rc, out = C.run(["wsl.exe", "-d", distro, "-u", "root", "--"] + args, timeout=30)
        if rc != 0:
            continue
        for token in out.replace("\n", " ").split():
            token = token.strip()
            if token.count(".") == 3 and not token.startswith(("127.", "172.17.")):
                return True, token
    return False, ""


def _make_shortcut(lnk: Path, target: str, args: str, workdir: str) -> None:
    ps = ("$s=(New-Object -COM WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';$s.Save()"
          % (lnk, target, args, workdir or str(Path(target).parent)))
    C.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=60)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class SetupApp:
    def __init__(self) -> None:
        self.s = C.setup_dpi()
        self.root = tk.Tk()
        self.root.title("智途 · 安装程序")
        self.root.configure(bg=BG)
        self.root.geometry("%dx%d" % (int(960 * self.s), int(660 * self.s)))
        self.root.minsize(int(880 * self.s), int(600 * self.s))

        self.q: "queue.Queue[Tuple[str, str, str]]" = queue.Queue()
        self.running = False
        self.steps: List[Step] = []

        self._build_style()
        self._build_ui()
        self._build_steps()
        self._fit_window(960, 660)
        self.root.after(120, self._drain)
        self._log("欢迎。安装前请确认：本程序所在目录旁的 payload/ 已随包提供。")
        self._log("第一步会先判定这台机器能不能跑；不能跑会直接说明原因，不会装一半。")

    def _fit_window(self, w: int, h: int) -> None:
        """按内容实际需求兜底放大窗口，但绝不超过屏幕工作区。

        字号随 DPI 放大后内容会变高（150% 时实测需求 1172px），写死高度会
        把底栏的「开始安装」裁到屏幕外；反过来把窗口顶到比屏幕还大也一样。
        """
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(
            "%dx%d" % (min(max(int(w * self.s), self.root.winfo_reqwidth()), sw - self.px(24)),
                       min(max(int(h * self.s), self.root.winfo_reqheight()),
                           sh - self.px(80))))
        self.root.minsize(min(int(880 * self.s), sw - self.px(24)),
                          min(int(600 * self.s), sh - self.px(80)))

    # 样式与布局 -------------------------------------------------------
    def f(self, size: int, bold: bool = False, mono: bool = False) -> tuple:
        fam = "Consolas" if mono else "Microsoft YaHei UI"
        return (fam, max(8, int(round(size * self.s))), "bold" if bold else "normal")

    def px(self, n: int) -> int:
        return max(1, int(round(n * self.s)))

    def _build_style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                     bordercolor=LINE, lightcolor=BG3, darkcolor=BG3,
                     font=self.f(9))
        st.configure("TButton", background=BG3, foreground=FG, borderwidth=1,
                     focusthickness=0, padding=(14, 7))
        st.map("TButton",
               background=[("active", "#2C3138"), ("disabled", BG2)],
               foreground=[("disabled", FG_DIM)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#14161A",
                     font=self.f(10, bold=True))
        st.map("Accent.TButton", background=[("active", "#AD97EE"),
                                             ("disabled", "#4A4560")])
        st.configure("Treeview", background=BG2, fieldbackground=BG2,
                     foreground=FG, rowheight=self.px(30), borderwidth=0)
        st.configure("Treeview.Heading", background=BG3, foreground=FG_DIM,
                     relief="flat", font=self.f(9))
        st.map("Treeview", background=[("selected", "#2E3440")])
        # 分段按钮（替代单选）。**不要用 ttk.Radiobutton**：clam 主题下三个
        # 指示圆圈画出来几乎一样（实测 150% 缩放时只有一个灰点，分不出选中谁），
        # 而"安装方式"选错会直接决定走离线还是联网，必须一眼看出当前是哪个。
        st.configure("Seg.TButton", background=BG3, foreground=FG_DIM,
                     font=self.f(9), padding=(self.px(11), self.px(4)))
        st.map("Seg.TButton", background=[("active", "#2C3138")],
               foreground=[("active", FG)])
        st.configure("SegOn.TButton", background=ACCENT, foreground="#14161A",
                     font=self.f(9, bold=True), padding=(self.px(11), self.px(4)))
        st.map("SegOn.TButton", background=[("active", ACCENT),
                                            ("pressed", ACCENT)])

    def _build_ui(self) -> None:
        head = tk.Frame(self.root, bg=BG2, height=self.px(64))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="智途", bg=BG2, fg=FG,
                 font=self.f(15, bold=True)).pack(side="left", padx=(self.px(20), self.px(8)))
        tk.Label(head, text="·  数据生产平台 · 安装程序", bg=BG2, fg=FG_DIM,
                 font=self.f(10)).pack(side="left")
        self.lbl_admin = tk.Label(head, text="", bg=BG2, fg=WARN_C,
                                  font=self.f(9))
        self.lbl_admin.pack(side="right", padx=20)

        # — 选项条：安装方式 + 服务器访问地址 —
        # 这两个值决定第 02/05/07 步的行为和 .env 里的公开地址，必须在开始前
        # 定下来；装完再改要重跑第 06 步（监控台不提供改 IP）。
        bar = tk.Frame(self.root, bg=BG2)
        bar.pack(fill="x", pady=(1, 0))

        self.mode_var = tk.StringVar(value="auto")
        self.host_ip_var = tk.StringVar(value=C.local_ip() or "")
        tk.Label(bar, text="安装方式", bg=BG2, fg=FG_DIM,
                 font=self.f(9)).pack(side="left", padx=(20, 8), pady=self.px(9))
        self._seg: Dict[str, ttk.Button] = {}
        for val, text in (("auto", "自动"), ("online", "联网构建"),
                          ("offline", "离线镜像")):
            b = ttk.Button(bar, text=text, style="Seg.TButton",
                           command=lambda v=val: self._set_mode(v))
            b.pack(side="left", padx=(0, self.px(6)))
            self._seg[val] = b
        self._paint_seg()

        tk.Label(bar, text="服务器地址", bg=BG2, fg=FG_DIM,
                 font=self.f(9)).pack(side="left", padx=(self.px(18), 8))
        ent = tk.Entry(bar, textvariable=self.host_ip_var, width=16,
                       bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                       font=self.f(9, mono=True))
        ent.pack(side="left", ipady=self.px(3), padx=(0, self.px(8)))
        self.lbl_mode = tk.Label(bar, text="", bg=BG2, fg=FG_DIM, font=self.f(9))
        self.lbl_mode.pack(side="left")
        # 输入框改了要跟着刷新"访问 http://x.x.x.x/"这句 —— 装之前用户就得
        # 确认这个地址是员工真的能打开的那个（多网卡机器会自动猜错）。
        self.host_ip_var.trace_add("write", lambda *_: self._sync_mode_hint(True))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=self.px(16), pady=self.px(14))

        left = tk.Frame(body, bg=BG, width=self.px(330))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="安装步骤", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(anchor="w", pady=(0, self.px(6)))
        self.tree = ttk.Treeview(left, columns=("s",), show="tree",
                                 selectmode="none", height=12)
        self.tree.column("#0", width=self.px(300))
        self.tree.pack(fill="both", expand=True)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(self.px(16), 0))
        tk.Label(right, text="日志", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(anchor="w", pady=(0, self.px(6)))
        wrap = tk.Frame(right, bg=LINE, bd=0)
        wrap.pack(fill="both", expand=True)
        # width=1：Text 默认按 80 字符算请求宽度（约 1040px），会把整个窗口
        # 顶到 1200+ 逻辑像素，1366 宽的笔记本上就没法用了。真正宽度交给
        # pack(fill="both", expand=True) 决定。
        self.logbox = tk.Text(wrap, bg="#0F1113", fg="#C9CFD6", bd=0,
                              insertbackground=FG, wrap="word", width=1,
                              font=self.f(9, mono=True), padx=self.px(10),
                              pady=self.px(8))
        sb = ttk.Scrollbar(wrap, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)

        foot = tk.Frame(self.root, bg=BG2, height=self.px(64))
        # side="bottom" + before=body：pack 按打包顺序分配空间，后 pack 的先被
        # 压成 0。底栏必须比可伸缩的日志区先占位，否则窗口一矮按钮就消失。
        foot.pack(side="bottom", fill="x", before=body)
        foot.pack_propagate(False)
        self.lbl_dir = tk.Label(foot, text="安装到：" + C.DEFAULT_INSTALL_DIR,
                                bg=BG2, fg=FG_DIM, font=self.f(9))
        self.lbl_dir.pack(side="left", padx=(self.px(20), 4))
        self.btn_dir = ttk.Button(foot, text="更改", width=6, command=self._pick_dir)
        self.btn_dir.pack(side="left")
        self.lbl_url = tk.Label(foot, text="", bg=BG2, fg=ACCENT, font=self.f(9))
        self.lbl_url.pack(side="left", padx=(self.px(16), 0))
        self.btn_open = ttk.Button(foot, text="打开平台", command=self._open_platform)
        self.btn_open.pack(side="right", padx=(0, 16))
        self.btn_run = ttk.Button(foot, text="开始安装", style="Accent.TButton",
                                  command=self._start)
        self.btn_run.pack(side="right", padx=8)

        self.install_dir = C.DEFAULT_INSTALL_DIR

    def _build_steps(self) -> None:
        inst = Installer(self._log, self.install_dir,
                         host_ip=self.host_ip_var.get().strip(),
                         online=self._online_pref())
        self.inst = inst
        self.steps = [
            Step("host", "01  环境体检", inst.step_host),
            Step("payload", "02  载荷与安装方式", inst.step_payload),
            Step("wsl", "03  WSL 2 与发行版", inst.step_wsl),
            Step("docker", "04  Docker Engine（WSL 内）", inst.step_docker),
            Step("images", "05  镜像（离线载入）", inst.step_images),
            Step("files", "06  部署文件与随机密钥", inst.step_files),
            Step("up", "07  启动容器栈", inst.step_up),
            Step("health", "08  服务健康检查", inst.step_health),
            Step("net", "09  端口映射与防火墙", inst.step_net),
            Step("token", "10  LS 长期凭证", inst.step_token),
            Step("finish", "11  快捷方式与收尾", inst.step_finish),
        ]
        for s in self.steps:
            self.tree.insert("", "end", iid=s.key, text="○  " + s.title)
        self.lbl_admin.config(
            text="已以管理员运行" if C.is_admin() else "未提权：安装需要管理员")
        self._sync_mode_hint()

    # -- 交互 -------------------------------------------------------------
    def _set_mode(self, val: str) -> None:
        self.mode_var.set(val)
        self._paint_seg()
        self._sync_mode_hint()

    def _paint_seg(self) -> None:
        cur = self.mode_var.get()
        for val, b in getattr(self, "_seg", {}).items():
            b.configure(style="SegOn.TButton" if val == cur else "Seg.TButton")

    def _online_pref(self) -> Optional[bool]:
        """把界面上的三态换成 Installer 要的 None / True / False。

        "自动" = 有镜像包就走离线（快且不依赖外网），没有才联网构建。
        不默认选"自动"以外的值，是因为大多数机器拿到的是同一个交付目录。
        """
        v = self.mode_var.get()
        if v == "online":
            return True
        if v == "offline":
            return False
        return None

    def _sync_mode_hint(self, light: bool = False) -> None:
        # light=True 是给输入框逐字符触发的：只重排提示文字，不碰磁盘
        # （validate_payload 要读 manifest + glob 目录，按键一次跑一次太浪费）。
        if not light or not hasattr(self, "_has_img"):
            try:
                rep = C.validate_payload()
                self._has_img = bool(rep["images"])
                self._n_img = len(rep["images"])
            except Exception:  # noqa: BLE001 - 提示条不能因为读目录失败就抛异常
                self._has_img, self._n_img = False, 0
        has_img = getattr(self, "_has_img", False)
        n_img = getattr(self, "_n_img", 0)
        ip = self.host_ip_var.get().strip()
        pref = self._online_pref()
        online = (not has_img) if pref is None else pref

        if pref is None:
            text = ("有 %d 个镜像包 → 离线载入" % n_img) if has_img else "无镜像包 → 联网构建"
        elif online:
            text = "强制联网构建（首装 20-40 分钟）"
        else:
            text = "强制离线载入"
        if online is False and not has_img:
            text += "　⚠ payload\\images 为空"
        if getattr(self, "lbl_mode", None) is not None:
            self.lbl_mode.config(text=text)
        # 访问地址单独放底栏：塞进这一行会把窗口顶到 1800+ 像素宽（实测），
        # 在 1366 宽的笔记本上直接超出屏幕，右边一列被切掉。
        if getattr(self, "lbl_url", None) is not None:
            self.lbl_url.config(text=("员工访问 http://%s/" % ip) if ip
                                else "未识别到局域网地址，请手工填写")
        # 选项改完立刻同步给 Installer：_build_steps 只在启动时跑一次，
        # 不同步的话界面上选了"联网"、实际仍按构造时的判断走。
        if getattr(self, "inst", None) is not None:
            self.inst._online_pref = pref
            self.inst.host_ip = ip or "127.0.0.1"

    def _log(self, msg: str) -> None:
        self.q.put(("log", msg, ""))

    def _drain(self) -> None:
        """把工作线程的消息搬到界面上。

        tkinter 不是线程安全的：从子线程直接改控件在 Windows 上会随机崩，
        所以后台只往队列里写，主线程 after() 轮询消费。
        """
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", a + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "step":
                    self._paint_step(a, b)
                elif kind == "done":
                    self.running = False
                    self.btn_run.configure(state="normal", text="重新运行")
                    self.btn_dir.configure(state="normal")
                    messagebox.showinfo("安装结束", a)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _paint_step(self, key: str, state: str) -> None:
        s = next((x for x in self.steps if x.key == key), None)
        if s is None:
            return
        s.state = state
        icon = {"pending": "○", "running": "◐", "ok": "●", "fail": "✕",
                "skip": "–"}.get(state, "○")
        self.tree.item(key, text="%s  %s" % (icon, s.title))

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择安装目录")
        if d:
            self.install_dir = d
            self.inst.install_dir = Path(d)
            self.lbl_dir.config(text="安装到：" + d)

    def _open_platform(self) -> None:
        os.startfile(C.platform_url(self.host_ip_var.get().strip()))  # noqa: S606

    def _start(self) -> None:
        if self.running:
            return
        if not C.is_admin():
            if messagebox.askyesno("需要管理员权限",
                                   "安装要写系统目录、注册端口映射与防火墙规则，\n"
                                   "需要管理员权限。现在以管理员身份重新启动吗？"):
                if C.elevate_self():
                    self.root.destroy()
                    return
                messagebox.showwarning("未能提权", "提权被取消，安装无法继续。")
            return
        self.running = True
        self.btn_run.configure(state="disabled", text="安装中…")
        self.btn_dir.configure(state="disabled")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        fatal = None
        for s in self.steps:
            self.q.put(("step", s.key, "running"))
            self._log("")
            self._log("== %s ==" % s.title)
            try:
                ok, msg = s.fn()
            except Exception as exc:  # noqa: BLE001 - 单步异常不能掀翻整个向导
                self._log(traceback.format_exc()[-1200:])
                ok, msg = False, "这一步抛异常：%s" % exc
            self._log(("  → " if ok else "  ✗ ") + msg)
            self.q.put(("step", s.key, "ok" if ok else "fail"))
            if not ok:
                fatal = (s, msg)
                break
        if fatal is None:
            ip = self.host_ip_var.get().strip() or "127.0.0.1"
            self.q.put(("done", "安装完成。\n\n"
                                "平台地址：http://%s/     ← 员工用这个\n"
                                "标注引擎：http://%s:%d/\n\n"
                                "请用「智途监控」日常起停与看日志。"
                        % (ip, ip, C.DEFAULT_LS_PORT), ""))
        else:
            s, msg = fatal
            self.q.put(("done", "在「%s」这一步停下：\n\n%s\n\n"
                                "日志里有具体原因；修好后可以重新运行，已完成的步骤会自动跳过。"
                        % (s.title, msg), ""))

    def run(self) -> None:
        self.root.mainloop()


def auto_run(argv: List[str]) -> int:
    """无界面按序执行（运维排障 / 计划任务用）。

    输出同时打到控制台与 %LOCALAPPDATA%\\Zhitu\\logs\\cli-auto-<日期>.log ——
    交付版是 --noconsole 的 exe，没有这一份日志就等于"跑了但什么都没留下"。
    """
    out = C.cli_sink("auto")
    out("智途安装程序 · 无人值守模式 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("日志：%s" % out.path)

    online: Optional[bool] = None
    if "--online" in argv:
        online = True
    elif "--offline" in argv:
        online = False
    host_ip = ""
    for i, a in enumerate(argv):
        if a == "--host-ip" and i + 1 < len(argv):
            host_ip = argv[i + 1]

    inst = Installer(out, host_ip=host_ip, online=online)
    inst.install_dir = Path(C.DEFAULT_INSTALL_DIR)
    steps = [
        ("环境体检", inst.step_host),
        ("载荷与安装方式", inst.step_payload),
        ("WSL 2 与发行版", inst.step_wsl),
        ("Docker Engine", inst.step_docker),
        ("镜像（离线载入）", inst.step_images),
        ("部署文件与随机密钥", inst.step_files),
        ("启动容器栈", inst.step_up),
        ("服务健康检查", inst.step_health),
        ("端口映射与防火墙", inst.step_net),
        ("LS 长期凭证", inst.step_token),
        ("快捷方式与收尾", inst.step_finish),
    ]
    for title, fn in steps:
        out("")
        out("== %s ==" % title)
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001
            out(traceback.format_exc()[-1200:])
            ok, msg = False, "异常：%s" % exc
        out(("  → " if ok else "  ✗ ") + msg)
        if not ok:
            out("")
            out("安装中断。修好后重跑即可，已完成步骤会跳过。")
            return 1
    out("")
    out("安装完成：%s" % C.platform_url(inst.host_ip))
    out("标注引擎：%s" % C.ls_url(inst.host_ip))
    return 0


def main() -> int:
    C.fix_console()
    if "--selftest" in sys.argv:
        return C.selftest()
    if "--auto" in sys.argv:
        return auto_run(sys.argv[1:])
    SetupApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
