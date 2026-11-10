#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智途 · 纯源码安装（clone 下来一条命令装完，不要 exe / 不要 payload / 不要 runtime）

为什么要有这个脚本
----------------------------------------------------------------------------
原来交付靠「离线包 zip（366 MB）」或「安装套件 zip（24 MB）」：里面是
PyInstaller 打的 exe + payload。之所以打 exe，唯一理由是**给一个能双击的图标**——
13 步安装逻辑本身就是纯 Python（src/zn_setup.py），跟 tkinter 窗口没有耦合。

于是只要仓库里源码齐全，完全可以：

    git clone <repo> D:\\zhitu-platform
    cd D:\\zhitu-platform\\platform\\deploy\\windows-native
    python install_from_source.py --dir C:\\Zhitu

脚本干四件事（前两件是原来 exe 打包时才做的）：
    1. 现场跑 make_payload.py，把「前端产物 + 编排层源码 + 迁移 + 品牌补丁 +
       Caddy 模板」组装成 windows-native/payload（约 1.8 MB）；
    2. 用环境变量 ZHITU_PAYLOAD_DIR 把它指给安装逻辑，免得复制进 src/ 污染工作树；
    3. 校验 Python 版本在 3.11–3.13（LS 1.23 在更高版本上没有预编译 wheel），
       不在就自动换成系统里合适的那个重新执行；
    4. 调 zn_setup.auto_run 跑完 13 步 —— PostgreSQL / Caddy / pip 依赖都是
       **现场联网下载**，跟离线包相比只是慢（25–45 分钟），不依赖任何预置二进制。

哪一步能省、哪一步不能省
----------------------------------------------------------------------------
  省得掉：PyInstaller、exe、runtime/（1 GB 预置运行时）、tkinter 窗口。
  省不掉：Python 3.11–3.13（用来建两个 venv）、外网（PG 330 MB + pip 依赖）、
          管理员权限（写 C:\\Zhitu、占用 80 端口）。
  前端产物：platform/web/dist 已随仓库提交（536 KB），服务器上**不需要 Node**。
          改过前端源码后记得本机 `cd platform/web && npm run build` 再提交。

用法
----------------------------------------------------------------------------
    python install_from_source.py                     装到 C:\\Zhitu（默认）
    python install_from_source.py --dir D:\\Zhitu       换盘
    python install_from_source.py --check             只体检，不装
    python install_from_source.py --no-download       只用 payload/cache 里的包（断网）
    python install_from_source.py --build-web         前端产物缺失时现场 npm run build
    python install_from_source.py --admin-phone 13800138000
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent            # windows-native/
SRC = HERE / "src"
REPO = HERE.parents[2]                            # 仓库根
WEB = REPO / "platform" / "web"
PAYLOAD = HERE / "payload"
PY_OK = ((3, 11), (3, 12), (3, 13))


def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except (OSError, ValueError):
        pass


def die(msg: str, code: int = 2) -> "None":
    say("")
    say("✗ " + msg)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# 1. Python 版本：必须是 3.11–3.13，否则换成合适的解释器重新执行
# ---------------------------------------------------------------------------
def _stub_tkinter() -> None:
    """没有 tkinter 时塞个空壳。

    src/zn_setup.py 顶部 `import tkinter as tk` + `from tkinter import ...`，
    但那只是给 GUI 用的；--auto 全程不碰窗口。服务器上装的是精简版 Python
    （或 Microsoft Store 版）时 tkinter 会缺，不该因此整条源码安装路走不通。
    """
    try:
        import tkinter  # noqa: F401
        from tkinter import filedialog, messagebox, ttk  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass

    class _Any:
        def __getattr__(self, name: str) -> "_Any":
            return _Any()

        def __call__(self, *a, **k) -> "_Any":
            return _Any()

    mod = types.ModuleType("tkinter")
    mod.__getattr__ = lambda name: _Any()  # type: ignore[attr-defined]
    sys.modules["tkinter"] = mod
    for sub in ("filedialog", "messagebox", "ttk", "scrolledtext", "font"):
        m = types.ModuleType("tkinter." + sub)
        m.__getattr__ = lambda name: _Any()  # type: ignore[attr-defined]
        sys.modules["tkinter." + sub] = m
    say("  （本机 Python 缺 tkinter，已用空壳替代 —— 只影响图形界面，不影响安装）")


def ensure_python(argv: list) -> None:
    cur = sys.version_info[:2]
    if PY_OK[0] <= cur <= PY_OK[-1]:
        return
    if os.environ.get("ZHITU_REEXEC") == "1":
        die("已经换过一次解释器还是 %d.%d，系统里没有可用的 Python 3.11–3.13。\n"
            "装一个再重跑：https://www.python.org/ftp/python/3.12.8/"
            "python-3.12.8-amd64.exe（勾选 Add python.exe to PATH）"
            % sys.version_info[:2])

    sys.path.insert(0, str(SRC))
    import zn_common as C  # noqa: PLC0415

    py, ver = C.find_python()
    if not py:
        die("当前是 Python %d.%d，不在 3.11–3.13 区间；系统里也找不到合适的。\n"
            "Label Studio 1.23 在 3.14+ 上没有预编译 wheel，pip 会从源码编译并失败，\n"
            "所以不能用「最新的」。请先装 3.12：%s" %
            ((sys.version_info.major, sys.version_info.minor), C.PY_DOWNLOAD_HINT))
    if Path(py).resolve() == Path(sys.executable).resolve():
        return
    say("当前 Python %d.%d 不在 3.11–3.13 区间，改用 %s（%s）重新执行 …"
        % (sys.version_info.major, sys.version_info.minor, py, ver))
    env = dict(os.environ, ZHITU_REEXEC="1")
    # 用 subprocess 而不是 os.execv：Windows 上没有真正的 exec，Python 是
    # 「起新进程 + 杀自己」模拟的，控制台句柄会绕一圈，偶尔表现成「窗口一闪就没了」。
    # 起子进程再透传退出码，行为稳定、输出也能完整看到。
    try:
        rc = subprocess.call([py, str(Path(__file__).resolve())] + argv, env=env)
    except OSError as exc:
        die("换成 %s 失败：%s" % (py, exc))
    raise SystemExit(rc)


# ---------------------------------------------------------------------------
# 2. 前端产物 + payload
# ---------------------------------------------------------------------------
def check_web(build: bool) -> None:
    idx = WEB / "dist" / "index.html"
    if idx.is_file():
        n = sum(1 for f in (WEB / "dist").rglob("*") if f.is_file())
        say("前端产物：%s（%d 个文件）" % (WEB / "dist", n))
        return
    if not build:
        die("缺前端产物：%s\n"
            "  仓库里本该带这份产物（536 KB，避免服务器装 Node）。\n"
            "  如果这份 clone 里没有，两种办法：\n"
            "    1) 加 --build-web 让脚本现场 npm ci && npm run build（需要 Node 18+）；\n"
            "    2) 在开发机上 cd platform/web && npm run build，把 dist 提交上来。"
            % idx)
    npm = _which("npm")
    if not npm:
        die("--build-web 需要 npm，但 PATH 里没有。装 Node 18+ 后重试，"
            "或在开发机构建好 dist 再提交。")
    say("现场构建前端（npm ci && npm run build）…")
    for args in (["ci"], ["run", "build"]):
        rc = subprocess.call([npm] + args, cwd=str(WEB))
        if rc != 0:
            die("npm %s 失败（rc=%d）" % (" ".join(args), rc))


def _which(name: str) -> str:
    import shutil
    return shutil.which(name) or ""


def build_payload() -> None:
    say("组装 payload（make_payload.py）…")
    rc = subprocess.call([sys.executable, str(HERE / "make_payload.py")],
                         cwd=str(HERE))
    if rc != 0:
        die("make_payload.py 失败（rc=%d）" % rc)
    must = (PAYLOAD / "app" / "services" / "orchestrator" / "requirements.txt",
            PAYLOAD / "app" / "labelstudio" / "brand" / "apply_brand.py",
            PAYLOAD / "frontend" / "index.html",
            PAYLOAD / "caddy" / "Caddyfile.tpl")
    missing = [str(p) for p in must if not p.is_file()]
    if missing:
        die("payload 组装不完整，缺：\n  " + "\n  ".join(missing))
    migs = list((PAYLOAD / "app" / "deploy" / "migrations").glob("0*.sql"))
    if not migs:
        die("payload 里没有迁移脚本（0*.sql），检查平台源码是否完整")
    say("payload 就绪：%d 个迁移脚本" % len(migs))


# ---------------------------------------------------------------------------
# 3. 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="智途纯源码安装：clone 下来一条命令装完，不需要 exe / 离线包")
    ap.add_argument("--dir", default="",
                    help="安装根目录，默认 C:\\Zhitu（代码落在 <dir>\\app，数据在 <dir>\\data）")
    ap.add_argument("--app-dir", default="", help="单独指定代码目录，覆盖 <dir>\\app")
    ap.add_argument("--data-dir", default="", help="单独指定数据目录，覆盖 <dir>\\data")
    ap.add_argument("--check", action="store_true", help="只体检，不安装")
    ap.add_argument("--plan", action="store_true",
                    help="只打印要装到哪、跑哪 13 步，一步都不执行")
    ap.add_argument("--no-download", action="store_true",
                    help="只用 payload/cache 里预置的 PG/Caddy 包（断网安装）")
    ap.add_argument("--build-web", action="store_true", help="前端产物缺失时现场构建")
    ap.add_argument("--admin-phone", default="", help="管理员手机号")
    ap.add_argument("--admin-name", default="", help="管理员姓名")
    ap.add_argument("--host-ip", default="", help="对外访问地址，默认自动探测")
    args = ap.parse_args()

    sys.path.insert(0, str(SRC))
    import zn_common as C  # noqa: PLC0415

    C.fix_console()
    say("=" * 66)
    say("智途 · 纯源码安装")
    say("  仓库：%s" % REPO)
    say("  Python：%s（%d.%d）" % (sys.executable, *sys.version_info[:2]))
    say("=" * 66)

    root = Path(args.dir or Path(C.DEFAULT_APP_DIR).parent)
    app_dir = Path(args.app_dir) if args.app_dir else (root / "app")
    data_dir = Path(args.data_dir) if args.data_dir else (root / "data")
    say("")
    say("安装目录：%s" % app_dir)
    say("数据目录：%s" % data_dir)

    # -- 前置检查 ---------------------------------------------------------
    say("")
    say("[1/4] 检查前端产物")
    check_web(args.build_web)

    say("")
    say("[2/4] 组装 payload")
    build_payload()

    # 让 zn_common.payload_dir() 认这份现场生成的载荷
    os.environ["ZHITU_PAYLOAD_DIR"] = str(PAYLOAD)

    if not C.is_admin():
        say("")
        say("！当前不是管理员会话。安装要写 %s、占用 80 端口、解压 PostgreSQL，" % app_dir)
        say("  这些在非管理员下会失败。请右键「以管理员身份运行」终端后重跑：")
        say("     python %s" % Path(__file__).name)
        if not (args.check or args.plan):
            return 3

    if args.check:
        say("")
        say("[3/4] 体检模式：只检查，不安装")
        rc = C.selftest()
        say("")
        say("[4/4] 预演完成。真正安装去掉 --check 再跑一次。")
        return rc

    say("")
    say("[3/4] 载入安装逻辑")
    _stub_tkinter()
    import zn_setup  # noqa: PLC0415

    argv = ["--auto", "--app-dir", str(app_dir), "--data-dir", str(data_dir)]
    if args.no_download:
        argv.append("--no-download")
    if args.host_ip:
        argv += ["--host-ip", args.host_ip]
    if args.admin_phone:
        argv += ["--admin-phone", args.admin_phone]
    if args.admin_name:
        argv += ["--admin-name", args.admin_name]

    if args.plan:
        # 把 run_steps 换成空跑，验证「参数 -> Installer -> 13 步」这条链是通的，
        # 同时让用户看到「到底会装到哪个目录」。不碰系统、不下载、不建库。
        seen: list = []
        real_run = zn_setup.run_steps
        zn_setup.run_steps = lambda steps, log: (
            seen.extend((s.key, s.title) for s in steps) or
            [(s.key, s.title, zn_setup.SKIP, "计划模式") for s in steps])
        try:
            zn_setup.auto_run(argv)
        finally:
            zn_setup.run_steps = real_run
        say("")
        say("[4/4] 计划确认（一步都没执行）")
        say("  代码目录：%s" % app_dir)
        say("  数据目录：%s" % data_dir)
        say("  载荷目录：%s" % PAYLOAD)
        say("  步骤 %d 个：%s" % (len(seen), " / ".join(t.split()[0] for _, t in seen)))
        say("")
        say("  去掉 --plan 就是真装。")
        return 0

    say("")
    say("[4/4] 开始跑 13 步（首次约 25–45 分钟，PostgreSQL 和 Label Studio 都要下载）")
    say("")
    rc = zn_setup.auto_run(argv)
    say("")
    say("=" * 66)
    if rc == 0:
        say("装完了。浏览器打开 http://%s/" % (args.host_ip or "127.0.0.1"))
        say("要改代码：直接改这个仓库里的文件，然后重跑第 10 步（启动平台进程）即可。")
    else:
        say("有步骤失败。看上面的报告，或重跑一次（已完成的步骤会自动跳过）。")
        say("日志目录：%s" % C.logs_dir())
    say("=" * 66)
    return rc


if __name__ == "__main__":
    ensure_python(sys.argv[1:])
    raise SystemExit(main())
