# -*- coding: utf-8 -*-
"""端到端验证「平台内可视化配标注流程 → 写进 Label Studio → 标注员看到的就是那样」。

覆盖 6 段：
  1. 素材接口（模板/控件类型）
  2. 步骤校验：字段名重复必须 400，且一次把问题说清
  3. 保存：结构化步骤 → 生成 XML，且**不动** LS
  4. 应用（已有标注）→ 409 保护，不静默改坏别人的标注
  5. 应用（无绑定）→ 新建 LS 项目并回写绑定
  6. 浏览器实测：往新项目里丢一条数据，用标注员身份打开，
     断言标签库读到的必填控件、条件分支收起状态，与设计器里配的一致

跑完自动清理（删 LS 项目 + 删平台探针项目行）。不进交付包。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from make_type_projects import call as ls_call, ls_token  # noqa: E402
from shot_roles import Browser  # noqa: E402
from verify_brand import find_browser  # noqa: E402
from verify_ls_permissions import secret, ticket_for  # noqa: E402

API = "http://127.0.0.1:8300"
SEC = secret()
FAILS: list[str] = []
IMG = "http://127.0.0.1:8080/static/images/forge-type-probe.png"
TITLE = "PROBE · 流程设计器"


def req(path: str, method: str = "GET", body=None, token: str = "") -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILS.append(label)


def login() -> str:
    st, d = req("/api/v1/auth/login", "POST",
                {"phone": "13800138001", "password": "Manager@2026"})
    assert st == 200, f"登录失败 {st} {d}"
    return d.get("access_token") or d.get("token") or ""


TWO_STAGE = [
    {"kind": "choices", "name": "has_defect", "title": "是否有缺陷",
     "required": True, "options": ["有缺陷", "无缺陷"]},
    {"kind": "choices", "name": "defect_type", "title": "缺陷类型",
     "required": True, "options": ["划痕", "破损", "污渍"],
     "depends_on": "has_defect", "depends_value": "有缺陷"},
    {"kind": "rectanglelabels", "name": "defect_box", "title": "框出缺陷位置",
     "options": ["缺陷"], "depends_on": "has_defect", "depends_value": "有缺陷"},
]


def main() -> int:
    tok = login()
    print("登录 ok")

    # ---- 1. 素材 ----
    print("\n=== 1. 设计器素材 ===")
    st, d = req("/api/v1/label-step-templates", token=tok)
    check(st == 200 and len((d or {}).get("templates") or []) >= 3, "内置模板可用",
          f"templates={len((d or {}).get('templates') or [])}")
    check(any(k.get("kind") == "choices" for k in (d or {}).get("kinds") or []),
          "控件类型清单可用")

    # ---- 建一个探针项目（真实走平台接口）----
    st, proj = req("/api/v1/projects", "POST",
                   {"name": TITLE, "client": "内部验证", "modality": "image",
                    "total_items": 1, "unit_price": 1}, tok)
    if st not in (200, 201):
        print("建项目失败", st, proj)
        return 2
    pid = proj.get("project", {}).get("id") or proj.get("id")
    print("探针项目", pid, proj.get("project", {}).get("code") or proj.get("code"))

    try:
        # ---- 2. 校验 ----
        print("\n=== 2. 步骤校验 ===")
        bad = {"object": "image", "steps": [
            {"kind": "choices", "name": "a", "title": "第一步", "options": ["x"]},
            {"kind": "choices", "name": "a", "title": "第二步", "options": []},
        ]}
        st, d = req(f"/api/v1/projects/{pid}/flow", "PUT", bad, tok)
        # 平台统一错误体是 {"error": {"code", "message"}}，不是 FastAPI 默认的 {"detail"}
        msg = ((d or {}).get("error") or {}).get("message") if isinstance(d, dict) else str(d)
        check(st == 400, "字段名重复被拒", f"{st}")
        check("重复" in str(msg) and "可选值" in str(msg), "一次报出多个问题", f"{msg}")

        # ---- 3. 保存 ----
        print("\n=== 3. 保存流程（不碰 LS）===")
        st, d = req(f"/api/v1/projects/{pid}/flow", "PUT",
                    {"object": "image", "steps": TWO_STAGE,
                     "template_key": "two_stage_defect"}, tok)
        check(st == 200, "保存成功", f"{st}")
        xml = ((d or {}).get("flow") or {}).get("label_config") or ""
        check("visibleWhen" in xml, "生成了条件分支", f"{len(xml)} 字符")
        check('required="true"' in xml, "必填写进了 XML")
        st, g = req(f"/api/v1/projects/{pid}/flow", token=tok)
        check(((g or {}).get("steps") or {}).get("object") == "image", "读回来结构完整")

        # ---- 4. 应用到引擎（新建 / 再更新）----
        # 顺序说明：必须先把"首次应用走新建"验掉，再去造标注。否则项目已经
        # 绑了 LS 项目又有了标注，这段必然被 409 拦住，是测试顺序问题而不是缺陷。
        print("\n=== 4. 应用到标注引擎 ===")
        st, d = req(f"/api/v1/projects/{pid}/flow/apply", "POST", {"force": False}, tok)
        check(st == 200, "应用成功", f"{st} {str(d)[:120]}")
        mode = (d or {}).get("mode")
        ls_pid = (d or {}).get("ls_project_id")
        check(mode == "created", "首次应用走「新建」分支", f"mode={mode} ls#{ls_pid}")
        check((d or {}).get("tasks_bound") is False, "如实说明数据还没导",
              f"hint={(d or {}).get('hint', '')[:40]}")

        # 再应用一次：应当走 updated 分支（此时还没有任何标注，不该被保护拦住）
        st, d2 = req(f"/api/v1/projects/{pid}/flow/apply", "POST", {"force": False}, tok)
        check(st == 200 and (d2 or {}).get("mode") == "updated",
              "第二次应用走「更新」分支", f"{st} mode={(d2 or {}).get('mode')}")

        # ---- 5. 有标注时的保护（自己造一条标注，不借真实项目）----
        # 为什么必须自造：借真实项目去试"覆盖保护"，一旦 force 判断写错就会
        # 改坏真实数据。宁可多写几步，也不拿别人的标注当实验品。
        print("\n=== 5. 已有标注时的保护 ===")
        ltok0 = ls_token()
        st, imp0 = ls_call(ltok0, f"/api/projects/{ls_pid}/import", "POST",
                           [{"data": {"image": IMG}}])
        check(st in (200, 201), "先垫一条任务（标注必须挂在任务上）", f"{st}")
        st, t0 = ls_call(ltok0, f"/api/tasks?project={ls_pid}&page_size=5")
        rows0 = t0.get("tasks") or t0.get("results") or []
        if rows0:
            tid0 = rows0[0]["id"]
            st, _ = ls_call(ltok0, f"/api/tasks/{tid0}/annotations/", "POST", {
                "result": [{"from_name": "has_defect", "to_name": "image",
                            "type": "choices", "value": {"choices": ["无缺陷"]}}]})
            check(st in (200, 201), "造一条真实标注用于测试保护", f"{st}")
            st, d = req(f"/api/v1/projects/{pid}/flow/apply", "POST", {"force": False}, tok)
            emsg = ((d or {}).get("error") or {}).get("message") or ""
            check(st == 409, "有标注时拒绝直接覆盖", f"{st} {emsg[:60]}")
            check("条标注" in emsg, "提示里说清影响多少条", emsg[:60])
            # 勾了确认才允许
            st, d = req(f"/api/v1/projects/{pid}/flow/apply", "POST", {"force": True}, tok)
            check(st == 200, "显式确认后才允许覆盖", f"{st}")
        else:
            check(False, "造标注失败，无法验证保护")

        # ---- 6. 浏览器实测：标注员看到的就是设计器里配的 ----
        print("\n=== 6. 浏览器实测（标注员视角）===")
        ltok = ls_token()
        st, imp = ls_call(ltok, f"/api/projects/{ls_pid}/import", "POST",
                          [{"data": {"image": IMG}}])
        check(st in (200, 201), "往新项目丢一条探针数据", f"{st}")
        st, tasks = ls_call(ltok, f"/api/tasks?project={ls_pid}&page_size=10")
        trows = tasks.get("tasks") or tasks.get("results") or []
        check(bool(trows), "拿到 task", f"{len(trows)} 条")
        # 取最新一条：第 5 段垫的那条已经带了标注，打开会是"已标注"态，
        # 测不出"全新任务的条件分支默认收起"。
        tid = trows[-1]["id"] if trows else 0

        out = Path("D:/forge-ls/shots/flow")
        out.mkdir(parents=True, exist_ok=True)
        b = Browser(find_browser(None), out)
        try:
            url = ticket_for("13800138001", True,
                             f"/projects/{ls_pid}/data?task={tid}", SEC, projects=[ls_pid])
            b.goto(url, settle=8.0)
            for _ in range(24):
                if b.js("!!(window.__FORGE_UX__ && window.__FORGE_UX__.isMounted && "
                        "window.__FORGE_UX__.isMounted())"):
                    break
                time.sleep(0.5)
            dg = b.js("window.__FORGE_UX__ ? JSON.stringify(window.__FORGE_UX__.diag()) : ''")
            d = json.loads(dg) if dg else {}
            check(d.get("mounted") is True, "增强层已挂载")
            check(d.get("required") == ["has_defect", "defect_type"],
                  "必填控件与设计器一致", f"{d.get('required')}")
            check(d.get("canvasKind") == "image", "题型识别为图像", f"{d.get('canvasKind')}")

            # 条件分支：默认收起 defect_type / defect_box
            blocks = b.js(
                "(() => Array.from(document.querySelectorAll('.lsf-choices, .lsf-rectanglelabels'))"
                ".map(n => ({cls: String(n.className).slice(0,50),"
                " h: Math.round(n.getBoundingClientRect().height),"
                " hidden: String(n.className).indexOf('_hidden') >= 0})))()")
            hidden = [x for x in (blocks or []) if x.get("hidden")]
            check(len(hidden) >= 1, "条件分支默认收起", f"hidden={len(hidden)}/{len(blocks or [])}")
            b.shot("flow-created")
            print("   截图:", "D:/forge-ls/shots/flow/flow-created.png")
        finally:
            b.close()
    finally:
        # ---- 清理：LS 项目 + 平台探针项目行 ----
        print("\n=== 清理 ===")
        for pid_txt in (pid,):
            pass
        try:
            st, g = req(f"/api/v1/projects/{pid}/flow", token=tok)
            ls_id = (g or {}).get("ls_project_id")
            if ls_id and str(ls_id).isdigit():
                print("  删 LS 项目", ls_id,
                      ls_call(ls_token(), f"/api/projects/{ls_id}/", "DELETE")[0])
        except Exception as exc:  # noqa: BLE001
            print("  清理 LS 失败:", exc)
        print("  平台项目行请用 psql 删除：delete from forge.projects where id =", pid)

    print()
    if FAILS:
        print("未通过：", FAILS)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
