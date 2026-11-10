#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把客户端打成两个真正的 .exe（PyInstaller onefile）。

为什么要指定解释器
----------------------------------------------------------------------------
必须用**带 tkinter 的** Python 打包。托管解释器（workbuddy 自带那个）没有
tkinter，用它打出来的 exe 一开界面就 ImportError；而 LS 的 venv 里同样没有。
本机可用的两个：
    C:\\Users\\G\\AppData\\Local\\Programs\\Python\\Python311\\python.exe   (Tk 8.6)  ← 默认
    C:\\Python315\\python.exe                                            (Tk 9.0)
选 3.11 而不是 3.15：PyInstaller 对刚发布的解释器支持有滞后，3.11 是稳的。

产物
----------------------------------------------------------------------------
    dist/智途安装程序.exe    安装向导（--auto 可无界面跑）
    dist/智途监控.exe        监控台（--status 打印一次状态）

用法
----------------------------------------------------------------------------
    python build_client.py                # 打两个 exe
    python build_client.py --clean        # 先清 build/ dist/ 再打
    python build_client.py --python <p>   # 指定解释器
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
DIST = HERE / "dist"
BUILD = HERE / "build"
ICON = SRC / "app.ico"

CANDIDATES = [
    r"C:\Users\G\AppData\Local\Programs\Python\Python311\python.exe",
    r"C:\Python315\python.exe",
    r"C:\Python311\python.exe",
]

APPS = [
    ("智途安装程序", "zhitu_setup.py"),
    ("智途监控", "zhitu_monitor.py"),
]


def find_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    for p in CANDIDATES:
        if not Path(p).is_file():
            continue
        rc = subprocess.run([p, "-c", "import tkinter,PyInstaller"],
                            capture_output=True).returncode
        if rc == 0:
            return p
    raise SystemExit(
        "找不到同时具备 tkinter 与 PyInstaller 的解释器。\n"
        "请先在该解释器里 pip install pyinstaller，或用 --python 指定。")


def build_one(py: str, name: str, entry: str) -> Path:
    cmd = [
        py, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", name,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--paths", str(SRC),
        "--hidden-import", "zhitu_common",
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    cmd.append(str(SRC / entry))
    print("\n$ " + " ".join(cmd))
    rc = subprocess.run(cmd, cwd=str(HERE)).returncode
    if rc != 0:
        raise SystemExit("打包 %s 失败（PyInstaller rc=%d）" % (name, rc))
    out = DIST / (name + ".exe")
    if not out.is_file():
        raise SystemExit("打包结束但没找到 %s" % out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=None)
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    py = find_python(args.python)
    print("解释器:", py)
    rc = subprocess.run([py, "-c", "import sys,tkinter;print(sys.version.split()[0],"
                                   "'Tk',tkinter.TkVersion)"],
                        capture_output=True, text=True)
    print("版本   :", (rc.stdout or "").strip())

    if args.clean:
        for d in (DIST, BUILD):
            shutil.rmtree(d, ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)

    results = []
    for name, entry in APPS:
        out = build_one(py, name, entry)
        results.append(out)

    print("\n" + "=" * 64)
    for p in results:
        print("产出 %-22s %7.1f MB" % (p.name, p.stat().st_size / 1048576))
    print("=" * 64)
    print("交付时把 dist/ 下两个 exe 与 payload/ 放在**同一目录**：")
    print("  <发布目录>\\智途安装程序.exe")
    print("  <发布目录>\\智途监控.exe")
    print("  <发布目录>\\payload\\...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
