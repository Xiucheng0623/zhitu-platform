#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
真实浏览器回归：全站真实数据 + 标注工作台整页直达 + LS 品牌外壳
============================================================================
2026-11-06 重写。旧版验证的是「iframe 里能不能免二次登录」，那套交互已经被
用户否掉了：现在是点侧栏「标注工作台」→ 整页导航进 LS → 品牌启动态 →
右下角「返回主工作台」。

检查项：
  1. 平台 11 个业务页面里不允许再出现任何演示/虚构数据字样
  2. 侧栏点击「标注工作台」后主文档 URL 变成 8080，平台壳不再存在
  3. 没有 LS 登录页 / 注册页 / 票据中转页
  4. LS 侧品牌外壳（启动态 CSS / 返回按钮 / spinner 覆盖）真实生效
  5. 返回按钮能回到平台

用法：
    python platform/web/tools/verify_ls_sso_ui.py --out D:/forge-ls/shots
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

_BRAND_DIR = Path(__file__).resolve().parents[2] / "labelstudio" / "brand"
sys.path.insert(0, str(_BRAND_DIR))
from verify_brand import CDP, WS, find_browser  # noqa: E402

API = "http://127.0.0.1:8300"
WEB = "http://127.0.0.1:9121"
LS = "http://127.0.0.1:8080"
PORT = 9341

RESULTS: list[tuple[bool, str, str]] = []

# 页面里一旦出现这些词，就说明还有没清干净的虚构内容
FORBIDDEN = [
    "演示数据", "演示画布", "初始种子", "本地确定性种子", "重置演示",
    "8.1%", "抽检 120 张", "vehicle.car", "陆一鸣", "10.24.3.18",
    "预期损失", "已用 128", "ls.internal", "s3://forge-prod-data",
    "Keycloak", "预计 40 秒完成", "已生成供应商邀请链接",
]

PAGES = [
    ("/", "工作台"),
    ("/projects", "项目管理"),
    ("/tasks", "任务管理"),
    ("/data", "数据管理"),
    ("/review", "审核中心"),
    ("/suppliers", "供应商管理"),
    ("/portal", "供应商门户"),
    ("/people", "人员管理"),
    ("/performance", "绩效中心"),
    ("/settlement", "结算中心"),
    ("/settings", "系统设置"),
]


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def platform_token(phone: str, password: str) -> str:
    req = urllib.request.Request(
        f"{API}/api/v1/auth/login",
        data=json.dumps({"phone": phone, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def expected_ls_project(token: str) -> int | None:
    """按 pickWorkbenchTask 的同一套规则算出应该进哪个 LS 项目。"""
    req = urllib.request.Request(
        f"{API}/api/v1/tasks?limit=500",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())["rows"]
    rank = {"in_progress": 0, "assigned": 1, "rework": 2, "pending": 3,
            "submitted": 4, "reviewing": 5}
    with_ls = [x for x in rows if x.get("ls_project_id") is not None]
    newest_first = sorted(with_ls, key=lambda x: x.get("created_at") or "", reverse=True)
    best = sorted(newest_first, key=lambda x: rank.get(x.get("status"), 9))
    return best[0]["ls_project_id"] if best else None


class Browser:
    def __init__(self, exe: str):
        self.profile = tempfile.mkdtemp(prefix="forge-full-ui-")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--remote-debugging-port={PORT}", f"--user-data-dir={self.profile}",
             "--window-size=1600,1000", "about:blank"],
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
        raise SystemExit(f"连不上浏览器调试端口：{last}")

    def js(self, expr: str, retries: int = 3):
        for i in range(retries):
            try:
                return self.cdp.eval_js(expr)
            except Exception:  # noqa: BLE001
                if i == retries - 1:
                    raise
                time.sleep(0.6)

    def goto(self, url: str, settle: float = 2.0) -> None:
        self.cdp.call("Page.navigate", url=url)
        time.sleep(settle)

    def frames(self) -> list[str]:
        tree = self.cdp.call("Page.getFrameTree")["frameTree"]

        def walk(node, out):
            out.append(node["frame"].get("url", ""))
            for child in node.get("childFrames", []) or []:
                walk(child, out)
        out: list[str] = []
        walk(tree, out)
        return out

    def shot(self, path: Path) -> None:
        r = self.cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        path.write_bytes(base64.b64decode(r["data"]))

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.profile, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="13800138001")
    ap.add_argument("--password", default="Hairong@2026")
    ap.add_argument("--out", default="D:/forge-ls/shots")
    ap.add_argument("--web", default=WEB)
    ap.add_argument("--browser", default=None)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    pages_dir = out / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    token = platform_token(args.phone, args.password)
    b = Browser(find_browser(args.browser))
    try:
        # --- 0. 登录页 -------------------------------------------------------
        b.goto(f"{args.web}/#/login", 2.2)
        leftovers = b.js(
            "(() => ['.login__eyebrow','.login__caps','.login__env']"
            ".filter(s => document.querySelector(s)).join(','))()"
        ) or ""
        check(leftovers == "", "登录页三处装饰元素已删除", f"残留={leftovers!r}")
        b.shot(out / "01-login.png")

        # --- 1. 注入 token，真重载让 AuthProvider 重新鉴权 -------------------
        b.js("localStorage.setItem('forge.platform.token', %s)" % json.dumps(token))
        b.goto(f"{args.web}/?full={int(time.time())}#/", 3.5)
        check("#/login" not in (b.js("location.hash") or ""),
              "平台侧保持登录态", f"hash={b.js('location.hash')!r}")

        # --- 1b. 未接入的装饰按钮不能静默、更不能假装成功 ---------------------
        b.js(
            "(() => { const btn=[...document.querySelectorAll('button')]"
            ".find(x=>x.textContent.trim()==='导出日报');"
            " if(btn) btn.click(); return !!btn; })()"
        )
        time.sleep(0.5)
        after_click = b.js("document.body.innerText") or ""
        check("该能力尚未接入" in after_click,
              "未接入按钮给出诚实反馈而非假成功",
              (after_click[-120:].replace("\n", " ")))

        # --- 2. 遍历业务页面，查虚构数据残留 ---------------------------------
        dirty: list[str] = []
        for path, label in PAGES:
            b.goto(f"{args.web}/#/{path.lstrip('/')}", 1.8)
            body = b.js("document.body.innerText") or ""
            hits = [w for w in FORBIDDEN if w in body]
            if hits:
                dirty.append(f"{label}: {hits}")
            b.shot(pages_dir / f"{path.strip('/') or 'overview'}.png")
        check(not dirty, "11 个业务页面无演示/虚构数据残留", "; ".join(dirty) or "全部干净")

        # 顶栏告警应显示真实状态，而不是「演示数据」
        top = b.js("document.querySelector('.shell__top')?.innerText || ''") or ""
        check("演示数据" not in top, "顶栏不再挂「演示数据」标记", top[:80].replace("\n", " "))

        # --- 3. 点侧栏「标注工作台」→ 整页导航 -------------------------------
        b.goto(f"{args.web}/?click={int(time.time())}#/", 2.5)
        clicked = b.js(
            "(() => { const a=[...document.querySelectorAll('.shell__link')]"
            ".find(x=>x.textContent.includes('标注工作台'));"
            " if(!a) return 'NOT_FOUND'; a.click(); return 'CLICKED'; })()"
        )
        check(clicked == "CLICKED", "侧栏「标注工作台」可被点击", str(clicked))

        # 导航一发生就抓品牌启动态（它至少停留 800ms，晚了就撤掉了）。
        # 注意：location.href 在 window.location.assign 调用后立刻变成目标地址，
        # 但 document 仍是平台旧页面。必须等 LS 文档里真的出现外壳节点才算到位。
        deadline = time.time() + 18
        while time.time() < deadline:
            try:
                ready = b.js(
                    "(() => location.href.indexOf('127.0.0.1:8080') >= 0 &&"
                    " !!document.querySelector('#forge-wb-shell-css'))()"
                )
            except Exception:  # noqa: BLE001 — 导航中 execution context 会被销毁
                ready = False
            if ready:
                break
            time.sleep(0.08)
        boot = b.js(
            "(async () => { let e=null;"
            " for (let i=0;i<12;i++){ e=document.querySelector('#forge-wb-boot');"
            " if(e) break; await new Promise(r=>setTimeout(r,40)); }"
            " const css=document.querySelector('#forge-wb-shell-css');"
            " const t=css?css.textContent:'';"
            " const anim=[...document.querySelectorAll("
            "  '.lsf-spinner-ls,.lsf-spinner-ls__body,[class*=\"spinner\"]>[class*=\"body\"],' +"
            "  'svg path.spike-fill, svg path.spike-stroke')]"
            "  .filter(x=>getComputedStyle(x).display!=='none').length;"
            " return JSON.stringify({"
            "  exists: !!e, opacity: e?getComputedStyle(e).opacity:'',"
            "  text: e?e.innerText.replace(/\\s+/g,' ').trim():'',"
            "  logo: !!document.querySelector('#forge-wb-boot .f-logo'),"
            "  bar: t.includes('forge-wb-slide'), visibleAnim: anim"
            " }); })()"
        ) or "{}"
        boot_info = json.loads(boot)
        b.shot(out / "02a-brand-boot.png")
        check(bool(boot_info.get("exists")), "整页进入时能看到品牌启动态", str(boot_info.get("text")))
        # 原版动物/方块加载动画现在是被整层隐藏，而不是被品牌 LOGO 盖住。
        check(bool(boot_info.get("logo")) and bool(boot_info.get("bar"))
              and int(boot_info.get("visibleAnim") or 0) == 0,
              "品牌启动态接管首屏，LS 原版加载动画已移除",
              f"logo={boot_info.get('logo')} bar={boot_info.get('bar')} "
              f"可见原版动画={boot_info.get('visibleAnim')}")
        time.sleep(7.0)

        href = b.js("location.href") or ""
        check("127.0.0.1:8080" in href, "点击后整页进入 LS（主文档已换 origin）", href)
        check("/projects/" in href and "/data" in href,
              "落在标注流 /projects/N/data", href)
        want = expected_ls_project(token)
        check(want is not None and f"/projects/{want}/data" in href,
              "默认进入的是可作业的最新批次", f"期望 #{want}，实际 {href}")
        check("labeling=1" in href, "直接进入标注流而非数据管理表格", href)
        check("9121" not in href, "平台外壳已不在当前文档", href)

        frames = b.frames()
        shell_frames = [f for f in frames if "127.0.0.1:9121" in f]
        check(not shell_frames, "没有任何平台 iframe 嵌套", str(shell_frames))
        check(not any("/user/login" in f or "/user/signup" in f or "/forge/sso" in f
                      for f in frames),
              "没有 LS 登录页 / 注册页 / 票据中转页", str(frames))

        # --- 4. LS 品牌外壳 --------------------------------------------------
        shell = b.js(
            "(() => { const back=document.querySelector('#forge-wb-back');"
            " return JSON.stringify({"
            "  css: !!document.querySelector('#forge-wb-shell-css'),"
            "  js: !!document.querySelector('#forge-wb-shell-js'),"
            "  boot: !!document.querySelector('#forge-wb-boot'),"
            "  back: !!back,"
            "  backHref: back ? back.href : '',"
            "  backFixed: back ? getComputedStyle(back).position : '',"
            "  backText: back ? back.innerText.trim() : '',"
            "  brand: (document.body.innerText||'').includes('智途'),"
            "  lsLeftover: (document.body.innerText||'').includes('Label Studio'),"
            "  iframes: [...document.querySelectorAll('iframe')].map(f=>f.src)"
            " }); })()"
        ) or "{}"
        info = json.loads(shell)
        check(bool(info.get("css")), "LS 页面已注入品牌外壳 CSS", shell[:200])
        check(bool(info.get("back")), "存在「返回主工作台」入口", str(info.get("backText")))
        check("9121" in str(info.get("backHref", "")), "返回按钮指向平台地址", str(info.get("backHref")))
        check(info.get("backFixed") == "fixed", "品牌外壳 CSS 真实生效（未被 CSP 拦）", str(info.get("backFixed")))
        check(not any("127.0.0.1:9121" in (u or "") for u in (info.get("iframes") or [])),
              "LS 页面内没有反向嵌套平台", str(info.get("iframes")))
        check(not info.get("lsLeftover"), "工作台界面无 Label Studio 原版字样",
              "残留=True" if info.get("lsLeftover") else "已清除")
        b.shot(out / "02-workbench-fullpage.png")

        # --- 5. 返回平台 -----------------------------------------------------
        b.js("(() => { const a=document.querySelector('#forge-wb-back'); if(a) a.click(); return !!a; })()")
        time.sleep(3.5)
        back_href = b.js("location.href") or ""
        check("127.0.0.1:9121" in back_href, "返回按钮回到平台", back_href)
        b.shot(out / "03-back-to-platform.png")

        print("\n截图：", out, "·", pages_dir)
    finally:
        b.close()

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
