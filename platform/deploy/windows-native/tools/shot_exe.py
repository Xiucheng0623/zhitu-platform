#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""截「已经跑起来的真实 exe 窗口」——按标题找 HWND，不 import 源码。

和 shot_win.py 的区别：那边是"在进程内建窗口再自拍"，只能证明源码画得对；
这边是外部抓别人进程的窗口，能证明打出来的 exe 真的能开、画得一样。

用法：
    python tools/shot_exe.py "安装程序" D:\\shot-exe.png [等待秒数]
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shot_win as S  # noqa: E402

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def be_dpi_aware() -> float:
    """必须先开 DPI 感知，否则量到的是被系统虚拟化的坐标。

    本机 2560x1600 @150%，不开的话 GetSystemMetrics 报 1707x1067、
    GetWindowRect 也按 /1.5 报，于是 PrintWindow 只按那个小尺寸建位图，
    截出来就是"窗口右边和下边被切掉"的假象——UI 本身没坏。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
    except Exception:  # noqa: BLE001
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            return 1.0
    try:
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


def find_hwnd(needle: str):
    hits = []

    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if needle in title:
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            hits.append((int(hwnd), title, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return hits


def main() -> int:
    needle = sys.argv[1] if len(sys.argv) > 1 else "安装程序"
    out = sys.argv[2] if len(sys.argv) > 2 else "D:/shot-exe.png"
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
    s = be_dpi_aware()
    print("DPI 感知已开，缩放 %.2f，屏幕 %dx%d"
          % (s, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))

    deadline = time.time() + 25
    hits = []
    while time.time() < deadline:
        hits = find_hwnd(needle)
        if hits:
            break
        time.sleep(0.5)
    if not hits:
        print("没找到标题含 %r 的可见窗口" % needle)
        for h, t, w, hh in find_hwnd(""):
            print("   现有窗口: %s  (%dx%d)" % (t, w, hh))
        return 2

    # 取面积最大的那个（Tk 会有几个隐藏的辅助窗口）
    hwnd, title, w0, h0 = max(hits, key=lambda x: x[2] * x[3])
    print("命中: %r  %dx%d  hwnd=%d" % (title, w0, h0, hwnd))

    time.sleep(max(0.0, wait))
    w, h, bgra = S.grab_rgb(hwnd)
    if S._blank(bgra, w, h):
        print("  PrintWindow 空白，顶到最前后重抓")
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        time.sleep(1.5)
        w, h, bgra = S.grab_rgb(hwnd)
        if S._blank(bgra, w, h):
            r = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
            bgra = S._from_screen(r)
    S.write_png(out, w, h, bgra)
    print("saved %s  %dx%d" % (out, w, h))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
