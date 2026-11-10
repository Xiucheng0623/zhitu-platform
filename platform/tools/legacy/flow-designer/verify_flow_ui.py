# -*- coding: utf-8 -*-
"""验证「标注流程设计器」在真浏览器里能跑通，且改动能落到标注引擎。

覆盖 5 段：
  1. 项目详情里出现「标注流程」面板，展示已配的步骤
  2. 点开设计器：步骤卡片、条件分支、实时 XML 预览都对
  3. 在界面上改一个步骤标题 + 加一个选项 → 预览 XML 跟着变
  4. 点「保存」→ 库里真的变了，但 LS 还没变（保存≠生效）
  5. 点「保存并应用到标注引擎」→ LS 项目的 label_config 真的是新的一份

为什么要这么验：这个功能就是要让甲方自己改，所以"打开能看见蓝字"不算过，
必须是**在界面上点、然后数据库和标注引擎各读一遍**都能对上。

跑完自动清理探针项目。不进交付包。

用法：
  python tools/.tmp/verify_flow_ui.py
  python tools/.tmp/verify_flow_ui.py --keep   # 不清理，留着看界面
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[1] / "labelstudio" / "brand"))

from verify_wired_ui import API, Browser, check, RESULTS  # noqa: E402
from verify_brand import CDP, WS, find_browser  # noqa: E402  (Browser 内部用到)

WEB = "http://127.0.0.1:9121"
TITLE = "PROBE·设计器UI"

TWO_STAGE = [
    {"kind": "choices", "name": "has_defect", "title": "是否有缺陷",
     "required": True, "options": ["有缺陷", "无缺陷"]},
    {"kind": "choices", "name": "defect_type", "title": "缺陷类型",
     "required": True, "options": ["划痕", "破损"],
     "depends_on": "has_defect", "depends_value": "有缺陷"},
    {"kind": "rectanglelabels", "name": "defect_box", "title": "框出缺陷位置",
     "options": ["缺陷"], "depends_on": "has_defect", "depends_value": "有缺陷"},
]


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


def ls_get(token: str, path: str) -> object:
    r = urllib.request.Request("http://127.0.0.1:8080/api" + path)
    # LS 的 access token 用 Bearer；写成 Token 会直接 401，
    # 症状是"清理失败/读不到配置"，很容易误判成项目被删了。
    r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--browser", default=None)
    args = ap.parse_args()

    out = Path("D:/forge-ls/shots/flow")
    out.mkdir(parents=True, exist_ok=True)

    st, d = req("/api/v1/auth/login", "POST",
                {"phone": "13800138001", "password": "Manager@2026"})
    assert st == 200, f"登录失败 {st} {d}"
    tok = d["access_token"]

    # 探针项目：真走平台接口建，跑完删掉
    st, proj = req("/api/v1/projects", "POST",
                   {"name": TITLE, "client": "内部验证", "modality": "image",
                    "total_items": 1, "unit_price": 1}, tok)
    assert st in (200, 201), f"建项目失败 {st} {proj}"
    pid = proj.get("project")["id"]
    print("探针项目", pid)

    # 先把流程配好（模拟"甲方上次已经配过"），再看界面
    st, _ = req(f"/api/v1/projects/{pid}/flow", "PUT",
                {"object": "image", "steps": TWO_STAGE,
                 "template_key": "two_stage_defect"}, tok)
    assert st == 200, f"预置流程失败 {st}"

    b = Browser(find_browser(args.browser), out)
    ls_pid = None
    try:
        b.goto(f"{WEB}/#/login", 2.0)
        b.js("localStorage.setItem('forge.platform.token', %s)" % json.dumps(tok))
        # 带 token 做一次真导航（只改 hash 不会重新执行脚本，token 注入就白做了）
        b.goto(f"{WEB}/?r={int(time.time())}#/projects", 3.5)

        # ---- 1. 面板存在且展示步骤 ----
        print("\n=== 1. 项目详情的「标注流程」面板 ===")
        opened = b.js(
            "(() => { const rows = [...document.querySelectorAll('tr, [role=row], .fk-row')];"
            " const r = rows.find(x => (x.innerText || '').includes(%s));"
            " if (!r) return false; r.click(); return true; })()" % json.dumps(TITLE))
        check(bool(opened), "点开探针项目的详情抽屉")
        time.sleep(1.4)
        body = b.text()
        check("标注流程" in body, "详情里有「标注流程」面板")
        check("是否有缺陷" in body and "缺陷类型" in body, "面板里能看到已配的步骤")
        check("仅当" in body, "条件分支在面板上有说明", )
        b.shot("ui-panel")

        # ---- 2. 打开设计器 ----
        print("\n=== 2. 打开设计器 ===")
        hit = b.click_partial("编辑流程")
        check(hit, "找到「编辑流程」入口")
        time.sleep(1.6)
        body = b.text()
        check("标注流程设计器" in body, "设计器已打开")
        cards = b.js("document.querySelectorAll('.fk-modal .fk-input').length")
        check((cards or 0) >= 6, "步骤卡片渲染出多个输入框", f"inputs={cards}")
        b.shot("ui-visual")  # 默认停在「标注员看到的样子」
        # XML 预览：切到 XML 页签再看
        b.click_partial("编译出的 XML")
        time.sleep(0.8)
        xml = b.js(
            "(() => { const t = [...document.querySelectorAll('.fk-modal textarea')]"
            ".find(x => (x.value || '').includes('<View'));"
            " return t ? t.value : ''; })()")
        check(bool(xml) and "visibleWhen" in str(xml), "XML 预览里有条件分支",
              f"{len(str(xml))} 字符")
        b.shot("ui-designer")

        # ---- 3. 改标题并加一个选项，预览要跟着变 ----
        print("\n=== 3. 在界面上改动 ===")
        b.click_partial("标注员看到的样子")
        time.sleep(0.6)
        # 第一个步骤标题输入框 = 设计器里第一个 .fk-input（步骤卡片里的 TextInput）
        changed = b.js(
            "(() => { const m = document.querySelector('.fk-modal'); if (!m) return 0;"
            " const ins = [...m.querySelectorAll('input.fk-input')]"
            "   .filter(x => !x.classList.contains('fk-mono'));"
            " if (!ins.length) return 0; ins[0].focus(); ins[0].select(); return 1; })()")
        check(bool(changed), "定位到第一步的标题输入框")
        b.cdp.call("Input.insertText", text="是否有缺陷（改）")
        time.sleep(1.6)  # 等 450ms 防抖 + 一次后端编译
        body = b.text()
        check("是否有缺陷（改）" in body, "标题改动已反映在预览里")

        # ---- 4. 保存：库里变，LS 不变 ----
        print("\n=== 4. 保存（只落库）===")
        hit = b.click_text("保存")
        check(hit, "点了「保存」")
        time.sleep(2.0)
        st, g = req(f"/api/v1/projects/{pid}/flow", token=tok)
        t0 = (g or {}).get("steps", {}).get("steps", [{}])[0].get("title", "")
        check(t0 == "是否有缺陷（改）", "库里的标题已更新", f"{t0!r}")
        check(not (g or {}).get("ls_project_id"), "此时还没绑定标注引擎")

        # ---- 5. 保存并应用：LS 真的变了 ----
        print("\n=== 5. 保存并应用到标注引擎 ===")
        hit = b.click_partial("保存并应用到标注引擎")
        check(hit, "点了「保存并应用到标注引擎」")
        # toast 只活两三秒，睡 3 秒再去读就只剩一片空白 —— 这不是功能问题，
        # 是断言时机问题。改成轮询，命中就停。
        said = ""
        for _ in range(16):
            time.sleep(0.35)
            said = b.text()
            if "标注项目已创建" in said or "还没有导入数据" in said:
                break
        check("标注项目已创建" in said or "还没有导入数据" in said,
              "如实提示「建了项目但还没导数据」")
        b.shot("ui-applied")
        st, g2 = req(f"/api/v1/projects/{pid}/flow", token=tok)
        ls_pid = (g2 or {}).get("ls_project_id")
        check(bool(ls_pid), "平台侧已经回写了标注引擎项目 id", f"ls#{ls_pid}")

        if ls_pid:
            from make_type_projects import ls_token  # noqa: PLC0415
            remote = ls_get(ls_token(), f"/projects/{ls_pid}/")
            cfg = remote.get("label_config") or ""
            check("是否有缺陷（改）" in cfg, "标注引擎里的 label_config 真的是新的",
                  f"{len(cfg)} 字符")
            check("visibleWhen" in cfg, "条件分支也带过去了")
    finally:
        b.close()
        print("\n=== 清理 ===")
        if args.keep:
            print("  --keep：保留探针项目", pid, "ls#", ls_pid)
        else:
            try:
                from make_type_projects import ls_token  # noqa: PLC0415
                if ls_pid:
                    r = urllib.request.Request(
                        f"http://127.0.0.1:8080/api/projects/{ls_pid}/", method="DELETE")
                    r.add_header("Authorization", "Bearer " + ls_token())
                    with urllib.request.urlopen(r, timeout=20) as resp:
                        print("  删 LS 项目", ls_pid, resp.status)
            except Exception as exc:  # noqa: BLE001
                print("  清理 LS 失败:", exc)
            print(f"  平台项目行：delete from forge.projects where id = '{pid}';")

    print()
    bad = [n for ok, n, _ in RESULTS if not ok]
    print("全部通过" if not bad else f"未通过 {len(bad)} 项：{bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
