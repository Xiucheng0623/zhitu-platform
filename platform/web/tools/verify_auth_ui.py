"""
FORGE · 前端身份流程端到端验证
================================================================
不是"看一眼截图觉得对"，而是真的在浏览器里把手机号输入、点登录、
读回 DOM，把「登录后落地在哪一页、顶栏显示谁、有没有被强制改密拦住」
变成可断言的结论。

为什么不用 Playwright / Puppeteer：
  本机装它们要走 npm 网络，而且这两个库的版本与 Edge 的 CDP 协议
  偶有错配。这里只用到 4 个 CDP 域（Page / Runtime / Input / Network），
  手写一个最小 WebSocket 客户端足够，且零额外依赖。

用法
----
  # 先起后端（native 模式）与前端 preview，然后：
  python tools/verify_auth_ui.py --phone 13800138000 --password 'ZhangWei2026Strong'

  # 只验证「未登录会被弹回登录页」
  python tools/verify_auth_ui.py --anon-only

  # 输出目录（默认 ./_shots）
  python tools/verify_auth_ui.py --out D:/tmp/shots
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# 复用 LS 品牌验证脚本里已经写好并跑通过的最小 CDP 客户端，
# 避免同一套代码在本仓库里存在两份实现。
_BRAND_DIR = Path(__file__).resolve().parents[2] / "labelstudio" / "brand"
sys.path.insert(0, str(_BRAND_DIR))
from verify_brand import CDP, WS, find_browser  # noqa: E402

WEB = "http://127.0.0.1:9121"
PORT = 9333


class Browser:
    def __init__(self, exe: str, out: Path):
        self.out = out
        self.profile = tempfile.mkdtemp(prefix="forge-uidrv-")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--remote-debugging-port={PORT}", f"--user-data-dir={self.profile}",
             "--window-size=1440,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.cdp = CDP(WS(self._ws_url()))
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")

    def _ws_url(self, timeout: float = 20.0) -> str:
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                    for t in json.loads(r.read()):
                        if t.get("type") == "page":
                            return t["webSocketDebuggerUrl"]
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(0.3)
        raise SystemExit(f"连不上 Edge 调试端口：{last}")

    def js(self, expr: str):
        return self.cdp.eval_js(expr)

    def goto(self, url: str, settle: float = 1.6) -> None:
        self.cdp.call("Page.navigate", url=url)
        time.sleep(settle)

    def type_into(self, selector: str, text: str) -> None:
        ok = self.js(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return false; el.focus(); el.select?.(); return true; })()"
            % json.dumps(selector)
        )
        if not ok:
            raise RuntimeError(f"找不到输入框 {selector}")
        self.cdp.call("Input.insertText", text=text)

    def click_text(self, text: str) -> bool:
        return bool(self.js(
            "(() => { const b = [...document.querySelectorAll('button, a')]"
            ".find(x => (x.textContent || '').trim() === %s);"
            " if (!b) return false; b.click(); return true; })()" % json.dumps(text)
        ))

    def shot(self, name: str) -> Path:
        r = self.cdp.call("Page.captureScreenshot", format="png")
        p = self.out / f"{name}.png"
        p.write_bytes(base64.b64decode(r["data"]))
        return p

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.profile, ignore_errors=True)


RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="13800138000")
    ap.add_argument("--password", default="")
    ap.add_argument("--web", default=WEB)
    ap.add_argument("--out", default="_shots")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--anon-only", action="store_true",
                    help="只验证未登录被拦，不跑登录")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    b = Browser(find_browser(args.browser), out)
    try:
        # --- 1. 未登录直接进工作台，必须被弹回登录页 -------------------------
        b.goto(f"{args.web}/#/")
        hash_now = b.js("location.hash")
        check(hash_now.startswith("#/login"), "未登录访问 / 会重定向到登录页",
              f"location.hash = {hash_now!r}")

        body = b.js("document.body.innerText") or ""
        check("手机号" in body, "登录页出现「手机号」字段而不是邮箱")
        check("企业邮箱" not in body, "登录页已移除「企业邮箱」字样")
        check("演示环境" not in body and "跳过认证" not in body,
              "登录页已移除演示/跳过认证入口")

        if args.anon_only:
            b.shot("01-login-anon")
            return report(b, out)

        if not args.password:
            raise SystemExit("需要 --password（或加 --anon-only 只测未登录拦截）")

        # --- 2. 手机号登录 ---------------------------------------------------
        b.type_into("input[name=phone]", args.phone)
        b.type_into("input[name=password]", args.password)
        b.shot("01-login-filled")
        clicked = b.click_text("登录")
        check(clicked, "找到并点击了「登录」按钮")
        time.sleep(2.5)

        hash_now = b.js("location.hash") or ""
        body = b.js("document.body.innerText") or ""
        err = b.js("(document.querySelector('.fk-error') || {}).innerText || ''")

        landed_change = hash_now.startswith("#/change-password")
        landed_home = hash_now in ("#/", "#", "") or hash_now.startswith("#/overview")
        if err:
            check(False, "登录成功", f"页面报错：{err}")
        elif landed_change:
            check(True, "登录成功且被强制跳转到改密页", f"location.hash = {hash_now!r}")
        elif landed_home:
            check(True, "登录成功并进入工作台", f"location.hash = {hash_now!r}")
        else:
            check(False, "登录成功", f"停在 {hash_now!r}")

        # --- 3. 顶栏身份来自服务端 -------------------------------------------
        if landed_home:
            side = b.js("(document.querySelector('.shell__user') || {}).innerText || ''")
            check("138****8000" in side or args.phone[-4:] in side,
                  "侧栏显示服务端返回的手机号", side.replace("\n", " / ")[:80])
            scope = b.js("(document.querySelector('.shell__mode') || {}).innerText || ''")
            check("视角" in scope, "顶栏视角标识存在且为只读文案", scope.replace("\n", " "))
            b.shot("02-overview")
        else:
            b.shot("02-change-password")
            # 改密页至少要有三个密码框和一个提交按钮
            n = b.js("document.querySelectorAll('input[type=password]').length")
            check(n == 3, "改密页有三个密码输入框", f"实际 {n}")

        # --- 4. 退出登录回到登录页 -------------------------------------------
        if landed_home:
            ok = b.js(
                "(() => { const b = document.querySelector('[title=\"退出登录\"]');"
                " if (!b) return false; b.click(); return true; })()"
            )
            time.sleep(1.2)
            h = b.js("location.hash") or ""
            check(bool(ok) and h.startswith("#/login"), "退出登录后回到登录页", f"hash={h!r}")
            tok = b.js("localStorage.getItem('forge.platform.token')")
            check(not tok, "退出后 localStorage 里的 token 已清除", f"token={tok!r}")
    except Exception as exc:  # noqa: BLE001
        check(False, "执行过程未抛异常", f"{type(exc).__name__}: {exc}")
    finally:
        pass
    return report(b, out)


def report(b: Browser, out: Path) -> int:
    ok = sum(1 for r in RESULTS if r[0])
    bad = len(RESULTS) - ok
    print()
    print(f"结果：{ok} 通过 / {bad} 失败（截图在 {out}）")
    b.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
