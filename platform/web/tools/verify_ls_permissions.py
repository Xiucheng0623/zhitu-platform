# -*- coding: utf-8 -*-
"""标注引擎内的角色权限、可见性收口与品牌残留回归。

验的三组事（都是用户明确提过的）：
  1. 普通标注员（票据 can_import=false）在 LS 里看不到「创建项目 / 导入」入口，
     且直接拼 URL、直接调接口都会被服务端 403 拦住 —— 前端隐藏不是安全边界。
  2. 标注员可见性收口：只见自己的批次项目 —— 项目列表页 302 回自己项目、
     /api/projects 与 /api/dm/views 列表只回白名单、别人项目 404、
     名册（users/annotators/people/organization）与导出与凭证签发一律不可见。
     供应商 / 管理员（can_import=true）不受影响。
  3. 导入空态页的第三方云存储品牌位已隐藏；品牌启动态在、
     LS 原版动物加载动画与模板注释都不出现在界面上。

用法：
    set -a && . /d/forge-ls/env.local && set +a
    python platform/web/tools/verify_ls_permissions.py
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "platform" / "labelstudio" / "brand"))
from verify_brand import CDP, WS, find_browser  # noqa: E402

API = "http://127.0.0.1:8300"
LS = "http://127.0.0.1:8080"
PORT = 9367

results: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, detail: str = "") -> None:
    results.append((bool(ok), title, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {title}" + (f" — {detail}" if detail else ""))


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_ticket(claims: dict, secret: str) -> str:
    body = b64u(json.dumps(claims, separators=(",", ":"),
                           ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = b64u(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                        hashlib.sha256).digest())
    return f"{body}.{sig}"


def secret() -> str:
    s = os.environ.get("FORGE_LS_SSO_SECRET", "").strip()
    if len(s) >= 32:
        return s
    env = Path("D:/forge-ls/env.local")
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("export FORGE_LS_SSO_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 FORGE_LS_SSO_SECRET")


def ticket_for(phone: str, can_import: bool, next_path: str, sec: str,
               projects=None) -> str:
    """签测试票据。projects 给了就按新式票据带项目白名单；不给按老式
    （LS 侧从 next 反解单项目白名单）——两条路都要能跑。"""
    now = int(time.time())
    claims = {
        "v": 1, "purpose": "ls-sso", "sub": "usr_probe_" + phone,
        "email": f"{phone}@staff.zhitu.local", "name": "权限探针",
        "can_import": can_import, "next": next_path,
        "jti": uuid.uuid4().hex, "iat": now, "exp": now + 90,
    }
    if projects:
        claims["ls_project_id"] = int(projects[0])
        claims["ls_projects"] = [int(x) for x in projects]
    return f"{LS}/forge/sso/?ticket={urllib.parse.quote(sign_ticket(claims, sec), safe='')}"


def ls_access() -> str:
    """用 env.local 里的 refresh token 换 JWT access（fixture 管理用）。"""
    env = Path("D:/forge-ls/env.local")
    refresh = ""
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("export FORGE_LS_REFRESH_TOKEN="):
            refresh = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not refresh:
        raise SystemExit("env.local 里找不到 FORGE_LS_REFRESH_TOKEN")
    req = urllib.request.Request(
        f"{LS}/api/token/refresh/",
        data=json.dumps({"refresh": refresh}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["access"]


def ensure_fixtures() -> tuple[int, int]:
    """保证两个测试批次项目在 LS 里存在（演示数据清过，这里自建）。

    返回 (自己项目 id, 别人项目 id)。带任务与 DM 视图，覆盖
    view→tasks 通道的越权验证。
    """
    cfg = ('<View><Text name="text" value="$text"/>'
           '<Choices name="label" toName="text">'
           '<Choice value="OK"/><Choice value="NG"/></Choices></View>')
    access = ls_access()

    def post(path, body):
        req = urllib.request.Request(
            LS + path, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + access,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def call(method, path, body=None):
        req = urllib.request.Request(
            LS + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + access,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def get(path):
        req = urllib.request.Request(LS + path,
                                     headers={"Authorization": "Bearer " + access})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    # 探针的建项目动作在非受限会话下会真的建出 'probe' 项目，先清掉上轮残留
    for row in get("/api/projects/?title=probe").get("results", []):
        if row.get("title") == "probe":
            call("DELETE", f"/api/projects/{row['id']}/")

    def ensure(title):
        # 按标题复用，重跑脚本不堆积测试项目
        for row in get("/api/projects/?title=" + urllib.parse.quote(title)).get("results", []):
            if row.get("title") != title:
                continue
            pid = int(row["id"])
            # 自愈：上一轮探针可能删过任务（早期版本 delete_tasks 选中过全部）
            detail = get(f"/api/projects/{pid}/")
            if not detail.get("task_number"):
                post(f"/api/projects/{pid}/import",
                     [{"text": f"{title} 样本 A"}, {"text": f"{title} 样本 B"}])
            return pid
        st, body = post("/api/projects/", {"title": title, "label_config": cfg})
        if st != 201 or not isinstance(body.get("id"), int):
            raise SystemExit(f"建测试项目失败: {st} {body}")
        pid = body["id"]
        post(f"/api/projects/{pid}/import",
             [{"text": f"{title} 样本 A"}, {"text": f"{title} 样本 B"}])
        post("/api/dm/views/", {"project": pid, "data": {
            "ordering": [], "selectedItems": {"all": False, "included": []},
            "filters": {"conjunction": "and", "items": []}}})
        return pid

    return ensure("B1-批次一"), ensure("B2-批次二")


PROBE_HIDDEN = r"""(() => {
  const text = (document.body.innerText || '');
  const leaked = /\{#|\{%/.test(text);
  const box = document.querySelector('[data-testid="dm-storage-provider-icons"]');
  const providerBox = box ? getComputedStyle(box).display === 'none' ? 'hidden' : 'visible' : 'absent';
  const empty = document.querySelector('[data-testid="empty-state-label"]');
  const emptyIcons = empty ? empty.querySelectorAll('svg, img').length : 0;
  const locked = [...document.querySelectorAll('[data-forge-locked="1"]')]
    .map(e => (e.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  const visible = [];
  document.querySelectorAll('a[href], button').forEach((el) => {
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    const href = el.getAttribute('href') || '';
    if (!t || t.length > 14) return;
    if (!/创建|导入|Create|Import/.test(t) && !/\/projects\/(create|new)|\/data\/import/.test(href)) return;
    const r = el.getBoundingClientRect();
    if (r.width > 1 && r.height > 1 && getComputedStyle(el).visibility !== 'hidden') visible.push(t);
  });
  const spike = [...document.querySelectorAll('svg path.spike-fill, svg path.spike-stroke')]
    .filter(p => getComputedStyle(p.closest('svg')).display !== 'none').length;
  return JSON.stringify({
    url: location.href, ready: document.readyState, leaked, providerBox, emptyIcons,
    locked, visible, spike, restricted: window.FORGE_LS_RESTRICTED === true,
  });
})()"""


def api_probe(cdp, own: int, other: int) -> dict:
    """在页面里直接调接口：证明服务端拦得住，不只是按钮藏了。

    own=当前会话应可见的项目，other=别人的项目。可见性收口的判词都在这：
    列表只剩自己、别人项目 404、名册/导出/凭证面 403、next_task 放行。
    """
    js = r"""(async () => {
      const csrf = (document.cookie.split('; ').find(c => c.startsWith('csrftoken=')) || '').split('=')[1] || '';
      const H = {'Content-Type': 'application/json', 'X-CSRFToken': csrf};
      const out = {};
      // 谁是我：名册收口要区分"自己"和"别人"，先拿自己的 id
      // 注意路径是 current-user（连字符），current_user 在这个版本是 404
      try {
        const r = await fetch('/api/current-user/whoami', {credentials: 'same-origin'});
        if (r.ok) out.me = await r.json();
      } catch (e) { /* 拿不到就退化成"全部视为别人" */ }
      const hit = async (key, url, opts) => {
        try {
          const r = await fetch(url, Object.assign({credentials: 'same-origin'}, opts || {}));
          out[key] = r.status;
          if (key === 'projectList' && r.ok) {
            const d = await r.json();
            out.projectIds = (d.results || d).map(p => p.id).sort();
            out.projectCount = d.count === undefined ? null : d.count;
          }
          if (key === 'viewList' && r.ok) {
            const d = await r.json();
            out.viewProjects = (d.results || d).map(v => v.project).sort();
          }
          if (key === 'userList' && r.ok) {
            const d = await r.json();
            const rows = d.results || d;
            out.userCount = rows.length;
            // 名册收口要验的是「别人认不出来」，不是「一行都没有」：
            // 列表空掉会让 DM 解析 annotators 引用时整页崩。
            const me = out.me || {};
            out.userSelfVisible = rows.some(u => u.id === me.id);
            out.userLeak = rows
              .filter(u => u.id !== me.id)
              .filter(u => u.email || u.phone || u.username || u.avatar ||
                           (u.first_name && u.first_name !== '标注员'))
              .map(u => u.id);
          }
          if (key === 'foreignPage') out.foreignFinal = r.url;
        } catch (e) { out[key] = 'ERR ' + e; }
      };
      await hit('importApi', '/api/projects/__OWN__/import', {method: 'POST', headers: H, body: '{}'});
      await hit('createApi', '/api/projects', {method: 'POST', headers: H, body: JSON.stringify({title: 'probe'})});
      await hit('importPage', '/projects/__OWN__/data/import');
      await hit('projectList', '/api/projects');
      await hit('viewList', '/api/dm/views');
      await hit('userList', '/api/users');
      await hit('annotatorsApi', '/api/projects/__OWN__/annotators/');
      await hit('exportApi', '/api/projects/__OWN__/export?exportType=JSON');
      await hit('tokenApi', '/api/current-user/token');
      await hit('peoplePage', '/people/');
      await hit('orgPage', '/organization/');
      await hit('settingsPage', '/projects/__OWN__/settings/');
      await hit('foreignApi', '/api/projects/__OTHER__/');
      await hit('foreignPage', '/projects/__OTHER__/data', {redirect: 'follow'});
      await hit('foreignTasks', '/api/tasks?project=__OTHER__');
      await hit('ownTasks', '/api/tasks?project=__OWN__');
      await hit('actionDelete', '/api/dm/actions/?id=delete_tasks&project=__OWN__',
                {method: 'POST', headers: H, body: JSON.stringify({selectedItems: {all: false, included: [999999]}})});
      await hit('actionNext', '/api/dm/actions/?id=next_task&project=__OWN__',
                {method: 'POST', headers: H, body: JSON.stringify({selectedItems: {all: false, included: []}})});
      if (typeof out.actionNext === 'number' && out.actionNext >= 400) {
        // 把 404 的语义带回来：LS 的"没有任务"不是我们守卫的"not found"
        try {
          const r = await fetch('/api/dm/actions/?id=next_task&project=__OWN__',
            {method: 'POST', credentials: 'same-origin', headers: H,
             body: JSON.stringify({selectedItems: {all: false, included: []}})});
          out.actionNextDetail = (await r.text()).slice(0, 120);
        } catch (e) {}
      }
      return JSON.stringify(out);
    })()"""
    expr = js.replace("__OWN__", str(int(own))).replace("__OTHER__", str(int(other)))
    raw = cdp.call("Runtime.evaluate", expression=expr,
                   awaitPromise=True, returnByValue=True)["result"].get("value")
    return json.loads(raw or "{}")


def run(cdp, url: str, want: str, wait: float = 25.0) -> dict:
    """导航到 url，等真正落到 want 这个路由且文档 ready 后再采样。

    只看 sleep 时长会采到上一页的 DOM（实测把管理员的会话状态当成了标注员的），
    所以这里必须轮询 location.href。
    """
    cdp.call("Page.navigate", url=url)
    deadline = time.time() + wait
    dom: dict = {}
    while time.time() < deadline:
        time.sleep(0.25)
        try:
            raw = cdp.call("Runtime.evaluate", expression=PROBE_HIDDEN,
                           returnByValue=True)["result"].get("value")
        except Exception:
            continue
        if not raw:
            continue
        dom = json.loads(raw)
        if want in dom.get("url", "") and dom.get("ready") == "complete":
            return dom
    return dom


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/forge-ls/shots/perm")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    sec = secret()
    prof = tempfile.mkdtemp(prefix="forge-perm-")
    proc = subprocess.Popen(
        [find_browser(None), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
         "--window-size=1600,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws = None
        end = time.time() + 20
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                    for t in json.loads(r.read()):
                        if t.get("type") == "page":
                            ws = t["webSocketDebuggerUrl"]
                            break
                if ws:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        cdp = CDP(WS(ws))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")

        own, other = ensure_fixtures()
        print(f"测试锚点：own=B1#{own}  other=B2#{other}")

        # ---- 1. 管理员：导入空态页的第三方品牌位 -------------------------
        admin_url = ticket_for("13800138001", True, f"/projects/{own}/data", sec)
        dom = run(cdp, admin_url, f"/projects/{own}/data")
        check(f"/projects/{own}/data" in dom.get("url", ""), "管理员可进入自己项目数据页",
              dom.get("url", ""))
        check(dom.get("leaked") is False, "页面上没有泄漏 Django 模板标记（{# / {%）",
              "仍有泄漏" if dom.get("leaked") else "干净")
        # 第三方云存储 logo 要么整块被移除，要么被 CSS 藏掉；剩下只能有品牌标记一个图标
        check(dom.get("providerBox") in ("absent", "hidden") and dom.get("emptyIcons", 9) <= 1,
              "导入空态页不再出现 Amazon/Google/Azure/Redis 品牌标识",
              f"providerBox={dom.get('providerBox')} 空态图标数={dom.get('emptyIcons')}")
        api = api_probe(cdp, own, other)
        check(api.get("importPage") == 200, "非受限账号（管理员）导入页正常打开",
              str(api.get("importPage")))
        check(api.get("peoplePage") == 200, "非受限账号可打开成员页（收口只针对标注员）",
              str(api.get("peoplePage")))
        # 探针的 createApi 会真的建出 'probe' 项目，这里只验证『不被裁剪』
        check(set([own, other]).issubset(set(api.get("projectIds") or [])),
              "非受限账号项目列表不受白名单裁剪",
              str(api.get("projectIds")))

        # ---- 2. 标注员：入口隐藏 + 服务端 403 ----------------------------
        anno_url = ticket_for("13700137000", False, f"/projects/{own}/data", sec,
                              projects=[own])
        dom = run(cdp, anno_url, f"/projects/{own}/data", 30)
        check(dom.get("restricted") is True, "标注员会话带受限标记（FORGE_LS_RESTRICTED）",
              f"restricted={dom.get('restricted')} url={dom.get('url')}")
        check(not dom.get("visible"), "标注员页面上没有可见的「创建 / 导入」入口",
              json.dumps(dom.get("visible"), ensure_ascii=False))
        api = api_probe(cdp, own, other)
        check(api.get("importApi") == 403, "标注员调导入接口被服务端 403 拦截",
              str(api.get("importApi")))
        check(api.get("createApi") == 403, "标注员调建项目接口被服务端 403 拦截",
              str(api.get("createApi")))
        check(api.get("importPage") == 403, "标注员直接打开导入页被 403 拦下",
              str(api.get("importPage")))
        check(dom.get("spike") == 0, "界面上没有可见的 LS 原版动物加载动画",
              f"spike={dom.get('spike')}")

        # ---- 2b. 标注员可见性收口：只见自己的批次 ------------------------
        check(api.get("projectIds") == [own] and api.get("projectCount") == 1,
              "标注员项目列表只剩自己的批次项目",
              f"ids={api.get('projectIds')} count={api.get('projectCount')}")
        check(api.get("viewProjects") == [own],
              "标注员 DM 视图列表只剩自己项目的视图",
              str(api.get("viewProjects")))
        check(api.get("userSelfVisible") is True,
              "名册里能看到自己（否则任务行解析标注人会整页崩）",
              f"userCount={api.get('userCount')}")
        check(api.get("userLeak") == [],
              "名册里除自己外没有任何真实身份（邮箱/手机/姓名/头像都脱敏）",
              f"仍在泄漏的 id={api.get('userLeak')}")
        check(api.get("annotatorsApi") == 403,
              "标注员查项目成员接口被 403 拦截", str(api.get("annotatorsApi")))
        check(api.get("exportApi") == 403,
              "标注员导出接口被 403 拦截", str(api.get("exportApi")))
        check(api.get("tokenApi") == 403,
              "标注员签发 API 凭证被 403 拦截（防绕开会话级限制）",
              str(api.get("tokenApi")))
        check(api.get("peoplePage") == 403,
              "标注员打不开成员页", str(api.get("peoplePage")))
        check(api.get("orgPage") == 403,
              "标注员打不开组织页", str(api.get("orgPage")))
        check(api.get("settingsPage") == 403,
              "标注员打不开项目设置页", str(api.get("settingsPage")))
        check(api.get("foreignApi") == 404,
              "标注员查别人项目接口 404（看不见即为不存在）",
              str(api.get("foreignApi")))
        check(api.get("foreignPage") == 404,
              "标注员打开别人项目页面 404",
              f"{api.get('foreignPage')} final={api.get('foreignFinal')}")
        check(api.get("foreignTasks") == 404,
              "标注员按别人项目拉任务 404", str(api.get("foreignTasks")))
        check(api.get("ownTasks") == 200,
              "标注员拉自己项目任务正常", str(api.get("ownTasks")))
        check(api.get("actionDelete") == 403,
              "标注员执行删任务等批量动作被 403 拦截", str(api.get("actionDelete")))
        # next_task 是标注流取任务的正路：只许 200，或 LS 自己的"没有任务"，
        # 绝不能是收口守卫的 "not found"（那说明被误伤）
        nxt = api.get("actionNext")
        nxt_detail = str(api.get("actionNextDetail") or "")
        check(nxt == 200 or (nxt == 404 and "no tasks" in nxt_detail),
              "标注流 next_task 动作不受收口影响（标注正路）",
              f"{nxt} {nxt_detail}")

        # 项目列表页对标注员没有意义：302 回自己的批次项目
        dom = run(cdp, f"{LS}/projects/", f"/projects/{own}/data", 30)
        check(f"/projects/{own}/data" in dom.get("url", ""),
              "标注员打开项目列表页被 302 带回自己的批次项目",
              dom.get("url", ""))

        # ---- 2c. 老式票据（无 ls_projects）退回单项目白名单 -------------
        dom = run(cdp, ticket_for("13700137000", False, f"/projects/{own}/data", sec),
                  f"/projects/{own}/data", 30)
        api = api_probe(cdp, own, other)
        check(api.get("projectIds") == [own],
              "老式票据（无白名单字段）退回单项目白名单",
              str(api.get("projectIds")))

        # ---- 2d. 新式票据带多项目白名单：两个批次都可见 ------------------
        dom = run(cdp, ticket_for("13700137000", False, f"/projects/{own}/data", sec,
                                  projects=[own, other]),
                  f"/projects/{own}/data", 30)
        api = api_probe(cdp, own, other)
        check(sorted(api.get("projectIds") or []) == sorted([own, other]),
              "多批次标注员可见自己全部批次项目（白名单取票据）",
              str(api.get("projectIds")))

        # ---- 3. 供应商/管理员：导入页照常 -------------------------------
        dom = run(cdp, ticket_for("13900139000", True, f"/projects/{own}/data", sec),
                  f"/projects/{own}/data")
        api = api_probe(cdp, own, other)
        check(api.get("importPage") == 200, "非受限账号（供应商/管理员）导入页正常打开",
              str(api.get("importPage")))
        check(api.get("foreignApi") == 200,
              "非受限账号可打开任意项目（收口只针对标注员）",
              str(api.get("foreignApi")))

        # ---- 4. 品牌启动态 ----------------------------------------------
        cdp.call("Page.navigate", url=ticket_for("13800138001", True, f"/projects/{own}/data", sec))
        seen_boot = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                raw = cdp.call("Runtime.evaluate",
                               expression="!!document.getElementById('forge-wb-boot')",
                               returnByValue=True)["result"].get("value")
            except Exception:
                raw = None
            if raw:
                seen_boot = True
                break
            time.sleep(0.05)
        check(seen_boot is True, "整页进入时品牌启动态出现", "未见 #forge-wb-boot")
        dom = run(cdp, f"{LS}/projects/{own}/data?labeling=1", f"/projects/{own}/data", 30)
        check(dom.get("spike") == 0, "标注流里也没有原版动物动画", f"spike={dom.get('spike')}")
        check(dom.get("leaked") is False, "标注流页面没有模板标记泄漏", str(dom.get("leaked")))
        cdp.call("Page.captureScreenshot")
        try:
            data = cdp.call("Page.captureScreenshot")["data"]
            (out / "workbench.png").write_bytes(base64.b64decode(data))
        except Exception:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            pass

    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
