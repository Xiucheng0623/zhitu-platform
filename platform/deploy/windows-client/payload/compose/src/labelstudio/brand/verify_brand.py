#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FORGE · Label Studio 品牌改造自检
============================================================================

改完 UI 必须自己看一眼，不能只读模板就宣布完成。这个脚本做三件事：

  1. 用表单登录拿到 sessionid（stdlib，不依赖 requests/httpx）
  2. 起一个无头浏览器，用 CDP 注入 Cookie —— 因为标注工作台要登录才能看
  3. 导航到目标页，等 React 水合完成，输出品牌断言并截图

为什么要用 CDP 而不是 `browser --screenshot url`：
    标注工作台是登录后才能访问的页面，命令行截图没法带 Cookie。
    CDP 的 Network.setCookie 可以直接把 sessionid 塞进浏览器上下文。

用法
----------------------------------------------------------------------------
    python verify_brand.py                                   # 默认查登录页
    python verify_brand.py --page project --project 1         # 查标注工作台
    python verify_brand.py --page project --out D:/shot.png

依赖：只用标准库（socket 手写最小 WebSocket 客户端）+ 本机 Edge/Chrome。
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（只够跑 CDP）
# ---------------------------------------------------------------------------
class WS:
    def __init__(self, url: str, timeout: float = 30.0):
        if not url.startswith("ws://"):
            raise ValueError(f"只支持 ws://，收到 {url}")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("WS 握手连接被关闭")
            buf += chunk
        status = buf.split(b"\r\n")[0]
        if b"101" not in status:
            raise IOError(f"WS 握手失败: {status!r}")

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("WS 连接已关闭")
            buf += chunk
        return buf

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        n = len(data)
        hdr = bytearray([0x81])  # FIN + text
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        # 客户端 → 服务端必须掩码
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> dict:
        while True:
            b1, b2 = self._exact(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            masked = b2 & 0x80
            mask = self._exact(4) if masked else None
            payload = self._exact(n) if n else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:
                return json.loads(payload)
            if opcode == 0x8:
                raise IOError("WS 被对端关闭")
            # ping/pong/binary 忽略

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class CDP:
    def __init__(self, ws: WS):
        self.ws = ws
        self._id = 0

    def call(self, method: str, **params) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send({"id": mid, "method": method, "params": params})
        while True:
            msg = self.ws.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result", {})

    def eval_js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def login(base: str, email: str, password: str) -> list[dict]:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login_url = f"{base}/user/login"
    html = opener.open(login_url, timeout=15).read().decode("utf-8", "replace")
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not m:
        raise SystemExit("登录页里找不到 csrfmiddlewaretoken —— LS 版本或模板又变了")
    data = urllib.parse.urlencode({
        "csrfmiddlewaretoken": m.group(1),
        "email": email,
        "password": password,
        "persist_session": "on",
    }).encode()
    req = urllib.request.Request(login_url, data=data, method="POST")
    req.add_header("Referer", login_url)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener.open(req, timeout=15).read()
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
               for c in jar]
    if not any(c["name"] == "sessionid" for c in cookies):
        raise SystemExit("登录没拿到 sessionid，账号密码不对？")
    return cookies


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------
def find_browser(explicit: str | None) -> str:
    if explicit:
        return explicit
    for p in BROWSERS:
        if Path(p).is_file():
            return p
    raise SystemExit("找不到 Chrome/Edge，用 --browser 指定可执行文件路径")


def wait_json(url: str, timeout: float = 25.0) -> dict:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.4)
    raise SystemExit(f"等不到 {url}: {last}")


@contextlib.contextmanager
def browser_session(browser: str, cookies: list[dict],
                    width: int = 1600, height: int = 1000, port: int = 9333):
    """起一个无头浏览器 + 注入 Cookie，yield 一个可 eval_js 的 CDP 会话。

    抽出来是因为扫描脚本（scan_zh_coverage.py）也要一模一样的这套东西——
    「起浏览器 / 等调试端口 / 拿 page target / 塞 Cookie / 设视口」这五步抄两遍
    迟早会漂移，出问题还得改两处。
    """
    profile = tempfile.mkdtemp(prefix="forge-cdp-")
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         f"--window-size={width},{height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_json(f"http://127.0.0.1:{port}/json/version")
        targets = wait_json(f"http://127.0.0.1:{port}/json")
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            raise SystemExit("没有可用的 page target")
        cdp = CDP(WS(page["webSocketDebuggerUrl"]))
        cdp.call("Network.enable")
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        for c in cookies:
            cdp.call("Network.setCookie",
                     name=c["name"], value=c["value"],
                     domain=c["domain"] or "127.0.0.1", path=c["path"] or "/")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 width=width, height=height, deviceScaleFactor=2, mobile=False)
        yield cdp
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def navigate(cdp: CDP, url: str, settle: float = 6.0) -> None:
    """导航并等 React 水合完成（readyState=complete 之后再静置 settle 秒）。"""
    cdp.call("Page.navigate", url=url)
    end = time.time() + 40
    while time.time() < end:
        if cdp.eval_js("document.readyState") == "complete":
            break
        time.sleep(0.3)
    time.sleep(settle)


def screenshot(browser: str, url: str, out: Path, cookies: list[dict],
               width: int = 1600, height: int = 1000, settle: float = 6.0) -> dict:
    with browser_session(browser, cookies, width, height) as cdp:
        navigate(cdp, url, settle)

        report = cdp.eval_js("""(() => ({
            title: document.title,
            url: location.href,
            lsLogo: document.querySelectorAll(
                'img[alt="Label Studio Logo"], svg[alt="Label Studio Logo"], svg.lsf-menu-header__logo'
            ).length,
            forgeLogo: document.querySelectorAll('.forge-brand-logo').length,
            emptyMark: document.querySelectorAll('.forge-empty-mark').length,
            panelSign: document.querySelectorAll('.forge-panel-sign').length,
            zhEntries: (window.__FORGE_ZH__ || {}).entries || 0,
            bodyHasLabelStudio: /Label Studio/.test(document.body.innerText || ""),
            enHeaders: (() => {
                // 数据管理表格的表头。表头和数据格共用 .lsf-table__cell，
                // 数据格按设计跳过（任务正文），表头必须翻掉。
                const bad = [];
                document.querySelectorAll('.lsf-table__cell.lsf-th').forEach(c => {
                    const t = (c.innerText || '').trim();
                    if (/^[A-Za-z][A-Za-z ]{2,}$/.test(t)) bad.push(t);
                });
                return bad.join(",");
            })(),
        }))()""")

        shot = cdp.call("Page.captureScreenshot", format="png")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(shot["data"]))
        report["screenshot"] = str(out)
        report["bytes"] = out.stat().st_size

        # ---- 打开左侧抽屉再断言一次 --------------------------------------------
        # ★ 为什么必须点开：菜单是「打开才渲染」的（关闭时 ul.lsf-main-menu 根本
        #   不在 DOM 里）。早期脚本在关闭状态下统计 a[href*=labelstud.io]，永远
        #   得到 0 —— 断言通过，但菜单里那三条外链其实一直都在。
        opened = cdp.eval_js("""(() => {
            const t = document.querySelector('.lsf-menu-header__trigger');
            if (!t) return false;
            t.click();
            return true;
        })()""")
        if opened:
            time.sleep(1.2)
            menu = cdp.eval_js("""(() => {
                const brand = document.querySelector('.forge-menu-brand');
                return {
                    menuOpen: !!document.querySelector('ul.lsf-main-menu'),
                    menuBrand: document.querySelectorAll('.forge-menu-brand').length,
                    menuBrandText: brand ? (brand.innerText || '').replace(/\\s+/g, ' ').trim() : "",
                    extLinks: document.querySelectorAll(
                        'a[href*="labelstud.io"], a[href*="humansignal"],'
                        + ' a[href*="github.com/HumanSignal"], a[href*="github.com/heartexlabs"]'
                    ).length,
                };
            })()""") or {}
            report.update(menu)
            shot2 = cdp.call("Page.captureScreenshot", format="png")
            out2 = out.with_name(out.stem + "-menu" + out.suffix)
            out2.write_bytes(base64.b64decode(shot2["data"]))
            report["screenshot_menu"] = str(out2)
        else:
            report["menuOpen"] = False
        return report


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE Label Studio 品牌自检")
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--email", default="admin@forge.local")
    ap.add_argument("--password", default=os.environ.get("LS_PASSWORD", "ForgeAdmin2026!"))
    ap.add_argument("--page", choices=["login", "project"], default="login")
    ap.add_argument("--project", default="1")
    ap.add_argument("--out", default=None)
    ap.add_argument("--browser", default=None)
    args = ap.parse_args()

    browser = find_browser(args.browser)
    out = Path(args.out or (
        "D:/forge-shots/ls-brand-login.png" if args.page == "login"
        else f"D:/forge-shots/ls-brand-project{args.project}.png"))

    if args.page == "login":
        url = f"{args.base}/user/login"
        cookies: list[dict] = []
    else:
        url = f"{args.base}/projects/{args.project}/data"
        cookies = login(args.base, args.email, args.password)
        print(f"登录成功，Cookie: {', '.join(c['name'] for c in cookies)}")

    print(f"browser = {browser}")
    print(f"url     = {url}")
    rep = screenshot(browser, url, out, cookies)
    print("-" * 70)
    for k in ("title", "url", "lsLogo", "forgeLogo", "emptyMark", "panelSign",
              "zhEntries", "bodyHasLabelStudio", "enHeaders",
              "menuOpen", "menuBrand", "menuBrandText", "extLinks",
              "screenshot", "bytes", "screenshot_menu"):
        if k in rep:
            print(f"{k:20} = {rep[k]}")

    ok = True
    # 品牌从早期的英文 FORGE 改成中文「智途」后，标题也跟着中文化了。
    # 断言两个都认，否则脚本永远报一条假失败，真问题会被噪音淹没。
    _title = rep.get("title") or ""
    if not ("智途" in _title or "FORGE" in _title.upper()):
        print("!! 标题里没有品牌名（智途 / FORGE）")
        ok = False
    if rep.get("lsLogo"):
        print("!! 仍有 Label Studio 字标残留（运行时替换没生效）")
        ok = False
    if args.page == "project" and not rep.get("forgeLogo"):
        print("!! 标注工作台里没找到品牌字标（替换脚本没跑起来）")
        ok = False
    if rep.get("extLinks"):
        print("!! 仍有指向 labelstud.io / humansignal / 官方 GitHub 仓库的外链（菜单已展开后统计）")
        ok = False
    if args.page == "project" and rep.get("emptyMark", 0) > 1:
        print("!! .forge-empty-mark 出现多个 —— 空态图标替换在自我嵌套，检查幂等守卫")
        ok = False
    if args.page == "project" and rep.get("menuOpen") and not rep.get("menuBrand"):
        print("!! 侧栏菜单里没有品牌块（3c 没插进去）")
        ok = False
    if args.page == "project" and not rep.get("zhEntries"):
        print("!! 中文界面层没加载")
        ok = False
    if rep.get("enHeaders"):
        print(f"!! 表格列头仍是英文：{rep['enHeaders']}")
        ok = False
    print("\nRESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
