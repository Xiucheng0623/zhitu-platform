#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WebDev 人工 Filter 可承接性 · 真实浏览器验证
============================================================================
目的不是"看看有没有接口"，而是回答一个具体问题：

    类似《WebDev 人工 Filter》这种「渲染模型生成的网页作品 → 人工打分 →
    写理由 → 多人独立评审」的项目，能不能用平台现有的一套通用标注能力
    承接，而不必为它重新开发一个页面？

验证分两层：
  A. API 层：label_config 合法性、数据契约、overlap（多人冗余）是否原生
  B. 浏览器层：用平台 SSO 票据进入 LS 标注流，真的把那条 HTML 作品渲染
     出来，检查 0-5 分选项、理由输入框是否存在，并**实测网页里的 JS
     是否还能运行**（需求要求"功能测试"，这一条决定要不要补能力）。

用法：
    python web/tools/verify_webdev_capability.py --out D:/forge-ls/shots
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

_BRAND_DIR = Path(__file__).resolve().parents[2] / "labelstudio" / "brand"
_CONFIGS = Path(__file__).resolve().parents[2] / "labelstudio" / "configs"
sys.path.insert(0, str(_BRAND_DIR))
from verify_brand import CDP, WS, find_browser  # noqa: E402

LS = "http://127.0.0.1:8080"
ENV_LOCAL = Path("D:/forge-ls/env.local")
PORT = 9343
PROJECT_ID = 6

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def gap(name: str, detail: str = "") -> None:
    """已知且必须补的能力，不算测试失败，但绝不能假装它已经通过。"""
    print(f"[GAP ] {name}" + (f" — {detail}" if detail else ""))


def env_value(name: str) -> str:
    text = ENV_LOCAL.read_text(encoding="utf-8")
    m = re.search(rf'export {name}="?([^"\n\r]+)"?', text)
    if not m:
        raise SystemExit(f"{ENV_LOCAL} 里找不到 {name}")
    return m.group(1).strip()


def ls_access_token() -> str:
    refresh = env_value("FORGE_LS_REFRESH_TOKEN")
    req = urllib.request.Request(
        f"{LS}/api/token/refresh/",
        data=json.dumps({"refresh": refresh}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access"]


def ls_api(path: str, token: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        LS + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return r.status, json.loads(raw) if raw else None


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_ticket(claims: dict, secret: str) -> str:
    body = b64u(json.dumps(claims, separators=(",", ":"),
                           ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = b64u(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                        hashlib.sha256).digest())
    return f"{body}.{sig}"


class Browser:
    def __init__(self, exe: str):
        self.profile = tempfile.mkdtemp(prefix="forge-webdev-")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--remote-debugging-port={PORT}", f"--user-data-dir={self.profile}",
             "--window-size=1680,1050", "about:blank"],
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

    def shot(self, path: Path) -> None:
        r = self.cdp.call("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.profile, ignore_errors=True)


def api_checks(token: str) -> None:
    xml = (_CONFIGS / "06-webdev-score.xml").read_text(encoding="utf-8")
    status, _ = ls_api("/api/projects/validate/", token, "POST",
                       {"label_config": xml})
    check(status == 204, "LS 官方校验器接受这份 XML 配置", f"HTTP {status}")

    _, project = ls_api(f"/api/projects/{PROJECT_ID}/", token)
    cfg = project.get("label_config", "")
    need = ["HyperText", "Choices", "TextArea", "required=\"true\""]
    missing = [n for n in need if n not in cfg]
    check(not missing, "评测界面由通用标签组合而成", f"缺={missing}")
    check(project.get("maximum_annotations", 1) >= 5,
          "项目支持 5-6 人独立评审（overlap）",
          f"maximum_annotations={project.get('maximum_annotations')}")

    _, tasks = ls_api(f"/api/projects/{PROJECT_ID}/tasks/?page_size=1", token)
    task = tasks[0]
    keys = set(task["data"].keys())
    check({"q_id", "prompt", "response_a_dist"} <= keys,
          "数据契约与需求文档一致", f"keys={sorted(keys)}")
    check(task.get("overlap") == project.get("maximum_annotations"),
          "冗余度已下发到任务", f"task.overlap={task.get('overlap')}")


def browser_checks(b: Browser, out: Path) -> None:
    secret = env_value("FORGE_LS_SSO_SECRET")
    now = int(time.time())
    claims = {
        "v": 1, "purpose": "ls-sso", "sub": "capability-probe",
        "email": "13800138009@staff.zhitu.local", "name": "能力验证",
        "next": f"/projects/{PROJECT_ID}/data?labeling=1",
        "jti": uuid.uuid4().hex, "iat": now, "exp": now + 90,
    }
    ticket = sign_ticket(claims, secret)
    url = f"{LS}/forge/sso/?ticket={urllib.parse.quote(ticket, safe='')}"
    b.goto(url, 8.0)

    location = b.js("location.href") or ""
    check(f"/projects/{PROJECT_ID}/data" in location and "/user/login" not in location,
          "平台 SSO 直接进入标注流，未出现二次登录", location[:120])
    check("/forge/sso" not in location, "票据中转页已完成 302", location[:120])

    body = b.js("document.body.innerText") or ""
    for label in ["整体评分", "0 - 不可用", "5 - 优秀", "评审理由", "渲染状态"]:
        check(label in body, f"评测控件可见：{label}")
    check("q_0001" in body and "题号" in body,
          "改一份 XML 后题号即刻上线（无需重编译前端）",
          f"q_id 可见={'q_0001' in body}")

    # HyperText 不是直接插进主文档，而是放进一个 sandbox iframe；
    # 之前在主文档里查 #t 全是 False，是探针写错了，不是页面没渲染。
    dom = b.js("""(() => {
      const f = document.querySelector('iframe.lsf-htx-richtext')
             || document.querySelector('iframe');
      const main = {
        textareas: document.querySelectorAll('textarea').length,
        radios: document.querySelectorAll('input[type=radio]').length,
        checkboxes: document.querySelectorAll('input[type=checkbox]').length,
      };
      if (!f) return {iframe: false, ...main};
      let inner = {}, err = null;
      try {
        const d = f.contentDocument;
        inner = {
          todoInput: !!d.querySelector('#t'),
          todoButton: !!Array.from(d.querySelectorAll('button'))
                        .find(x => x.textContent.trim() === '添加'),
          scriptTag: !!Array.from(d.querySelectorAll('script'))
                        .find(s => (s.textContent || '').includes('function add')),
          onclickAttr: !!Array.from(d.querySelectorAll('button'))
                         .find(x => x.getAttribute('onclick')),
          addFn: typeof f.contentWindow.add,
          bodyLen: (d.body ? d.body.innerHTML.length : 0),
        };
      } catch (e) { err = String(e); }
      return {
        iframe: true,
        sandbox: f.getAttribute('sandbox'),
        srcdocLen: (f.getAttribute('srcdoc') || '').length,
        inner, err, ...main,
      };
    })()""") or {}
    inner = dom.get("inner") or {}
    check(dom.get("iframe") is True and inner.get("todoInput") is True,
          "HyperText 真的渲染了模型生成的网页作品（sandbox iframe）",
          f"sandbox={dom.get('sandbox')} bodyLen={inner.get('bodyLen')}")
    check(dom.get("textareas", 0) >= 1, "理由输入框存在",
          f"textarea={dom.get('textareas')}")
    check(dom.get("radios", 0) + dom.get("checkboxes", 0) >= 6,
          "评分/检查项渲染成真实表单控件",
          f"radio={dom.get('radios')} checkbox={dom.get('checkboxes')}")
    check("allow-scripts" in (dom.get("sandbox") or ""),
          "LS 预览 iframe 沙箱本身允许脚本", str(dom.get("sandbox")))
    check(inner.get("scriptTag") is False and inner.get("onclickAttr") is False,
          "LS 在渲染前剥离 <script> / onclick（安全设计，实测确认）",
          f"scriptTag={inner.get('scriptTag')} onclick={inner.get('onclickAttr')}")

    if inner.get("addFn") != "function":
        gap("预览内 JS 不可执行 → 「功能测试」必须在原始产物的新标签页完成",
            f"typeof iframe.add = {inner.get('addFn')}；需求本来就写的是"
            "「点击新标签页全屏查看」，与 AIData 的做法一致")
        gap("平台缺少通用「新标签页打开原始产物」能力",
            "需一次性补：原始 HTML 独立源 + sandbox CSP + 通用打开按钮，"
            "不是为 WebDev 单独建页面")
    else:
        check(True, "网页作品 JS 可执行，预览内即可做功能测试")

    # 真的点一次「添加」，看交互是否生效（预期不会生效，如实记录）
    interact = b.js("""(() => {
      const f = document.querySelector('iframe.lsf-htx-richtext')
             || document.querySelector('iframe');
      if (!f || !f.contentDocument) return 'no-frame';
      const d = f.contentDocument;
      const inp = d.querySelector('#t');
      const btn = Array.from(d.querySelectorAll('button'))
                    .find(x => x.textContent.trim() === '添加');
      if (!inp || !btn) return 'missing';
      inp.value = '功能探针';
      btn.click();
      const ul = d.querySelector('#l');
      return ul ? ul.querySelectorAll('li').length : -1;
    })()""")
    print(f"[INFO] 预览内点击「添加」后 li 数 = {interact}（0 = 交互被剥离，符合预期）")

    b.shot(out / "webdev-01-annotate.png")
    print("\n截图：", out / "webdev-01-annotate.png")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/forge-ls/shots")
    ap.add_argument("--browser", default=None)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    token = ls_access_token()
    api_checks(token)

    b = Browser(find_browser(args.browser))
    try:
        browser_checks(b, out)
    finally:
        b.close()

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
