#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把原生版的两个窗口各截一张（不依赖 Pillow，PNG 自己写）。

为什么要自己写 PNG：本机有 tkinter 的解释器（Python311）里没有 Pillow，
而有 Pillow 的托管解释器里没有 tkinter —— 两个凑不到一起。PNG 只需要 zlib，
直接按行打包即可。

为什么不用全屏截图：开发机上别的窗口在前台，全屏抓到的是别人的界面，
会被误判成"界面崩了"。这里按 HWND 用 PrintWindow(PW_RENDERFULLCONTENT) 取，
对遮挡免疫，也不要求窗口在最前面。

用法：
    python tools/shot_win.py setup   D:\\shot-setup.png
    python tools/shot_win.py monitor D:\\shot-monitor.png
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 2
GA_ROOT = 2
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def hwnd_of(root) -> int:
    """winfo_id() 给的是子 HWND，沿 GetParent 找尺寸最接近 Tk 自报值的顶层框。"""
    want_w, want_h = root.winfo_width(), root.winfo_height()
    hwnd = int(user32.GetAncestor(wintypes.HWND(root.winfo_id()), GA_ROOT)
               or root.winfo_id())
    best, best_err = hwnd, None
    cur = int(root.winfo_id())
    for _ in range(5):
        r = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(cur), ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w >= want_w and h >= want_h:
            err = (w - want_w) + (h - want_h)
            if best_err is None or err < best_err:
                best, best_err = cur, err
        parent = int(user32.GetParent(wintypes.HWND(cur)) or 0)
        if not parent:
            break
        cur = parent
    return best


def _from_screen(rect):
    """直接从屏幕对应区域 BitBlt。PrintWindow 拿到空白时的兜底。"""
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    try:
        gdi32.BitBlt(memdc, 0, 0, w, h, hdc, rect.left, rect.top, SRCCOPY)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return buf.raw
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdc)


def _blank(bgra: bytes, w: int, h: int) -> bool:
    """判断是不是"一片纯色"：每隔一段采样，看不同取值的种类数。"""
    step = max(4, (w * h // 4000) * 4 // 4) * 4
    seen = set()
    for i in range(0, len(bgra) - 4, step):
        seen.add(bgra[i:i + 3])
        if len(seen) > 24:
            return False
    return True


def grab_rgb(hwnd: int):
    rect = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("窗口尺寸为 0（可能被最小化）")
    hdc = user32.GetWindowDC(wintypes.HWND(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    try:
        if not user32.PrintWindow(wintypes.HWND(hwnd), memdc, PW_RENDERFULLCONTENT):
            gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, SRCCOPY)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h              # 负 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return w, h, buf.raw
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)


def write_png(path: str, w: int, h: int, bgra: bytes) -> None:
    rows = []
    for y in range(h):
        row = bgra[y * w * 4:(y + 1) * w * 4]
        rgb = bytearray(w * 3)
        # BGRA -> RGB（丢掉 alpha）
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rows.append(b"\x00" + bytes(rgb))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def grab(app, out: str, wait_s: float = 6.0) -> None:
    root = app.root
    root.attributes("-topmost", True)
    root.deiconify()

    def shoot() -> None:
        try:
            hwnd = hwnd_of(root)
            w, h, bgra = grab_rgb(hwnd)
            if _blank(bgra, w, h):
                # PrintWindow 有时候只给回一帧空白（窗口还没被 DWM 合成完），
                # 这时把窗口顶到最前，直接抓屏幕对应区域。
                print("  PrintWindow 拿到空白，改用屏幕区域抓取")
                root.lift()
                root.focus_force()
                root.update()
                time.sleep(1.0)
                rect = wintypes.RECT()
                user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
                bgra = _from_screen(rect)
            write_png(out, w, h, bgra)
            print("saved %s  %dx%d  (tk %dx%d)"
                  % (out, w, h, root.winfo_width(), root.winfo_height()))
        except Exception as exc:  # noqa: BLE001
            print("FAILED: %s" % exc)
        finally:
            root.quit()

    root.after(int(wait_s * 1000), shoot)
    root.mainloop()
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "setup"
    out = sys.argv[2] if len(sys.argv) > 2 else "D:/shot.png"
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    if which == "monitor":
        import zn_monitor as m
        grab(m.MonitorApp(), out, wait_s=wait or 16.0)
    else:
        import zn_setup as s
        grab(s.SetupApp(), out, wait_s=wait or 6.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
