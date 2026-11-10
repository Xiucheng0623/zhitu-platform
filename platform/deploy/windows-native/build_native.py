#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把原生安装程序打成 .exe（PyInstaller onefile）。

为什么必须用带 tkinter 的解释器
----------------------------------------------------------------------------
托管解释器（workbuddy 自带那个）与各项目 venv 里都没有 tkinter，用它们打出来的
exe 一开界面就 ImportError。本机可用的是：
    C:\\Users\\G\\AppData\\Local\\Programs\\Python\\Python311\\python.exe  (Tk 8.6) ← 默认
    C:\\Python315\\python.exe                                           (Tk 9.0)
选 3.11：PyInstaller 对新解释器支持滞后。

产物
----------------------------------------------------------------------------
    dist/智途安装程序（原生）.exe     安装向导（--auto / --selftest）
    dist/智途监控（原生）.exe         监控台（--status）

用法
----------------------------------------------------------------------------
    python build_native.py
    python build_native.py --clean
    python build_native.py --python <解释器路径>
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
DIST = HERE / "dist"
BUILD = HERE / "build"
# 复用容器版那张图标（同一个品牌，不重复维护两份 .ico）
ICON = HERE.parent / "windows-client" / "src" / "app.ico"

CANDIDATES = [
    r"C:\Users\G\AppData\Local\Programs\Python\Python311\python.exe",
    r"C:\Python315\python.exe",
    r"C:\Python311\python.exe",
]

APPS = [
    ("智途安装程序（原生）", "zn_setup.py"),
    ("智途监控（原生）", "zn_monitor.py"),
]


def find_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    for p in CANDIDATES:
        if not Path(p).is_file():
            continue
        if subprocess.run([p, "-c", "import tkinter,PyInstaller"],
                          capture_output=True).returncode == 0:
            return p
    raise SystemExit("找不到同时具备 tkinter 与 PyInstaller 的解释器。\n"
                     "请先在该解释器里 pip install pyinstaller，或用 --python 指定。")


def build_one(py: str, name: str, entry: str,
              add_data: "list[tuple[Path, str]] | None" = None) -> Path:
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--noconsole", "--name", name,
           "--distpath", str(DIST), "--workpath", str(BUILD), "--specpath", str(BUILD),
           "--paths", str(SRC), "--hidden-import", "zn_common"]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    # payload 直接塞进 exe：交付时**只有一个文件**，不用再给一个 payload 目录。
    # 安装程序启动时 PyInstaller 会解到 sys._MEIPASS/payload（见 zn_common.payload_dir）。
    for src, dest in (add_data or []):
        cmd += ["--add-data", "%s%s%s" % (src, os.pathsep, dest)]
    cmd.append(str(SRC / entry))
    print("\n$ " + " ".join(cmd))
    if subprocess.run(cmd, cwd=str(HERE)).returncode != 0:
        raise SystemExit("打包 %s 失败" % name)
    out = DIST / (name + ".exe")
    if not out.is_file():
        raise SystemExit("打包结束但没找到 %s" % out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=None)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--no-embed", action="store_true",
                    help="不把 payload 打进 exe（退回 exe + payload/ 目录的老形态）")
    args = ap.parse_args()

    py = find_python(args.python)
    print("解释器:", py)
    if args.clean:
        for d in (DIST, BUILD):
            shutil.rmtree(d, ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)

    payload = HERE / "payload"
    embed: list = []
    if args.no_embed:
        print("[说明] --no-embed：不内嵌载荷，交付时需要 payload/ 目录")
    elif (payload / "manifest.json").is_file():
        embed = [(payload, "payload")]
        print("内嵌载荷：%s（单文件交付）" % payload)
    else:
        print("[警告] 没有 payload/（先跑 make_payload.py）——这次打成 exe+目录形态")

    results = []
    for name, entry in APPS:
        # 监控台不需要载荷（它只查服务与端口），保持小体积
        data = embed if entry.startswith("zn_setup") else None
        results.append(build_one(py, name, entry, data))
    print("\n" + "=" * 64)
    for p in results:
        print("产出 %-30s %6.1f MB" % (p.name, p.stat().st_size / 1048576))
    print("=" * 64)
    if embed:
        print("交付：两个 exe 就是全部，**不需要 payload 目录**")
        print("  <发布目录>\\智途安装程序（原生）.exe   ← 自包含，拷到服务器双击")
        print("  <发布目录>\\智途监控（原生）.exe")
        print("  想离线装：把 PG / caddy 的包放到 exe 旁的 cache\\ 里即可")
    else:
        print("交付目录（两个 exe 与 payload/ 同层）：")
        print("  <发布目录>\\智途安装程序（原生）.exe")
        print("  <发布目录>\\智途监控（原生）.exe")
        print("  <发布目录>\\payload\\...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
