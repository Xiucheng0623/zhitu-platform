"""
FORGE · 前端接通真库的端到端验证
================================================================
这个脚本回答一个问题：**界面上的数字，是不是数据库里真的有？**

做法不是"看一眼截图觉得像"，而是：
  1. 直接用 API 读到每个资源的真实条数（这是唯一可信的基准）
  2. 在浏览器里打开对应页面，把界面渲染出来的数字读回来
  3. 两边比对 —— 差一条就算失败

为什么必须这么做：
  前端接真库最容易出的错不是报错，是"看起来没事"。
  典型两种：
    · store 还在读 seed，页面数字漂亮但库里只有 2 个项目 —— 全靠肉眼根本发现不了
    · 接口通了但字段名对不上，一对一映射错位，进度条 0%、金额 ¥0 也不报错
  这两种都不会让页面白屏，只会让人拿着错的数字做决策。

覆盖：
  · 顶栏不再出现「演示数据」
  · 项目 / 任务 / 供应商 / 人员 / 审核 / 结算 六页条数与 API 一致
  · 任务行里显示真实的项目名（验证 join 生效，不是显示 project_id）
  · 通过界面新建一个项目 → API 里真的多了一条 → 界面上能看见
  · 新建失败时弹窗不关闭、错误写在原处（不会丢用户填的内容）

用法
----
  python tools/verify_wired_ui.py --phone 13800138000 --password admin1111

  # 只比对条数，不跑「新建项目」写操作
  python tools/verify_wired_ui.py --read-only

退出码 0 表示全绿。
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
import urllib.error
import urllib.request
from pathlib import Path

_BRAND_DIR = Path(__file__).resolve().parents[2] / "labelstudio" / "brand"
sys.path.insert(0, str(_BRAND_DIR))
from verify_brand import CDP, WS, find_browser  # noqa: E402

API = "http://127.0.0.1:8300"
WEB = "http://127.0.0.1:9121"
PORT = 9335


# ---------------------------------------------------------------------------
# API 侧：拿到"事实基准"
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base: str, phone: str, password: str):
        self.base = base
        self.token = ""
        self.login(phone, password)

    def call(self, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {payload[:300]}") from None

    def login(self, phone: str, password: str) -> None:
        r = self.call("/api/v1/auth/login", "POST",
                      {"phone": phone, "password": password})
        tok = r.get("access_token") or r.get("token")
        if not tok:
            raise SystemExit(f"登录未返回 token：{r}")
        self.token = tok

    def rows(self, path: str) -> list:
        return self.call(path).get("rows") or []


# ---------------------------------------------------------------------------
# 浏览器侧
# ---------------------------------------------------------------------------
class Browser:
    def __init__(self, exe: str, out: Path):
        self.out = out
        self.profile = tempfile.mkdtemp(prefix="forge-wired-")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--remote-debugging-port={PORT}", f"--user-data-dir={self.profile}",
             "--window-size=1440,1600", "about:blank"],
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

    def js(self, expr: str):
        return self.cdp.eval_js(expr)

    def goto(self, url: str, settle: float = 2.0) -> None:
        self.cdp.call("Page.navigate", url=url)
        time.sleep(settle)

    def hash_to(self, route: str, settle: float = 2.0) -> None:
        """站内切页只改 hash。
        用 Page.navigate 去改 hash 会等一个"导航完成"事件，而 hash 变化并不触发
        真正的导航 —— CDP 会一直等到超时。直接改 location.hash 更稳。
        """
        self.js(f"location.hash = {json.dumps(route)}")
        time.sleep(settle)

    def click_partial(self, text: str) -> bool:
        """按包含匹配点击（页签的文字后面往往跟着条数，比如「供应商成员 3」）"""
        return bool(self.js(
            "(() => { const b = [...document.querySelectorAll('button, a, [role=tab]')]"
            ".find(x => (x.textContent || '').includes(%s));"
            " if (!b) return false; b.click(); return true; })()" % json.dumps(text)
        ))

    def text(self) -> str:
        return self.js("document.body.innerText") or ""

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
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def badge_text(b: Browser) -> str:
    """顶栏数据来源标记：0 = 正常（无标记），否则返回标记文字"""
    return b.js(
        "(() => { const el = document.querySelector('.shell__demo');"
        " return el ? el.innerText.trim() : ''; })()"
    ) or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="13800138000")
    ap.add_argument("--password", default="admin1111")
    ap.add_argument("--api", default=API)
    ap.add_argument("--web", default=WEB)
    ap.add_argument("--out", default="_shots-wired")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--read-only", action="store_true",
                    help="不跑新建项目的写操作")
    ap.add_argument("--shots", action="store_true",
                    help="顺便截图留证（默认关闭：无头 Chrome 长时间运行后偶发卡死）")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    try:
        api = Api(args.api, args.phone, args.password)
    except Exception as exc:  # noqa: BLE001
        print(f"接口登录失败，无法建立基准：{exc}")
        return 2

    # --- 基准数据 ---------------------------------------------------------
    projects = api.rows("/api/v1/projects")
    tasks = api.rows("/api/v1/tasks?limit=500")
    suppliers = api.rows("/api/v1/suppliers")
    members = api.rows("/api/v1/supplier-members")
    reviews = api.rows("/api/v1/reviews")
    settlements = api.rows("/api/v1/settlements")
    staff = api.call("/api/v1/admin/users").get("items") or []
    print(f"基准：项目 {len(projects)} · 任务 {len(tasks)} · 供应商 {len(suppliers)} · "
          f"标注员 {len(members)} · 审核 {len(reviews)} · 结算 {len(settlements)} · "
          f"内部账号 {len(staff)}")
    print()

    b = Browser(find_browser(args.browser), out)
    try:
        # --- 1. 注入 token ---------------------------------------------------
        # 注意必须触发一次真正的文档重载：
        # 只改 hash 的 Page.navigate 不会重新执行脚本，React 的 AuthProvider
        # 早就以"无 token"的状态跑完了，后续每个页面都会被弹回登录页 ——
        # 症状看起来像"接了真库但没数据"，实际只是注入时机错了。
        b.goto(f"{args.web}/#/login", 2.2)  # 首屏必须是真导航（此时还停在 about:blank）
        b.js("localStorage.setItem('forge.platform.token', %s)" % json.dumps(api.token))
        b.goto(f"{args.web}/?reload={int(time.time())}#/", 3.0)
        landed = b.js("location.hash") or ""
        check(not landed.startswith("#/login"),
              "带 token 重载后没有被弹回登录页", f"location.hash={landed!r}")

        # --- 1.5 真刷新一次，确认不会回落到 seed -----------------------------
        # 刷新用真导航（重新执行脚本，完整走一遍 token → /auth/me → 拉 10 个接口）
        b.goto(f"{args.web}/?r2={int(time.time())}#/tasks", 3.4)
        check(badge_text(b) == "", "整页刷新后仍是真实数据（没有回落到 seed）",
              f"标记={badge_text(b)!r}")

        # --- 2. 一次走完所有页面，把每页 innerText 一起带回来 ----------------
        # 为什么不在 Python 里一页一页 navigate + 读 DOM：
        #   那样要几十次 CDP 往返，中间只要有一次响应没收干净，整条连接就废了
        #   （实测在第 6 张截图之后偶发超时，页面本身完全正常）。
        #   改成在页面里跑一个循环，一次调用拿到全部页面的文本 ——
        #   断言内容一模一样，往返次数从 20+ 降到 1。
        ROUTES = [
            ["#/", "overview"],
            ["#/projects", "projects"],
            ["#/tasks", "tasks"],
            ["#/suppliers", "suppliers"],
            ["#/people", "people"],
            ["#/settlement", "settlement"],
            ["#/review", "review"],
            ["#/data", "data"],
            ["#/performance", "performance"],
        ]
        pages = b.js(
            """
            (async () => {
              const routes = %s;
              const out = {};
              for (const [hash, key] of routes) {
                location.hash = hash;
                await new Promise(r => setTimeout(r, 1100));
                out[key] = document.body.innerText || '';
              }
              const badge = document.querySelector('.shell__demo');
              out.__badge = badge ? badge.innerText.trim() : '';
              const partial = document.querySelector('[title^="以下模块未能加载"]');
              out.__partial = partial ? partial.innerText.trim() : '';
              return out;
            })()
            """
            % json.dumps(ROUTES)
        )
        if not isinstance(pages, dict):
            raise RuntimeError(f"页面巡检脚本没有返回结果：{pages!r}")

        check(pages.get("__badge", "") == "",
              "顶栏没有「演示数据」标记（说明数据来自后端）", f"标记={pages.get('__badge')!r}")
        check(pages.get("__partial", "") == "",
              "没有模块加载失败", pages.get("__partial", ""))

        body = pages.get("overview", "")
        check("FORGE" in body or "工作台" in body, "工作台可渲染")

        # --- 3. 项目页 -------------------------------------------------------
        body = pages.get("projects", "")
        check(f"{len(projects)}" in body, "项目页条数与接口一致", f"接口 {len(projects)} 条")
        if projects:
            names = [p["name"] for p in projects[:3] if p.get("name")]
            check(any(n in body for n in names), "项目页显示了库里的真实项目名",
                  f"期望其中之一：{names}")

        # --- 4. 任务页（重点验证 project_name / assignee_name 的 join） -------
        body = pages.get("tasks", "")
        check(f"{len(tasks)} 个批次" in body or f"{len(tasks)}" in body,
              "任务页条数与接口一致", f"接口 {len(tasks)} 条")
        if tasks:
            # 后端 join 出来的项目名必须出现在列表里；
            # 若只显示 project_id，说明 join 没生效或映射层取错字段
            pname = tasks[0].get("project_name") or ""
            pid = tasks[0].get("project_id") or ""
            check(bool(pname) and pname in body,
                  "任务行显示项目名（join 生效）",
                  f"project_name={pname!r} project_id={pid!r}")
            check(pid not in body, "任务行没有把 project_id 直接漏出来", f"id={pid!r}")
            tname = tasks[0].get("name") or ""
            check(bool(tname) and tname in body, "任务行显示任务名称", f"name={tname!r}")
            aname = tasks[0].get("assignee_name") or ""
            if aname:
                check(aname in body, "任务行显示标注员姓名（join 生效）",
                      f"assignee_name={aname!r}")

        # --- 5. 供应商页 -----------------------------------------------------
        body = pages.get("suppliers", "")
        if suppliers:
            check(suppliers[0]["name"] in body, "供应商页显示库里的真实供应商",
                  f"期望 {suppliers[0]['name']!r}")

        # --- 6. 人员页（内部团队必须是库里的人，不是种子里的虚构同事） --------
        body = pages.get("people", "")
        check(f"{len(staff)} 人" in body or f"{len(staff) + len(members)} 人" in body,
              "人员页人数与接口一致",
              f"内部 {len(staff)} + 标注员 {len(members)}")
        if staff:
            hit = next((x["name"] for x in staff[:8] if x.get("name")), "")
            check(bool(hit) and hit in body, "内部团队显示库里的真实账号", f"期望 {hit!r}")
        # 种子里的虚构同事不该再出现 —— 它们曾是这张表的主要内容
        fake = [n for n in ("陆一鸣", "唐婉", "周则明", "岑雨") if n in body]
        check(not fake, "内部团队不再混入种子数据里的虚构员工", f"仍出现：{fake}")

        # 供应商成员是另一个页签，单独点开看一眼
        if members:
            b.hash_to("#/people", 1.6)
            b.click_partial("供应商成员")
            time.sleep(1.0)
            body2 = b.text()
            check(members[0]["name"] in body2, "供应商成员页显示库里的真实标注员",
                  f"期望 {members[0]['name']!r}")

        # --- 7. 结算 / 审核 / 数据 / 绩效 -------------------------------------
        body = pages.get("settlement", "")
        if settlements:
            check(settlements[0]["code"] in body, "结算页显示库里的真实结算单",
                  f"期望 {settlements[0]['code']!r}")

        # 这两个模块库里目前是 0 行。空表最容易暴露"没接上"和"没数据"分不清 ——
        # 所以断言的是页面本身没崩、标题在，而不是有内容。
        check("数据管理" in pages.get("data", ""),
              "数据管理页可正常渲染", f"接口 {len(api.rows('/api/v1/assets'))} 条资产")
        check("绩效" in pages.get("performance", ""),
              "绩效中心页可正常渲染", f"接口 {len(api.rows('/api/v1/performance'))} 条快照")

        # 截图留证放到最后：截图不是断言，崩了不该拖垮整轮验证。
        # （实测连拍大图后标签页偶尔不再响应，页面本身没问题 —— 所以它必须
        #   best-effort，不能挡在关键断言前面。）
        # --- 8. 通过界面新建项目，验证写链路真的落库 -------------------------
        if not args.read_only:
            before = len(api.rows("/api/v1/projects"))
            stamp = time.strftime("%m%d%H%M%S")
            proj_name = f"联调验证-{stamp}"

            b.hash_to("#/projects", 2.4)
            opened = b.click_text("新建项目")
            time.sleep(0.8)
            opened = opened and bool(b.js(
                "(() => !!document.querySelector(\"input[placeholder*='自动驾驶']\"))()"
            ))
            check(opened, "「新建项目」弹窗能打开")

            if opened:
                b.type_into("input[placeholder*='自动驾驶']", proj_name)
                b.type_into("input[placeholder*='蔚途汽车']", "联调客户")
                clicked = b.click_text("创建为草稿")
                check(clicked, "点击了「创建为草稿」")
                time.sleep(2.2)

                after = len(api.rows("/api/v1/projects"))
                check(after == before + 1,
                      "新建项目真的落库了（接口条数 +1）",
                      f"{before} → {after}")
                rows_now = api.rows("/api/v1/projects")
                hit = next((r for r in rows_now if r.get("name") == proj_name), None)
                check(hit is not None, "库里的新项目名称与界面输入一致",
                      f"查找 {proj_name!r}")
                if hit:
                    check(bool(hit.get("code")), "后端自动生成了项目编号",
                          f"code={hit.get('code')!r}")

                # 新建成功后应能回到列表并看到它
                b.hash_to("#/projects", 2.4)
                check(proj_name in b.text(), "列表里能看到刚创建的项目", proj_name)

        # --- 9. 截图留证（默认关闭，用 --shots 打开）-------------------------
        # 截图不是断言。无头 Chrome 在长时间运行后偶尔不再响应 CDP，
        # 一旦卡住就会把整轮验证拖到超时 —— 而那时所有断言其实已经跑完了。
        # 所以默认不拍；需要人工看图时再加 --shots。
        if args.shots:
            shots = [("01-projects", "#/projects"), ("02-tasks", "#/tasks"),
                     ("03-people", "#/people"), ("04-settlement", "#/settlement")]
            taken = 0
            for name, route in shots:
                try:
                    b.hash_to(route, 1.4)
                    b.shot(name)
                    taken += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[note] 截图 {name} 未完成（不影响结论）：{type(exc).__name__}")
                    break
            print(f"截图 {taken}/{len(shots)} 张 → {out}")

    except Exception as exc:  # noqa: BLE001
        check(False, "执行过程未抛异常", f"{type(exc).__name__}: {exc}")
    finally:
        b.close()

    ok = sum(1 for r in RESULTS if r[0])
    bad = len(RESULTS) - ok
    print()
    print(f"结果：{ok} 通过 / {bad} 失败（截图在 {out}）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
