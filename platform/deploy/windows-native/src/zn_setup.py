#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 · 原生 Windows 安装程序（不依赖 Docker / WSL / 容器）。

为什么是"原生"而不是容器
----------------------------------------------------------------------------
容器方案的镜像是 Linux 的：Windows 上要么开 WSL 2（要求嵌套虚拟化，云主机
常常没有），要么用 Docker Desktop（Windows Server 不支持）。所以服务器就是
一台 Windows 时，每个组件都用原生 Windows 版最稳：

    PostgreSQL for Windows   数据库
    Python venv + pip        编排层（本项目自己的 FastAPI 服务）
    Python venv + pip        Label Studio（官方 pip 包，Django 应用）
    caddy.exe for Windows    统一入口（静态文件 + /api 反代）
    计划任务                 开机自启 + 崩溃 1 分钟内拉起

13 个步骤，每步都可单独重跑（已完成的会跳过）。全部动作都在 Windows 进程层，
不启动任何 Linux 虚拟机。

用法
----------------------------------------------------------------------------
    智途安装程序.exe                     打开向导（推荐）
    智途安装程序.exe --auto              无界面按序执行
    智途安装程序.exe --auto --no-download  只用 payload/cache 里的安装包（断网安装）
    智途安装程序.exe --selftest          只体检，不动系统
"""
from __future__ import annotations

import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zn_common as C  # noqa: E402

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

LS_VERSION = "1.23.2"
PG_SERVICE = C.TASKS["pg"]


def _lit(text: str) -> str:
    """把字符串包成 SQL 字面量（单引号翻倍）。

    口令是随机生成的，可能带 `'` 或 `\\`；直接拼进 SQL 会语法错甚至注入。
    """
    return "'" + str(text).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
class Step:
    def __init__(self, key: str, title: str, fn: Callable):
        self.key = key
        self.title = title
        self.fn = fn
        self.state = "pending"


# 步骤表只有这一份。GUI 与 --auto 都从这里取，改一处就不会两边不一致 ——
# 容器路线里 GUI 和无界面各写一份，已经漂移过一次（GUI 11 步、auto 还是 10 步）。
STEP_SPEC = (
    ("host", "01  环境体检"),
    ("python", "02  Python 3.11-3.13"),
    ("postgres", "03  PostgreSQL"),
    ("orchestrator", "04  编排层依赖"),
    ("database", "05  建库与迁移"),
    ("labelstudio", "06  标注引擎 LS"),
    ("brand", "07  品牌与单点登录"),
    ("config", "08  配置与管理员"),
    ("web", "09  前端与入口"),
    ("tasks", "10  启动平台进程"),
    ("health", "11  健康检查"),
    ("token", "12  LS 长期凭证"),
    ("finish", "13  收尾"),
)


def step_list(inst: "Installer") -> List[Step]:
    return [Step(key, title, getattr(inst, "step_" + key)) for key, title in STEP_SPEC]


# 步骤依赖：某步失败后，依赖它的步骤直接标 SKIP。
# 不标的话同一个根因会在后面每一步重复报一遍，真正的问题反而被淹没。
NEEDS = {
    "postgres": ("python",),
    "orchestrator": ("python",),
    "database": ("postgres", "orchestrator"),
    "labelstudio": ("python",),
    "brand": ("labelstudio", "database"),
    "config": ("database", "orchestrator"),
    "web": (),
    "tasks": ("postgres", "labelstudio", "web"),
    "health": ("tasks",),
    "token": ("labelstudio", "config"),
    "finish": (),
}

OK, FAIL, SKIP = "ok", "fail", "skip"


def run_steps(steps: List[Step], log: Callable[[str], None],
              paint: Callable[[str, str], None] | None = None
              ) -> List[Tuple[str, str, str, str]]:
    """按序跑，**失败不中断**，返回 [(key, 标题, 状态, 说明)]。

    为什么不停：一步失败就 return，等于让人每跑一次只帮我们验证一个点。
    13 步能各自独立失败，来回十几轮谁也受不了。跑完为止，把「哪一步、什么原因」
    一次性全拿到，一次修完。
    """
    titles = {s.key: s.title for s in steps}
    done: Dict[str, str] = {}
    results: List[Tuple[str, str, str, str]] = []
    for s in steps:
        # SKIP 也要往下传：postgres 被跳过却继续去建库，只会多报一个假错误
        blocked = [k for k in NEEDS.get(s.key, ()) if done.get(k) in (FAIL, SKIP)]
        log("")
        log("== %s ==" % s.title)
        if blocked:
            msg = "跳过：前置步骤失败（%s）" % "、".join(titles.get(k, k) for k in blocked)
            log("  - " + msg)
            if paint:
                paint(s.key, SKIP)
            done[s.key] = SKIP
            results.append((s.key, s.title, SKIP, msg))
            continue
        if paint:
            paint(s.key, "running")
        try:
            ok, msg = s.fn()
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc()[-1200:])
            ok, msg = False, "这一步抛异常：%s" % exc
        msg = str(msg)
        log(("  → " if ok else "  ✗ ") + msg)
        if paint:
            paint(s.key, OK if ok else FAIL)
        done[s.key] = OK if ok else FAIL
        results.append((s.key, s.title, OK if ok else FAIL, msg))
    return results


def format_summary(results: List[Tuple[str, str, str, str]]) -> str:
    """13 步结果总表。失败项就是这次的修复清单。"""
    tag = {OK: "[通过]", FAIL: "[失败]", SKIP: "[跳过]"}
    lines = ["=" * 66, " 结果汇总", "=" * 66]
    for _key, title, status, msg in results:
        lines.append("%s %s" % (tag.get(status, status), title))
        if status != OK:
            for ln in (msg or "").splitlines()[:6]:
                lines.append("        " + ln.strip())
    n_ok = sum(1 for r in results if r[2] == OK)
    n_bad = sum(1 for r in results if r[2] == FAIL)
    n_skip = sum(1 for r in results if r[2] == SKIP)
    lines.append("")
    lines.append("通过 %d / 失败 %d / 跳过 %d（共 %d 步）"
                 % (n_ok, n_bad, n_skip, len(results)))
    return "\n".join(lines)


def build_report(inst: "Installer", results: List[Tuple[str, str, str, str]]) -> str:
    """完整报告：环境 + 每步全文（不截断），直接发出来就能定位问题。"""
    lines = ["智途 Windows 原生安装 · 报告",
             "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "管理员：%s" % ("是" if C.is_admin() else "否"),
             "系统：%s" % platform.platform(),
             "安装目录：%s" % inst.app_dir,
             "数据目录：%s" % inst.data_dir,
             "主机 IP：%s" % inst.host_ip,
             ""]
    lines.append(format_summary(results))
    lines.append("")
    lines.append("=" * 66)
    lines.append(" 每一步的完整说明")
    lines.append("=" * 66)
    for _key, title, status, msg in results:
        lines.append("")
        lines.append("[%s] %s" % (status, title))
        lines.append(msg or "（无输出）")
    return "\n".join(lines) + "\n"


def write_report(inst: "Installer", results: List[Tuple[str, str, str, str]],
                 near: str = "") -> Path:
    """报告写到日志旁边（和 install-gui/auto 日志同目录），返回路径。"""
    base = Path(near).parent if near else C.logs_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir())
    p = base / ("install-report-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        p.write_text(build_report(inst, results), encoding="utf-8")
    except OSError:
        return Path("")
    return p


class Installer:
    """原生安装流程。每个方法返回 (ok, 说明)。"""

    def __init__(self, log: Callable[[str], None],
                 app_dir: str = C.DEFAULT_APP_DIR,
                 data_dir: str = C.DEFAULT_DATA_DIR,
                 host_ip: str = "", allow_download: bool = True,
                 admin_phone: str = "", admin_name: str = ""):
        self.log = log
        self.app_dir = Path(app_dir)
        self.data_dir = Path(data_dir)
        self.host_ip = (host_ip or "").strip() or C.local_ip() or "127.0.0.1"
        self.allow_download = allow_download
        self.admin_phone = admin_phone.strip()
        self.admin_name = admin_name.strip() or "管理员"
        self.cfg: Dict[str, str] = {}
        self.secrets: Dict[str, str] = {}
        self.pg_port = C.DEFAULT_PG_PORT
        self.py = ""
        self.py_ver = ""
        self.report: Dict[str, object] = {}

    # 路径 ---------------------------------------------------------------
    @property
    def pg_root(self) -> Path:
        return self.app_dir / "pgsql"

    @property
    def orch_dir(self) -> Path:
        return self.app_dir / "services" / "orchestrator"

    @property
    def venv_orch(self) -> Path:
        return self.app_dir / "venv"

    @property
    def venv_ls(self) -> Path:
        return self.app_dir / "lsvenv"

    @property
    def web_root(self) -> Path:
        return self.app_dir / "web" / "dist"

    @property
    def env_file(self) -> Path:
        return self.data_dir / "config" / "forge.env"

    @property
    def pg_data(self) -> Path:
        return self.data_dir / "pgdata"

    @property
    def ls_data(self) -> Path:
        return self.data_dir / "labelstudio"

    @property
    def bin_dir(self) -> Path:
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Zhitu" / "cache"

    # -- 01 环境体检 ------------------------------------------------------
    def step_host(self) -> Tuple[bool, str]:
        build, name = C.windows_build()
        self.log("系统：%s build %d" % (name, build))
        self.log("管理员：%s" % ("是" if C.is_admin() else "否"))
        lp = getattr(self.log, "path", "")
        if lp:
            self.log("安装日志：%s" % lp)
        self.log("安装目录：%s" % self.app_dir)
        self.log("数据目录：%s" % self.data_dir)
        self.log("员工访问地址：http://%s/" % self.host_ip)
        self.report["build"] = build

        if not C.is_admin():
            return False, "安装必须用管理员身份运行（要建服务、写 ProgramData、注册计划任务）。"
        if build and build < 14393:
            return False, "这台机器的 Windows 版本太旧（build %d），需要 Windows 10/Server 2016 以上。" % build

        # 磁盘：PG + 两个 venv + LS 数据，留 5 GB 余量
        try:
            free = shutil.disk_usage(str(self.app_dir.drive + "\\")).free
            self.log("磁盘可用：%.1f GB" % (free / 2 ** 30))
            if free < 5 * 2 ** 30:
                return False, "磁盘可用空间不足 5 GB（当前 %.1f GB）。" % (free / 2 ** 30)
        except OSError:
            pass

        busy = [p for p in (C.DEFAULT_HTTP_PORT, C.DEFAULT_LS_PORT, C.DEFAULT_API_PORT)
                if C.port_open(p)]
        if busy:
            return False, ("端口被占用：%s。80 是平台入口、8100 是标注引擎、8300 是编排层，"
                           "请先停掉占用它们的程序（IIS / 其他站点）。" % busy)
        self.log("端口 80 / 8100 / 8300 均空闲")

        # 内存：Windows 自身 1.5-2 GB + PG + 两个 Python venv。4 GB 是下限。
        # 不在这里拦的话，症状会推迟到第 06 步（pip 装标注引擎）被系统杀掉，
        # 报错只是"安装失败"，看不出是内存不够 —— 现场最难判的一类。
        ram = C.total_ram_gb()
        if ram:
            self.log("物理内存：%.1f GB" % ram)
            if ram < 3.5:
                return False, ("物理内存只有 %.1f GB，装不下（至少要 4 GB，推荐 8 GB）："
                               "Windows 自身要占 1.5-2 GB，第 06 步装标注引擎会被系统杀掉。"
                               % ram)
            if ram < 7.0:
                self.log("[提示] 内存偏紧，安装会慢；建议加大虚拟内存（页面文件）到 8 GB")
        return True, "环境就绪。"

    # -- 02 Python --------------------------------------------------------
    def step_python(self) -> Tuple[bool, str]:
        # 先把源码树铺到安装目录：第 04 步（编排层）、05 步（迁移）、07 步（品牌）
        # 全部从 <安装目录> 下读文件，铺不开后面必然一步步报「缺文件」。
        # 这一步跟有没有预置运行时无关，老旧 exe+payload 形态也一样要走。
        src_staged = C.stage_sources(self.app_dir, self.log)
        if src_staged is not None:
            ok, msg = src_staged
            if not ok:
                return False, msg
            self.log(msg)

        # 离线整包：runtime/ 里已经带好 Python 与全部依赖，铺开即可，
        # 既不找系统 Python，也不联网。铺开之后后面的 PG / 依赖 / caddy
        # 每一步都会因为「已就位」自动跳过下载与 pip。
        staged = C.stage_runtime(self.app_dir, self.log)
        if staged is not None:
            ok, msg = staged
            if not ok:
                return False, msg
            self.py = str(C.venv_python(self.venv_orch))
            self.py_ver = '3.13'
            self.log('Python：%s（离线预置，无需安装）' % self.py)
            return True, msg

        # 在线安装：找系统 Python，区间限定 3.11–3.13
        self.py, self.py_ver = C.find_python((3, 11), (3, 13))
        if self.py:
            self.log("Python：%s（%s）" % (self.py, self.py_ver))
            return True, "Python %s" % self.py_ver

        self.log("没找到 3.11–3.13 的 Python，尝试用 winget 安装 3.12 …")
        rc, out = C.run(["winget.exe", "install", "-e", "--id", "Python.Python.3.12",
                         "--silent", "--accept-package-agreements",
                         "--accept-source-agreements"], timeout=900)
        self.log((out or "")[-600:])
        self.py, self.py_ver = C.find_python((3, 11), (3, 13))
        if self.py:
            return True, "已通过 winget 安装 Python %s" % self.py_ver
        return False, ("这台机器没有可用的 Python 3.11–3.13，自动安装也没成功。\n"
                       "请手动安装后重跑：%s\n（安装时务必勾选 Add python.exe to PATH）"
                       % C.PY_DOWNLOAD_HINT)

    # -- 03 PostgreSQL ----------------------------------------------------
    def _fetch_zip(self, url: str, name: str, min_mb: int) -> Tuple[bool, str]:
        """优先用预置包（exe 旁 cache\ / payload\cache\），没有再下载。"""
        for d in C.cache_dirs():
            pre = d / name
            if pre.is_file() and pre.stat().st_size > min_mb * 1048576:
                self.log("使用预置包 %s" % pre)
                return True, str(pre)
        dst = self.bin_dir / name
        if dst.is_file() and dst.stat().st_size > min_mb * 1048576:
            self.log("使用已下载的缓存 %s" % dst)
            return True, str(dst)
        if not self.allow_download:
            return False, ("没有预置 %s，且当前是「只用本地包」模式。\n"
                           "把它放进 payload\\cache\\ 后重跑，或允许联网下载。" % name)
        self.log("下载 %s …" % url)
        ok, msg = C.download(url, dst, self.log, min_bytes=min_mb * 1048576)
        self.log("  " + msg)
        return (True, str(dst)) if ok else (False, msg)

    def step_postgres(self) -> Tuple[bool, str]:
        if C.pg_installed(self.pg_root):
            self.log("PostgreSQL 已就位：%s（工具集 %s）"
                     % (self.pg_root, C.pg_toolset(self.pg_root)))
        else:
            self.log("准备 PostgreSQL（官方包 330 MB，备用包 23 MB，都会先看本地缓存）…")
            ok, msg = C.pg_fetch(
                self.pg_root, self.bin_dir, self.log,
                extra_dirs=C.cache_dirs(),
                allow_download=self.allow_download)
            self.log(msg)
            if not ok:
                return False, ("拿不到 PostgreSQL：\n%s\n"
                               "可手动下载官方 zip 放到 payload\\cache\\ 后重跑。"
                               % msg)
            self.log("PostgreSQL 就位：%s（工具集 %s）"
                     % (self.pg_root, C.pg_toolset(self.pg_root)))
            if not C.pg_full_tools(self.pg_root):
                self.log("[说明] 这次用的是备用包，没有 psql / pg_dump："
                         "建库与迁移改由 Python 执行，备份走冷备。功能不受影响。")

        # 先删老版本遗留的 Windows 服务（11-09 之后不再注册服务）。
        #
        # 顺序很关键：**必须在判端口之前删**。老服务在跑的时候占着 5432 和
        # data 目录，先判端口就会把它当成"正常在跑"直接放过去，等后面
        # pg_start_local 再去起，撞上「lock file postmaster.pid already exists」，
        # 报出来的却是「5432 没监听」—— 真机上第 03 步起不来的头号原因。
        if C.service_exists(PG_SERVICE):
            self.log("发现老版本注册的服务 %s（会跟直起的实例抢端口和 data 目录），先停掉删除"
                     % PG_SERVICE)
            C.remove_service(PG_SERVICE, self.log, wait_port=C.DEFAULT_PG_PORT)

        # 端口：5432 被别人占着就顺延到 5433/5434，并把端口写进 postgresql.conf
        #
        # 判「是不是自己」不能只看 service_exists：服务存在不代表这个端口就归它，
        # 用户自己装的 PostgreSQL（服务名 postgresql-x64-17 之类）同样占 5432。
        # 判错的后果很具体 —— 自己的库被当成外人而换到 5433，配置里写 5433、
        # 实际连的是别人的 5432，报错就长成「password authentication failed」。
        self.pg_port = C.DEFAULT_PG_PORT
        if C.port_open(C.DEFAULT_PG_PORT):
            mine, why = C.pg_owns_port(self.pg_data, C.DEFAULT_PG_PORT)
            self.log("5432 正在使用：%s" % why)
            if not mine:
                for p in (5433, 5434, 5435):
                    if not C.port_open(p):
                        self.pg_port = p
                        break
                self.log("5432 上是别的库，改用 %d（已写入配置）" % self.pg_port)
                self.cfg["PG_PORT"] = str(self.pg_port)
        self.report["pg_port"] = self.pg_port

        # 口令必须跨运行复用：第 03 步生成、第 08 步才写进 .env，中间任何一步失败
        # 重跑都会把它丢掉；重跑时又生成一个新口令，拿它去连「已经存在的旧集群」
        # 就是 password authentication failed。所以生成后立刻落到 native.json。
        saved = dict(C.load_config() or {})
        superpass = (self.secrets.get("PG_SUPER")
                     or str(saved.get("pg_super") or "")
                     or C.random_secret(24))
        self.secrets["PG_SUPER"] = superpass
        if saved.get("pg_super") != superpass:
            saved["pg_super"] = superpass
            try:
                C.save_config(saved)
            except OSError as exc:
                self.log("[提示] 口令暂存失败（不影响本次安装）：%s" % exc)

        ok, msg = C.pg_initdb(self.pg_root, self.pg_data, superpass, log=self.log)
        self.log(msg)
        if not ok:
            return False, msg

        # data 目录可能是**别的 PG 主版本**建的（老安装残留）。版本不符时
        # postgres 会直接拒绝启动，要等几十秒轮询完才在日志里看到
        # "database files are incompatible" —— 这里提前拦下来，直接说怎么办。
        cv = C.pg_cluster_version(self.pg_data)
        bv = C.pg_binary_version(self.pg_root)
        if cv and bv and cv != bv:
            return False, (
                "数据目录 %s 是 PostgreSQL %s 建的，这次自带的是 %s，起不了。\n"
                "处理办法：把 %s 改名留档（例如 pgdata-old），再重新运行安装程序，"
                "它会用自带版本重新建库。里面如果已经有业务数据，先备份再动。"
                % (self.pg_data, cv, bv, self.pg_data))

        conf = self.pg_data / "postgresql.conf"
        try:
            text = conf.read_text(encoding="utf-8", errors="replace")
            if self.pg_port != C.DEFAULT_PG_PORT:
                text += "\nport = %d\nlisten_addresses = '127.0.0.1'\n" % self.pg_port
                conf.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.log("[提示] 改 postgresql.conf 失败：%s" % exc)

        # 不注册 Windows 服务。服务器本来就常开，注册服务只为「开机自启」，
        # 代价是管理员权限、组策略、SCM 恢复策略一堆约束。直接 pg_ctl 起。
        ok, msg = C.pg_start_local(self.pg_root, self.pg_data, self.pg_port,
                                   log=self.log)
        self.log(msg)
        if not ok:
            return False, msg
        for _ in range(20):
            if C.port_open(self.pg_port):
                break
            time.sleep(1)
        else:
            tail = C.pg_log_tail(self.pg_data)
            return False, ("PostgreSQL 进程起来了但 %d 端口没监听。\n%s\n\n%s"
                           % (self.pg_port,
                              C.pg_classify(tail)[1],
                              tail[-800:] or "（postgres.log 为空）"))

        # 端口通了不等于口令对：数据目录里可能是上一次留下的集群（initdb 见到
        # PG_VERSION 就不碰它），而当时的口令已经丢了。不在这里兜住，报错会拖到
        # 第 05 步，变成一句 psql 原始英文，用户根本看不出是自己库的口令问题。
        py = str(C.venv_python(self.venv_orch))
        good, out, rc = C.pg_verify_super(self.pg_root, self.pg_port, superpass, py=py)
        if good:
            return True, "PostgreSQL 在 127.0.0.1:%d 监听（口令已验证）" % self.pg_port
        if rc == 9:
            self.log("[提示] 这台机器暂时没有 psql/psycopg，跳过口令校验（第 05 步会再验）")
            return True, "PostgreSQL 在 127.0.0.1:%d 监听" % self.pg_port
        self.log("口令对不上：%s" % (out or "").strip()[-200:])
        self.log("数据目录里是已有的集群，按本次口令重置（不动里面的数据）…")
        ok2, msg2 = C.pg_reset_super_password(
            self.pg_root, self.pg_data, PG_SERVICE, superpass,
            port=self.pg_port, py=py, log=self.log)
        self.log(msg2)
        if not ok2:
            return False, (
                "连不上 %d 上的 PostgreSQL，自动重置口令也没成功：\n%s\n\n"
                "如果这个库里有你自己的数据，先备份再动手。确认可以重来，"
                "用管理员 PowerShell 把数据目录改名后重跑安装程序：\n"
                "  sc.exe stop %s\n"
                "  Rename-Item -LiteralPath \"%s\" -NewName pgdata-old"
                % (self.pg_port, msg2, PG_SERVICE, self.pg_data))
        return True, "PostgreSQL 在 127.0.0.1:%d 监听（口令已重置）" % self.pg_port

    # -- 05 建库与迁移 ----------------------------------------------------
    def _db(self, sql: str, db: str = "postgres") -> Tuple[int, str]:
        """执行一段 SQL（走 C.pg_apply：有 psql 用 psql，没有用 venv 的 psycopg）。"""
        superpass = self.secrets.get("PG_SUPER") or ""
        return C.pg_apply(self.pg_root, db, sql, superpass,
                          py=str(C.venv_python(self.venv_orch)),
                          port=self.pg_port)

    def _db_file(self, path: Path) -> Tuple[int, str]:
        superpass = self.secrets.get("PG_SUPER") or ""
        return C.pg_apply_file(self.pg_root, "forge", path, superpass,
                               py=str(C.venv_python(self.venv_orch)),
                               port=self.pg_port)

    @staticmethod
    def _hit(out: str) -> bool:
        """把「查到 1 行」判出来。

        psql 输出 `(1 row)`，我们自己的 runner 输出 `RESULT 1`，两种都要认 ——
        这个判断决定"建不建库/角色"，认错了会拿 already exists 当失败往上抛。
        """
        for line in (out or "").splitlines():
            s = line.strip()
            if s.startswith("RESULT "):
                try:
                    return int(s.split()[1]) > 0
                except (IndexError, ValueError):
                    return False
            if s in ("(1 row)", "1 row"):
                return True
        return False

    def step_database(self) -> Tuple[bool, str]:
        superpass = self.secrets.get("PG_SUPER") or ""
        if not superpass:
            return False, "缺少 PostgreSQL 超级用户口令（第 03 步没完成？）。"
        if not C.venv_python(self.venv_orch).is_file():
            return False, "编排层 venv 还没建好（第 04 步），没法执行 SQL。"

        apppass = self.secrets.get("DB_APP") or C.random_secret(16)
        self.secrets["DB_APP"] = apppass

        rc, out = self._db("SELECT 1 FROM pg_database WHERE datname='forge'")
        if not self._hit(out):
            self.log("创建数据库 forge …")
            rc, out = self._db("CREATE DATABASE forge")
            if rc != 0:
                hint = ""
                if "password authentication failed" in (out or ""):
                    hint = ("\n提示：这说明 %d 上那个库的口令和安装程序记的不一致。"
                            "重新运行安装程序会自动重置口令（不会动库里的数据）。"
                            % self.pg_port)
                return False, ("建库失败：%s%s" % (out[-400:] or rc, hint))

        # 角色与权限基线。原来是 000_roles.sql + \gexec（psql 专有），这里改成
        # 幂等 SQL：备用源没有 psql，元命令根本执行不了。
        self.log("创建/收敛 forge_app 角色 …")
        rc, out = self._db(
            "DO $do$ BEGIN "
            "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='forge_app') THEN "
            "    CREATE ROLE forge_app LOGIN PASSWORD %s; "
            "  ELSE ALTER ROLE forge_app WITH LOGIN PASSWORD %s; "
            "  END IF; "
            "END $do$; "
            "ALTER ROLE forge_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
            "ALTER ROLE forge_app CONNECTION LIMIT 60;"
            % (_lit(apppass), _lit(apppass)))
        if rc != 0:
            return False, "创建 forge_app 失败：%s" % (out[-600:] or rc)

        migs = sorted((self.app_dir / "deploy" / "migrations").glob("0*.sql"))
        migs = [m for m in migs if m.name != "000_roles.sql"]
        for m in migs:
            self.log("迁移 %s" % m.name)
            rc, out = self._db_file(m)
            if rc != 0:
                return False, "%s 失败：%s" % (m.name, out[-600:] or rc)

        # 注意：要指定库名。默认是 postgres 库，在那里数表永远是 0 —— 会误导人。
        rc, out = self._db("SELECT count(*) AS tables FROM information_schema.tables "
                           "WHERE table_schema = 'forge'", "forge")
        n_tab = ""
        for line in (out or "").splitlines():
            if line.startswith("ROW "):
                n_tab = line[4:].strip()
        self.log("库结构完成：%d 个迁移，%s 张表" % (len(migs), n_tab or "?"))

        self.cfg["DB_DSN"] = ("postgresql://forge_app:%s@127.0.0.1:%d/forge"
                              % (apppass, self.pg_port))
        return True, "库结构已就绪（%d 个迁移）。" % len(migs)

    # -- 04 编排层 --------------------------------------------------------
    def step_orchestrator(self) -> Tuple[bool, str]:
        req = self.orch_dir / "requirements.txt"
        if not req.is_file():
            return False, "载荷里缺编排层源码：%s" % req
        # 离线包把依赖预装进了运行时；服务器可能根本没网，别再 pip 一遍
        if (self.venv_orch / "Lib" / "site-packages" / "fastapi").is_dir():
            self.log('编排层依赖已在预置运行时里，跳过 pip')
            return True, "编排层运行时已就绪（离线预置）"
        ok, msg = C.venv_create(self.py, self.venv_orch)
        self.log("venv：%s" % msg)
        if not ok:
            return False, msg
        self.log("安装编排层依赖（1-3 分钟）…")
        ok, msg = C.pip_install(self.venv_orch, ["-r", str(req)], self.log)
        self.log(msg if len(msg) < 400 else msg[-400:])
        if not ok:
            return False, "编排层依赖安装失败（见日志）。"
        return True, "编排层依赖已安装。"

    # -- 06 Label Studio --------------------------------------------------
    def step_labelstudio(self) -> Tuple[bool, str]:
        # 离线包里 LS 的全套依赖已经躺在运行时中。判 site-packages 而不是
        # Scripts/label-studio.exe —— 便携运行时没有 console_scripts 的 exe。
        if (self.venv_ls / "Lib" / "site-packages" / "label_studio").is_dir():
            self.log('Label Studio %s 已在预置运行时里，跳过 pip' % LS_VERSION)
            return True, "Label Studio %s 已就绪（离线预置）" % LS_VERSION
        ok, msg = C.venv_create(self.py, self.venv_ls)
        self.log("venv：%s" % msg)
        if not ok:
            return False, msg
        exe = self.venv_ls / "Scripts" / "label-studio.exe"
        if exe.is_file():
            self.log("Label Studio 已安装，跳过 pip")
            return True, "已安装（%s）" % exe
        self.log("安装 Label Studio %s（依赖很多，10-20 分钟，请耐心等）…" % LS_VERSION)
        ok, msg = C.pip_install(self.venv_ls, ["label-studio==%s" % LS_VERSION],
                                self.log, timeout=5400)
        if not ok:
            self.log(msg[-800:])
            return False, ("Label Studio 安装失败。常见原因：pip 源不通、"
                           "Python 版本不在 3.11-3.13、或磁盘不足。")
        if not exe.is_file():
            return False, "pip 报成功但找不到 label-studio.exe，venv 可能坏了。"
        return True, "Label Studio %s 已安装。" % LS_VERSION

    # -- 07 品牌补丁与 SSO ------------------------------------------------
    def step_brand(self) -> Tuple[bool, str]:
        brand = self.app_dir / "labelstudio" / "brand" / "apply_brand.py"
        if not brand.is_file():
            return False, "载荷里缺品牌补丁：%s" % brand
        site = self.venv_ls / "Lib" / "site-packages" / "label_studio"
        if not site.is_dir():
            return False, "找不到 LS 的 site-packages：%s" % site
        self.log("给 %s 打智途品牌补丁 + 装 SSO 中间件 …" % site)
        rc, out = C.run([str(C.venv_python(self.venv_ls)), str(brand),
                         "--ls-root", str(site)], timeout=600)
        self.log((out or "")[-1200:])
        if rc != 0:
            return False, ("品牌补丁失败（rc=%s）。退出码 2 = 有补丁锚点没匹配上，"
                           "说明 LS 版本与补丁不配套。" % rc)
        return True, "品牌与 SSO 已打入 LS。"

    # -- 08 配置与首个管理员 ----------------------------------------------
    def step_config(self) -> Tuple[bool, str]:
        cfg_dir = self.data_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, str] = {}
        if self.env_file.is_file():
            for line in self.env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

        def pick(key: str, gen: Callable[[], str]) -> str:
            # 重装不换密钥：换了会把已登录的人全踢下线，也让 LS 凭证失效
            return existing.get(key) or self.secrets.get(key) or gen()

        self.secrets["JWT"] = pick("FORGE_JWT_SECRET", lambda: C.random_secret(48))
        self.secrets["SSO"] = pick("FORGE_LS_SSO_SECRET", lambda: C.random_secret(48))
        self.secrets["LS_SECRET"] = pick("LS_SECRET_KEY", lambda: C.random_secret(32))
        # PG 超级用户口令必须持久化：第 12 步、重跑、以及后续任何建库动作都要用它。
        # 只放内存的话，第二次运行就拿不到，LS 的库建不出来而报错点很远。
        # 顺序反了会出大事：.env 里可能存着上一版写下的旧口令，而本次第 03 步
        # 已经把库里的口令改成新生成的（或沿用了 native.json 里的）。必须以本次
        # 实际生效的口令为准，.env 只在单独重跑第 08 步时兜底。
        self.secrets["PG_SUPER"] = (self.secrets.get("PG_SUPER")
                                    or existing.get("PG_SUPER_PASSWORD") or "")
        sso = self.secrets["SSO"]
        if not self.secrets["PG_SUPER"]:
            self.log("[提示] 没有保留 PG 超级用户口令，创建 LS 数据库这一步会被跳过。")

        # Label Studio 用独立的库与角色：与平台库分开，配额与故障隔离
        ls_pass = existing.get("LS_POSTGRE_PASSWORD") or C.random_secret(16)
        superpass = self.secrets.get("PG_SUPER") or ""
        if superpass:
            # 幂等：角色/库已存在时 CREATE 会报错，先查再建
            rc, out = self._db("SELECT 1 FROM pg_roles WHERE rolname='ls'")
            if self._hit(out):
                self._db("ALTER ROLE ls PASSWORD %s" % _lit(ls_pass))
            else:
                self._db("CREATE ROLE ls LOGIN PASSWORD %s" % _lit(ls_pass))
            rc, out = self._db("SELECT 1 FROM pg_database WHERE datname='labelstudio'")
            if not self._hit(out):
                rc, out = self._db("CREATE DATABASE labelstudio OWNER ls")
                if rc != 0:
                    self.log("[提示] 创建 labelstudio 库失败：%s" % (out[-300:] or rc))
        self.cfg.update({
            "FORGE_AUTH_MODE": "native",
            "FORGE_HOST": "127.0.0.1",
            "FORGE_PORT": str(C.DEFAULT_API_PORT),
            "FORGE_JWT_SECRET": self.secrets["JWT"],
            "FORGE_LS_SSO_SECRET": sso,
            "FORGE_DB_DSN": self.cfg.get("DB_DSN") or existing.get("FORGE_DB_DSN", ""),
            "FORGE_LS_BASE_URL": "http://127.0.0.1:%d" % C.DEFAULT_LS_PORT,
            "FORGE_LS_PUBLIC_BASE_URL": "http://%s:%d" % (self.host_ip, C.DEFAULT_LS_PORT),
            "FORGE_CORS_ORIGINS": ("http://%s,http://%s:%d"
                                   % (self.host_ip, self.host_ip, C.DEFAULT_LS_PORT)),
            "FORGE_JWT_ISSUER": "forge-orchestrator",
            "FORGE_JWT_AUDIENCE": "forge-platform",
            "FORGE_JWT_TTL_MIN": "480",
            "FORGE_LOGIN_MAX_FAILURES": "5",
            "FORGE_LOGIN_LOCK_MIN": "15",
            "FORGE_PASSWORD_MIN_LEN": "8",
            "LS_SECRET_KEY": self.secrets["LS_SECRET"],
            "LS_POSTGRE_NAME": "labelstudio",
            "LS_POSTGRE_USER": "ls",
            "LS_POSTGRE_PASSWORD": ls_pass,
            # 校验/重装都要用；这个文件已经 icacls 收紧到 Administrators+SYSTEM
            "PG_SUPER_PASSWORD": superpass,
        })
        if not self.cfg["FORGE_DB_DSN"]:
            return False, "没有数据库连接串（第 04 步没完成？）。"

        lines = ["# 智途运行时配置 —— 由安装程序生成，含密钥，不要外传",
                 "# 重装会保留这里的密钥；换掉它们等于把所有人踢下线"]
        for k in sorted(self.cfg):
            lines.append("%s=%s" % (k, self.cfg[k]))
        self.env_file.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        self.log("配置写入 %s（%d 项）" % (self.env_file, len(self.cfg)))

        # ACL：只有管理员与 SYSTEM 能读，里面有数据库口令
        C.run(["icacls", str(self.env_file), "/inheritance:r", "/grant:r",
               "BUILTIN\\Administrators:(R,W)", "NT AUTHORITY\\SYSTEM:(R)"], timeout=60)

        # 首个管理员：没有它没人能登录，而且必须从命令行开通
        phone = self.admin_phone or existing.get("BOOTSTRAP_PHONE") or ""
        if not phone:
            self.log("未提供管理员手机号，跳过首个管理员创建")
            self.log("稍后手动执行：cd %s && %s -m tools.bootstrap_admin --phone <手机号> --name <姓名>"
                     % (self.orch_dir, C.venv_python(self.venv_orch)))
            return True, "配置已写入（管理员账号需手工创建）。"

        self.log("创建首个管理员 %s …" % phone)
        env = {k: str(v) for k, v in self.cfg.items()}
        rc, out = C.run([str(C.venv_python(self.venv_orch)), "-m", "tools.bootstrap_admin",
                         "--phone", phone, "--name", self.admin_name,
                         "--roles", "platform_admin,ops_director"],
                        timeout=300, cwd=str(self.orch_dir), env=env)
        self.log((out or "")[-800:])
        self.pwd_hint = ""
        for line in (out or "").splitlines():
            if "密码" in line or "password" in line.lower():
                self.pwd_hint = line.strip()
        self.secrets["BOOTSTRAP_PHONE"] = phone
        if rc != 0:
            return False, "创建管理员失败（见日志；账号已存在也会返回非 0）。"
        return True, "首个管理员 %s 已创建。" % phone

    # -- 09 前端与入口 ----------------------------------------------------
    def step_web(self) -> Tuple[bool, str]:
        src = C.payload_dir() / "frontend"
        if not (src / "index.html").is_file():
            return False, "载荷里缺前端产物：%s（打包机要先 npm run build）" % src
        if self.web_root.exists():
            shutil.rmtree(self.web_root, ignore_errors=True)
        self.web_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, self.web_root)
        if not (self.web_root / "fonts" / "zhitu-serif.woff2").is_file():
            self.log("[提示] 前端字体缺失：字标会回退成系统宋体")
        self.log("前端已落盘：%s" % self.web_root)

        # Caddy
        caddy = self.app_dir / "bin" / "caddy.exe"
        if not caddy.is_file():
            ok, res = self._fetch_zip(C.CADDY_ZIP_URL, "caddy_2.8.4_windows_amd64.zip", 8)
            if not ok:
                return False, "拿不到 Caddy：%s" % res
            tmp = self.app_dir / "_caddy_tmp"
            ok, msg = C.unzip(Path(res), tmp, self.log)
            if not ok:
                return False, msg
            found = next(tmp.rglob("caddy.exe"), None)
            if not found:
                return False, "解压后没找到 caddy.exe。"
            caddy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, caddy)
            shutil.rmtree(tmp, ignore_errors=True)
            self.log("caddy.exe 就位：%s" % caddy)

        tpl = (C.payload_dir() / "caddy" / "Caddyfile.tpl").read_text(encoding="utf-8")
        rendered = (tpl.replace("{{APP}}", str(self.app_dir))
                       .replace("{{API_PORT}}", str(C.DEFAULT_API_PORT))
                       .replace("{{HTTP_PORT}}", str(C.DEFAULT_HTTP_PORT))
                       .replace("{{LS_PORT}}", str(C.DEFAULT_LS_PORT))
                       .replace("{{WEB_ROOT}}", str(self.web_root))
                       .replace("{{LOG_DIR}}", str(self.data_dir / "logs")))
        cfg = self.data_dir / "config" / "Caddyfile"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(rendered, encoding="utf-8", newline="\n")
        self.log("入口配置：%s" % cfg)

        # Windows 防火墙：默认拦入站。不放行这两个端口的话，服务器本机用
        # 127.0.0.1 打得开（环回流量不过防火墙），局域网里其他同事打不开 ——
        # 甲方的原话会是「装好了但网页打不开」，而且几乎没人会想到是防火墙。
        # 先删后加保证幂等：重装时端口若变了，同名旧规则会被覆盖掉。
        for port, label in ((C.DEFAULT_HTTP_PORT, "平台入口"),
                            (C.DEFAULT_LS_PORT, "标注引擎")):
            rule = "智途 %s %d" % (label, port)
            C.run(["netsh", "advfirewall", "firewall", "delete", "rule",
                   "name=%s" % rule], timeout=30)
            rc, out = C.run(["netsh", "advfirewall", "firewall", "add", "rule",
                             "name=%s" % rule, "dir=in", "action=allow",
                             "protocol=TCP", "localport=%d" % port,
                             "profile=any"], timeout=30)
            if rc == 0:
                self.log("防火墙已放行 TCP %d（%s）" % (port, label))
            else:
                # 组策略托管的机器会拒 netsh。不阻断安装：本机照样能用，
                # 只是同事访问要手工加规则，说明书「常见问题」里写了怎么做。
                self.log("[提示] 防火墙规则没加上（%d）：%s"
                         % (port, (out or "").strip()[-160:]))

        rc, out = C.run([str(caddy), "validate", "--config", str(cfg),
                         "--adapter", "caddyfile"], timeout=60)
        if rc != 0:
            self.log((out or "")[-600:])
            return False, "Caddy 配置校验失败（见日志）。"
        self.log("Caddy 配置校验通过")
        self.report["caddy"] = str(caddy)
        self.report["caddyfile"] = str(cfg)
        return True, "前端与入口就绪。"

    # -- 10 计划任务 ------------------------------------------------------
    def _launcher(self, name: str, env: Dict[str, str], work_dir: Path,
                  command: str) -> Path:
        log_file = self.data_dir / "logs" / (name + ".log")
        path = self.data_dir / ("run-%s.cmd" % name)
        C.write_launcher(path, env, work_dir, command, log_file)
        return path

    def step_tasks(self) -> Tuple[bool, str]:
        env = {k: str(v) for k, v in self.cfg.items()}
        env.setdefault("FORGE_LS_REFRESH_TOKEN", "")
        py = str(C.venv_python(self.venv_orch))

        # 1) 编排层
        orch_cmd = self._launcher("orchestrator", env, self.orch_dir,
                                  '"%s" -m app.serve' % py)
        # 不注册计划任务：三个进程在下面统一启动，谁已经在跑就跳过。
        # (显示名, 启动脚本, 工作目录, 端口)
        pending: List[Tuple[str, Path, Path, int]] = [
            ("编排层", orch_cmd, self.orch_dir, C.DEFAULT_API_PORT)]

        # 2) 标注入擎
        ls_env = {
            "LABEL_STUDIO_BASE_DATA_DIR": str(self.ls_data),
            "DISABLE_SIGNUP_WITHOUT_LINK": "true",
            "FORGE_LS_SSO_SECRET": self.cfg.get("FORGE_LS_SSO_SECRET", ""),
            # 本机内网回调，不关掉 SSRF 防护会静默丢 webhook
            "USE_DEFAULT_BANNED_SUBNETS": "false",
            "DJANGO_SECRET_KEY": self.cfg.get("LS_SECRET_KEY", ""),
            "DJANGO_DB": "default",
            "POSTGRE_NAME": "labelstudio",
            "POSTGRE_USER": "ls",
            "POSTGRE_PASSWORD": self.cfg.get("LS_POSTGRE_PASSWORD", ""),
            "POSTGRE_HOST": "127.0.0.1",
            "POSTGRE_PORT": str(self.pg_port),
        }
        # 标注入口：标准 venv 用 Scripts/label-studio.exe；离线便携运行时没有
        # console_scripts 生成的 exe，用 start_ls.py 转发同一个 main()，参数
        # 结构完全一致（argv[0] 是脚本名，后面还是 start --port …）。
        ls_exe = self.venv_ls / "Scripts" / "label-studio.exe"
        if ls_exe.is_file():
            ls_home = ls_exe.parent
            ls_head = '"%s"' % ls_exe
        else:
            ls_home = self.venv_ls
            ls_head = '"%s" "%s"' % (C.venv_python(self.venv_ls),
                                       self.venv_ls / "start_ls.py")
        ls_cmd = self._launcher(
            "labelstudio", ls_env, ls_home,
            '%s start --port %d --data-dir "%s" --no-browser'
            % (ls_head, C.DEFAULT_LS_PORT, self.ls_data))
        pending.append(("标注引擎", ls_cmd, ls_home, C.DEFAULT_LS_PORT))

        # 3) 入口
        caddy = Path(str(self.report.get("caddy") or (self.app_dir / "bin" / "caddy.exe")))
        caddyfile = Path(str(self.report.get("caddyfile") or
                             (self.data_dir / "config" / "Caddyfile")))
        caddy_cmd = self._launcher("caddy", {}, caddy.parent,
                                   '"%s" run --config "%s" --adapter caddyfile'
                                   % (caddy, caddyfile))
        pending.append(("入口", caddy_cmd, caddy.parent, C.DEFAULT_HTTP_PORT))

        # 直接起进程，不注册任何东西。启动脚本留盘（run-*.cmd），
        # 手工重启用同一份命令、日志也落在同一处。
        for label, cmd, wd, port in pending:
            if C.port_open(port):
                self.log("%s 已在 %d 上跑着，不重复启动" % (label, port))
                continue
            pid = C.spawn_detached(["cmd.exe", "/c", str(cmd)], wd)
            if not pid:
                return False, "启动%s失败：%s" % (label, cmd)
            self.log("启动%s：PID %d（%s）" % (label, pid, cmd.name))
        for label, cmd, wd, port in pending:
            # 标注引擎要跑 Django 迁移，首次启动 60-90 秒，别按 120 秒卡
            limit = 300 if port == C.DEFAULT_LS_PORT else 150
            deadline = time.time() + limit
            while time.time() < deadline:
                if C.port_open(port):
                    break
                time.sleep(2)
            else:
                return False, ("%s 没能在 %d 上监听起来（等了 %d 秒）。"
                               "看 %s 里的 %s.err.log。"
                               % (label, port, limit, self.data_dir / "logs", cmd.stem))
            self.log("%s 就绪：%d" % (label, port))

        # 4) 每日备份（凌晨 3:00，保留 7 份）
        # 为什么不塞进上面那三个：那三个是「服务型」任务，开机启动 + 每 1 分钟
        # 重复触发。备份如果是那种触发器，一天会跑 1440 次，冷备还会反复停库。
        # 备份任务单独用 -Daily 触发器，且不在安装时启动（等它到点自己跑）。
        #
        # 也不在这里 fail-soft：三个服务任务失败会阻断，备份失败同样阻断 ——
        # 这是交付给甲方的生产环境，没有自动备份的部署不该被当成"装好了"。
        # 把目录显式传进去：计划任务以 SYSTEM 运行，读不到用户的
        # %LOCALAPPDATA%\Zhitu\native.json，backup_db 会静默退回默认
        # C:\Zhitu\...。装在自定义目录的机器上，备份就会每晚失败而白天一切正常。
        backup_cmd = self._launcher(
            "backup",
            {"ZHITU_APP_DIR": str(self.app_dir),
             "ZHITU_DATA_DIR": str(self.data_dir)},
            self.orch_dir,
            '"%s" -m tools.backup_db --keep 7' % py)
        # 守护进程：替代原来的「每分钟重复触发」与「每日 03:00 备份」两个计划任务。
        # 一个常驻循环做同样的事，且不写 SCM、不写任务计划库、不需要 SYSTEM 身份。
        ok, msg = self._start_watchdog()
        self.log(msg)
        if not ok:
            self.log("[提示] 守护没起来不影响现在使用，只是崩了不会自动拉起")
        self.log("备份目录：%s（保留最近 7 份，由守护每天 03:00 执行）"
                 % (self.data_dir / "backup"))
        return True, "三个进程已在运行，守护进程已接管保活与每日备份"

        return True, "四个计划任务已注册（三个服务 + 每日备份）"

    # -- 11 健康检查 ------------------------------------------------------
    def _start_watchdog(self) -> Tuple[bool, str]:
        """起守护进程（本 exe --watch）。

        它干两件事：三个进程崩了 5 秒内拉起；每天 03:00 跑一次备份。
        这就是「不注册计划任务」的替代方案 —— 服务器常开，一个常驻循环能做
        同样的事，还省掉 SYSTEM 身份、WinRM/CIM、组策略那一串坑。
        """
        pidf = self.data_dir / "watchdog.pid"
        try:
            old = int(pidf.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old = 0
        if C.pid_alive(old):
            return True, "守护进程已在运行（PID %d）" % old
        exe = Path(sys.executable)
        if getattr(sys, "frozen", False):
            cmd = [str(exe), "--watch"]
        else:
            cmd = [str(exe), str(Path(__file__).resolve()), "--watch"]
        pid = C.spawn_detached(cmd, self.app_dir)
        if not pid:
            return False, "守护进程起不来：%s" % exe
        try:
            pidf.parent.mkdir(parents=True, exist_ok=True)
            pidf.write_text(str(pid), encoding="utf-8")
        except OSError:
            pass
        return True, ("守护进程已启动（PID %d）：进程挂了 5 秒内拉起，"
                      "每天 03:00 自动备份" % pid)

    def step_health(self) -> Tuple[bool, str]:
        checks = [
            ("编排层 /healthz", "http://127.0.0.1:%d/healthz" % C.DEFAULT_API_PORT),
            ("平台入口 :80", "http://127.0.0.1:%d/" % C.DEFAULT_HTTP_PORT),
            ("标注引擎 :%d" % C.DEFAULT_LS_PORT,
             "http://127.0.0.1:%d/health" % C.DEFAULT_LS_PORT),
        ]
        deadline = time.time() + 420
        last: Dict[str, Dict[str, object]] = {}
        while time.time() < deadline:
            last = {name: C.probe_http(url, timeout=5) for name, url in checks}
            if all(v["ok"] for v in last.values()):
                for name, v in last.items():
                    self.log("  %s OK（%sms）" % (name, v["ms"]))
                return True, "三个入口全部就绪。"
            time.sleep(5)
        for name, v in last.items():
            self.log("  %s %s" % (name, "OK" if v["ok"] else "不可达 code=%s" % v["code"]))
        logs = self.data_dir / "logs"
        return False, ("7 分钟内没全部就绪。看 %s 下的 *.err.log；"
                       "LS 第一次启动要建表，可能确实慢。" % logs)

    # -- 12 LS 长期凭证 ---------------------------------------------------
    def step_token(self) -> Tuple[bool, str]:
        """取 LS refresh token 写进配置并重启编排层。

        LS 的 access token 只有 5 分钟，编排层靠 refresh 续。没有它，
        服务全绿、网页能开，但标注员标完之后平台数字不动 —— 这是整套部署里
        最难自己查出来的坑。
        """
        existing = ""
        if self.env_file.is_file():
            for line in self.env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("FORGE_LS_REFRESH_TOKEN="):
                    existing = line.split("=", 1)[1].strip()
        if existing:
            self.log("已有 FORGE_LS_REFRESH_TOKEN，跳过")
            return True, "沿用现有 LS 凭证。"

        email = "admin@zhitu.local"
        password = self.secrets.get("LS_ADMIN") or ""
        if not password:
            return False, ("还没有 LS 管理员账号。请先在 http://%s:%d 注册一个账号，"
                           "然后把 refresh token 手工填进 %s 的 FORGE_LS_REFRESH_TOKEN。"
                           % (self.host_ip, C.DEFAULT_LS_PORT, self.env_file))

        base = "http://127.0.0.1:%d" % C.DEFAULT_LS_PORT
        ok, token = fetch_ls_refresh(base, email, password)
        if not ok:
            return False, "拿不到 LS 凭证：%s" % token
        text = self.env_file.read_text(encoding="utf-8", errors="replace")
        if "FORGE_LS_REFRESH_TOKEN=" in text:
            text = "\n".join(("FORGE_LS_REFRESH_TOKEN=" + token)
                             if ln.startswith("FORGE_LS_REFRESH_TOKEN=") else ln
                             for ln in text.splitlines())
        else:
            text += "\r\nFORGE_LS_REFRESH_TOKEN=%s\r\n" % token
        self.env_file.write_text(text, encoding="utf-8")
        self.log("已写入 FORGE_LS_REFRESH_TOKEN（%s…）" % token[:8])

        env = {k: str(v) for k, v in self.cfg.items()}
        env["FORGE_LS_REFRESH_TOKEN"] = token
        orch_cmd = self._launcher("orchestrator", env, self.orch_dir,
                                  '"%s" -m app.serve' % C.venv_python(self.venv_orch))
        # 重启编排层：不注册任务，直接杀旧进程再起新的
        C.kill_port(C.DEFAULT_API_PORT, self.log)
        time.sleep(1)
        pid = C.spawn_detached(["cmd.exe", "/c", str(orch_cmd)], self.orch_dir)
        self.log("编排层已重启（PID %d）" % pid)
        for _ in range(30):
            if C.probe_http("http://127.0.0.1:%d/healthz" % C.DEFAULT_API_PORT)["ok"]:
                return True, "LS 凭证已生效，编排层已重启。"
            time.sleep(2)
        return False, "凭证已写入，但编排层 60 秒内没恢复响应，看 logs\\orchestrator.err.log。"

    # -- 13 收尾 ----------------------------------------------------------
    def step_finish(self) -> Tuple[bool, str]:
        made = []
        # 遗留演示账号：全新安装是空库不会有，但「重装到旧实例」会留着
        # （早期版本 003_seed_verify.sql 灌的两个演示号）。这里不自动删 ——
        # 那个号可能已经在派活用，删了会连带影响任务归属。只在安装报告里点名。
        rc_l, out_l = self._db(
            "SELECT 1 FROM forge.users "
            "WHERE phone IN ('+8613800138000', '+8613800138001') LIMIT 1",
            db="forge")
        if rc_l == 0 and self._hit(out_l):
            self.log("发现遗留演示账号（+8613800138000 张伟 / +8613800138001）："
                     "不会自动删除，请登录后在「人员管理」里停用或改绑。")
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        target = "http://%s/" % self.host_ip
        try:
            lnk = desktop / "智途平台.url"
            lnk.write_text("[InternetShortcut]\r\nURL=%s\r\n" % target, encoding="utf-8")
            made.append("智途平台.url")
        except OSError as exc:
            self.log("创建桌面网址失败：%s" % exc)
        # 服务器重启后一键恢复：起守护，守护再把三个进程拉起来。
        # 不注册开机自启（用户明确不要），所以留一个双击就能用的入口。
        try:
            bat = desktop / "启动智途.bat"
            bat.write_text("@echo off\r\n"
                           "chcp 65001 >nul\r\n"
                           "echo 正在启动智途平台（守护进程会自动拉起三个服务）…\r\n"
                           "start \"智途守护\" \"%s\" --watch\r\n"
                           "timeout /t 3 >nul\r\n"
                           "start \"\" \"%s\"\r\n" % (sys.executable, target),
                           encoding="utf-8", newline="")
            made.append("启动智途.bat")
        except OSError as exc:
            self.log("创建启动脚本失败：%s" % exc)
        C.save_config({"app_dir": str(self.app_dir), "data_dir": str(self.data_dir),
                       "host_ip": self.host_ip, "pg_port": self.pg_port,
                       "python": self.py})
        msg = "平台：%s" % target
        return True, msg + ("（桌面已放快捷方式：%s）" % "、".join(made) if made else "")


# ---------------------------------------------------------------------------
# LS 凭证获取（内联，避免依赖容器版模块）
# ---------------------------------------------------------------------------
def fetch_ls_refresh(base: str, email: str, password: str) -> Tuple[bool, str]:
    """登录 LS 抓 refresh token。

    必须绕开系统代理：本机有 HTTP_PROXY 时，发往 127.0.0.1 的请求会被代理
    接走并返回 502，看起来像"LS 没起来"。
    """
    import http.cookiejar
    import re as _re
    import urllib.parse
    import urllib.request as U

    jar = http.cookiejar.CookieJar()
    opener = U.build_opener(U.HTTPCookieProcessor(jar), U.ProxyHandler({}))
    try:
        with opener.open(base + "/user/login/", timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
        m = _re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
        if not m:
            return False, "拿不到 CSRF token（LS 登录页结构变了？）"
        data = urllib.parse.urlencode({
            "csrfmiddlewaretoken": m.group(1),
            "email": email, "password": password,
        }).encode()
        req = U.Request(base + "/user/login/?next=/", data=data,
                        headers={"Referer": base + "/user/login/"})
        with opener.open(req, timeout=20) as r:
            final = r.geturl()
        if "/user/login" in final:
            return False, "登录被拒（账号或口令不对）"
        req = U.Request(base + "/api/token/", data=b"{}",
                        headers={"Content-Type": "application/json",
                                 "Referer": base + "/"})
        import json
        with opener.open(req, timeout=20) as r:
            body = json.loads(r.read().decode() or "{}")
        token = body.get("refresh") or ""
        return (True, token) if token else (False, "接口没返回 refresh 字段")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class SetupApp:
    def __init__(self) -> None:
        self.s = C.setup_dpi()
        self.root = tk.Tk()
        self.root.title("智途 · 安装程序（Windows 原生）")
        self.root.configure(bg=BG)
        self.root.geometry("%dx%d" % (int(1000 * self.s), int(700 * self.s)))

        self.q: "queue.Queue[Tuple[str, str, str]]" = queue.Queue()
        self.running = False
        self.steps: List[Step] = []

        self._style()
        self._ui()
        self._build_steps()
        self._fit(1000, 700)
        self.root.after(120, self._drain)
        self._log("本安装程序只用 Windows 自带的组件：PostgreSQL、Python、Caddy、计划任务。")
        self._log("不启动 Docker、不需要 WSL、不需要任何 Linux 环境。")

    def _fit(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("%dx%d" % (
            min(max(int(w * self.s), self.root.winfo_reqwidth()), sw - 24),
            min(max(int(h * self.s), self.root.winfo_reqheight()), sh - 80)))

    def f(self, size: int, bold: bool = False, mono: bool = False) -> tuple:
        fam = "Consolas" if mono else "Microsoft YaHei UI"
        return (fam, max(8, int(round(size * self.s))), "bold" if bold else "normal")

    def px(self, n: int) -> int:
        return max(1, int(round(n * self.s)))

    def _style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                     bordercolor=LINE, font=self.f(9))
        st.configure("TButton", background=BG3, foreground=FG, padding=(14, 7))
        st.map("TButton", background=[("active", "#2C3138"), ("disabled", BG2)],
               foreground=[("disabled", FG_DIM)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#14161A",
                     font=self.f(10, bold=True))
        st.map("Accent.TButton", background=[("active", "#AD97EE"),
                                             ("disabled", "#4A4560")])
        st.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG,
                     rowheight=self.px(30), borderwidth=0)
        st.map("Treeview", background=[("selected", "#2E3440")])

    def _ui(self) -> None:
        head = tk.Frame(self.root, bg=BG2, height=self.px(64))
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="智途", bg=BG2, fg=FG,
                 font=self.f(15, bold=True)).pack(side="left", padx=(self.px(20), self.px(8)))
        tk.Label(head, text="·  Windows 原生安装程序（无 Docker / 无 WSL）", bg=BG2,
                 fg=FG_DIM, font=self.f(10)).pack(side="left")
        self.lbl_admin = tk.Label(head, text="", bg=BG2, fg=WARN_C, font=self.f(9))
        self.lbl_admin.pack(side="right", padx=20)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=self.px(16), pady=self.px(12))

        left = tk.Frame(body, bg=BG, width=self.px(300))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="安装步骤", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(anchor="w", pady=(0, self.px(6)))
        self.tree = ttk.Treeview(left, columns=("s",), show="tree",
                                 selectmode="none", height=13)
        self.tree.column("#0", width=self.px(280))
        self.tree.pack(fill="both", expand=True)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(self.px(16), 0))
        opt = tk.Frame(right, bg=BG2)
        opt.pack(fill="x", pady=(0, self.px(8)))
        self.host_ip_var = tk.StringVar(value=C.local_ip() or "")
        self.phone_var = tk.StringVar()
        self.name_var = tk.StringVar(value="管理员")
        for label, var, width in (("服务器地址", self.host_ip_var, 15),
                                  ("管理员手机号", self.phone_var, 15),
                                  ("姓名", self.name_var, 9)):
            tk.Label(opt, text=label, bg=BG2, fg=FG_DIM,
                     font=self.f(9)).pack(side="left", padx=(self.px(10), self.px(6)),
                                          pady=self.px(8))
            tk.Entry(opt, textvariable=var, width=width, bg=BG3, fg=FG,
                     insertbackground=FG, relief="flat",
                     font=self.f(9, mono=True)).pack(side="left", ipady=self.px(3))
        tk.Label(right, text="日志", bg=BG, fg=FG_DIM,
                 font=self.f(9)).pack(anchor="w", pady=(0, self.px(6)))
        wrap = tk.Frame(right, bg=LINE)
        wrap.pack(fill="both", expand=True)
        self.logbox = tk.Text(wrap, bg="#0F1113", fg="#C9CFD6", bd=0, width=1,
                              wrap="word", insertbackground=FG,
                              font=self.f(9, mono=True), padx=self.px(10),
                              pady=self.px(8))
        sb = ttk.Scrollbar(wrap, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)

        foot = tk.Frame(self.root, bg=BG2, height=self.px(64))
        foot.pack(side="bottom", fill="x", before=body)
        foot.pack_propagate(False)
        self.lbl_dir = tk.Label(foot, text="装到：" + C.DEFAULT_APP_DIR, bg=BG2,
                                fg=FG_DIM, font=self.f(9))
        self.lbl_dir.pack(side="left", padx=(self.px(20), 4))
        ttk.Button(foot, text="更改", width=6,
                   command=self._pick_dir).pack(side="left")
        ttk.Button(foot, text="打开平台",
                   command=self._open).pack(side="right", padx=(0, 16))
        self.btn_run = ttk.Button(foot, text="开始安装", style="Accent.TButton",
                                  command=self._start)
        self.btn_run.pack(side="right", padx=8)
        self.app_dir = C.DEFAULT_APP_DIR

    def _build_steps(self) -> None:
        # 日志双写：窗口里能看，同时落一份文件。出问题时用户直接把文件发过来，
        # 不用再靠截图猜（截图经常正好截掉最关键的那几行）。
        self._sink = C.tee_sink("gui", self._log)
        self.inst = Installer(self._sink, app_dir=self.app_dir,
                              host_ip=self.host_ip_var.get(),
                              admin_phone=self.phone_var.get(),
                              admin_name=self.name_var.get())
        i = self.inst
        self.steps = step_list(i)
        for s in self.steps:
            self.tree.insert("", "end", iid=s.key, text="○  " + s.title)
        self.lbl_admin.config(
            text="已以管理员运行" if C.is_admin() else "未提权：安装需要管理员")

    # -- 交互 -------------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.q.put(("log", str(msg), ""))

    def _drain(self) -> None:
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", a + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "step":
                    self._paint(a, b)
                elif kind == "done":
                    self.running = False
                    self.btn_run.configure(state="normal", text="重新运行")
                    messagebox.showinfo("安装结束", a)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _paint(self, key: str, state: str) -> None:
        s = next((x for x in self.steps if x.key == key), None)
        if not s:
            return
        icon = {"pending": "○", "running": "◐", "ok": "●", "fail": "✕",
                "skip": "–"}.get(state, "○")
        self.tree.item(key, text="%s  %s" % (icon, s.title))

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择安装目录")
        if d:
            self.app_dir = d
            self.inst.app_dir = Path(d)
            self.lbl_dir.config(text="装到：" + d)

    def _open(self) -> None:
        ip = self.host_ip_var.get().strip() or "127.0.0.1"
        os.startfile("http://%s/" % ip)  # noqa: S606

    def _start(self) -> None:
        if self.running:
            return
        if not C.is_admin():
            if messagebox.askyesno("需要管理员权限",
                                   "安装要建数据库服务、写 ProgramData、注册计划任务，\n"
                                   "必须用管理员身份。现在重新以管理员启动吗？"):
                if C.elevate_self():
                    self.root.destroy()
                    return
                messagebox.showwarning("未能提权", "提权被取消，安装无法继续。")
            return
        self.inst.host_ip = self.host_ip_var.get().strip() or C.local_ip()
        self.inst.admin_phone = self.phone_var.get().strip()
        self.inst.admin_name = self.name_var.get().strip() or "管理员"
        self.running = True
        self.btn_run.configure(state="disabled", text="安装中…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        results = run_steps(self.steps, self._log,
                            paint=lambda k, st: self.q.put(("step", k, st)))
        report = write_report(self.inst, results,
                              near=getattr(self._sink, "path", ""))
        n_bad = sum(1 for r in results if r[2] == FAIL)
        ip = self.host_ip_var.get().strip() or "127.0.0.1"
        if n_bad == 0:
            extra = ""
            phone = self.phone_var.get().strip()
            if phone:
                extra = "\n\n管理员账号：%s（初始密码在日志里，只显示一次）" % phone
            self.q.put(("done", "安装完成。\n\n平台地址：http://%s/%s\n\n"
                                "标注引擎：http://%s:%d/\n\n"
                                "守护进程在跑：进程挂了 5 秒内拉起，每天 03:00 自动备份。"
                        % (ip, extra, ip, C.DEFAULT_LS_PORT), ""))
        else:
            bad = [r for r in results if r[2] == FAIL]
            head = "\n".join(
                "· %s\n  %s" % (t, ((m or "").strip().splitlines() or [""])[0][:110])
                for _k, t, _s, m in bad)
            tip = (("\n\n完整的 13 步结果在：\n%s\n\n把这个文件发出来，一次就能看完。"
                    % report) if report else "")
            self.q.put(("done", "跑完了，有 %d 步没成功：\n\n%s%s"
                        % (n_bad, head, tip), ""))

    def run(self) -> None:
        self.root.mainloop()


def _backed_today(data_dir: Path) -> bool:
    """今天有没有备份过。判断依据是 backup 目录里文件的修改时间。"""
    today = time.strftime("%Y-%m-%d")
    try:
        for p in (data_dir / "backup").glob("*"):
            if (p.is_file()
                    and time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)) == today):
                return True
    except OSError:
        pass
    return False


def watch_loop(interval: int = 5) -> int:
    """守护循环：进程挂了拉起 + 每天 03:00 备份。

    由安装程序以 `--watch` 启动（不注册计划任务/服务）。PID 记在
    <数据目录>/watchdog.pid，监控台据此判断它还在不在。
    """
    out = C.cli_sink("watch")
    cfg = C.load_config() or {}
    app_dir = Path(str(cfg.get("app_dir") or C.DEFAULT_APP_DIR))
    data_dir = Path(str(cfg.get("data_dir") or C.DEFAULT_DATA_DIR))
    try:
        pg_port = int(str(cfg.get("pg_port") or C.DEFAULT_PG_PORT))
    except (TypeError, ValueError):
        pg_port = C.DEFAULT_PG_PORT
    pg_root = app_dir / "pgsql"
    pg_data = data_dir / "pgdata"

    targets = (
        ("编排层", data_dir / "run-orchestrator.cmd", C.DEFAULT_API_PORT),
        ("标注引擎", data_dir / "run-labelstudio.cmd", C.DEFAULT_LS_PORT),
        ("入口", data_dir / "run-caddy.cmd", C.DEFAULT_HTTP_PORT),
    )
    out("智途守护启动 %s（间隔 %ds）" % (time.strftime("%Y-%m-%d %H:%M:%S"), interval))
    out("安装目录 %s ／ 数据目录 %s" % (app_dir, data_dir))
    # 单实例：重复点「启动智途」不该起出第二个守护（两个都在拉进程会互相打架）
    pidf = data_dir / "watchdog.pid"
    try:
        other = int(pidf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        other = 0
    if other and other != os.getpid() and C.pid_alive(other):
        out("已有守护在跑（PID %d），本次退出" % other)
        return 0
    try:
        pidf.parent.mkdir(parents=True, exist_ok=True)
        pidf.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    try:
        while True:
            for label, cmdfile, port in targets:
                if C.port_open(port) or not cmdfile.is_file():
                    continue
                pid = C.spawn_detached(["cmd.exe", "/c", str(cmdfile)], app_dir)
                out("%s  %s 没在 %d 上，已拉起（PID %s）"
                    % (time.strftime("%H:%M:%S"), label, port, pid or "失败"))
            # 数据库也没注册服务，挂了同样由这里拉起
            if not C.port_open(pg_port):
                ok, msg = C.pg_start_local(pg_root, pg_data, pg_port, log=out)
                out("%s  PostgreSQL：%s" % (time.strftime("%H:%M:%S"), msg))
            # 每日备份（原计划任务 ZhituBackupDaily 的替代）
            bk = data_dir / "run-backup.cmd"
            if (time.strftime("%H:%M") >= "03:00" and bk.is_file()
                    and not _backed_today(data_dir)):
                C.spawn_detached(["cmd.exe", "/c", str(bk)], app_dir)
                out("%s  触发每日备份" % time.strftime("%H:%M:%S"))
            time.sleep(interval)
    except KeyboardInterrupt:
        out("守护退出")
        return 0


def auto_run(argv: List[str]) -> int:
    out = C.cli_sink("auto")
    out("智途 · Windows 原生安装 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("日志：%s" % out.path)
    host_ip, phone, name = "", "", "管理员"
    for i, a in enumerate(argv):
        if a == "--host-ip" and i + 1 < len(argv):
            host_ip = argv[i + 1]
        if a == "--admin-phone" and i + 1 < len(argv):
            phone = argv[i + 1]
        if a == "--admin-name" and i + 1 < len(argv):
            name = argv[i + 1]
    inst = Installer(out, host_ip=host_ip, admin_phone=phone, admin_name=name,
                     allow_download="--no-download" not in argv)
    results = run_steps(step_list(inst), out)
    report = write_report(inst, results, near=out.path)
    out("")
    out(format_summary(results))
    if report:
        out("")
        out("完整报告：%s" % report)
    n_bad = sum(1 for r in results if r[2] == FAIL)
    n_skip = sum(1 for r in results if r[2] == SKIP)
    if n_bad:
        out("")
        out("有 %d 步失败、%d 步跳过。修好后重跑即可，已完成的会跳过。" % (n_bad, n_skip))
        return 1
    out("")
    out("安装完成：http://%s/" % inst.host_ip)
    return 0


def main() -> int:
    C.fix_console()
    if "--selftest" in sys.argv:
        return C.selftest()
    if "--watch" in sys.argv:
        return watch_loop()
    if "--auto" in sys.argv:
        return auto_run(sys.argv[1:])
    SetupApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
