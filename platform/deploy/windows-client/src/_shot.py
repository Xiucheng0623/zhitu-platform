#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一个 tkinter 窗口单独截下来（只截它自己的矩形，不抓整屏）。

为什么不用全屏截图：开发机上还有其他窗口在前台，全屏抓到的是别人的界面，
看起来"什么都没画出来"，容易误判成 UI 崩了。

两个必须绕开的坑（都实测踩过）
----------------------------------------------------------------------------
1. **窗口位置**：Windows 把未激活窗口放在 (-32000,-32000) 这个哨兵坐标。
   按 winfo_rootx 去抓，得到的是窗口外的一片桌面（黑图）。
2. **前台抢占**：用 update() 泵事件时，窗口可能根本没被真正映射到前台，
   截图抓到的是别人的窗口（本次实测：抓到了编辑器自己的窗口）。
   对策：用 after() + mainloop() 走正常事件循环，再按 HWND 用 PrintWindow
   直接取窗口内容 —— 它对遮挡免疫，不要求窗口在最前面。

用法：
    python _shot.py monitor  D:\\out.png
    python _shot.py setup    D:\\out.png
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

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
    """Tk 的 winfo_id() 给的是子 HWND，真正的顶层框要往上找。

    不能只用 GetAncestor(GA_ROOT)：改过 geometry 之后，winfo_id() 有几率指向
    一个刚被重建的小窗口（实测返回 237x39，抓出来是一块碎片）。这里沿
    GetParent 一层层往上，取**尺寸最接近 Tk 自己报的窗口尺寸**的那个。
    """
    want_w, want_h = root.winfo_width(), root.winfo_height()
    hwnd = int(user32.GetAncestor(wintypes.HWND(root.winfo_id()), GA_ROOT)
               or root.winfo_id())
    best, best_err = hwnd, None
    cur = int(root.winfo_id())
    for _ in range(5):
        r = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(cur), ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        # 顶层框比客户区多出标题栏与边框，用"不小于且尽量接近"来打分
        if w >= want_w and h >= want_h:
            err = (w - want_w) + (h - want_h)
            if best_err is None or err < best_err:
                best, best_err = cur, err
        parent = int(user32.GetParent(wintypes.HWND(cur)) or 0)
        if not parent:
            break
        cur = parent
    return best


def grab_hwnd(hwnd: int) -> Image.Image:
    """按 HWND 取窗口位图。PrintWindow 对遮挡免疫，不需要窗口在最前面。"""
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
        # 2 = PW_RENDERFULLCONTENT：DWM 合成的内容也能取到
        if not user32.PrintWindow(wintypes.HWND(hwnd), memdc, PW_RENDERFULLCONTENT):
            gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, SRCCOPY)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负 = 自上而下，否则图是倒的
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0      # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)


def grab(app, out: str, wait_s: float = 8.0) -> None:
    root = app.root
    root.attributes("-topmost", True)
    root.deiconify()

    def shoot() -> None:
        try:
            hwnd = hwnd_of(root)
            img = grab_hwnd(hwnd)
            img.save(out)
            print("saved", out, img.size, "logical",
                  (root.winfo_width(), root.winfo_height()), "hwnd", hwnd)
        except Exception as exc:  # noqa: BLE001
            print("FAILED:", exc)
        finally:
            root.quit()

    # 用正常事件循环跑够时间：后台线程的状态要回来、after() 轮询要跑几轮，
    # 界面才是"真实状态"而不是骨架（health 三次各 2s 超时，等太短会抓到"正在检查…"）。
    root.after(int(wait_s * 1000), shoot)
    root.mainloop()
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    out = sys.argv[2] if len(sys.argv) > 2 else "D:/shot.png"
    if which == "monitor":
        import zhitu_monitor as m
        grab(m.MonitorApp(), out)
    else:
        import zhitu_setup as s
        grab(s.SetupApp(), out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
