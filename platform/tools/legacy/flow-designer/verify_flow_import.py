# -*- coding: utf-8 -*-
"""验证「从标注引擎导入现有配置」这条路径。

场景：老项目是在 Label Studio 后台直接配的表单，平台里只有绑定、没有流程。
这时甲方打开设计器会看到空白，一点应用就把原配置覆盖了。这条接口负责把
现状读回来当起点，不认识的控件如实列出来（绝不能静默丢掉）。

覆盖：
  A. 未绑定 → 如实回 not_bound，不假装成功
  B. 已绑定 → 读回 label_config，解析出步骤 / 标题 / 条件分支
  C. 带平台不认识的控件 → unsupported 里点名，便于界面切手写模式

跑完自动清理。不进交付包。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from make_type_projects import call as ls_call, ls_token  # noqa: E402

API = "http://127.0.0.1:8300"
TITLE = "PROBE·导入现有配置"
FAILS: list[str] = []


def req(path, method="GET", body=None, token=""):
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


def check(ok, label, extra=""):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{('  ' + extra) if extra else ''}")
    if not ok:
        FAILS.append(label)


CFG = """<View>
  <Image name="image" value="$image"/>
  <Header value="是否有缺陷"/>
  <Choices name="has_defect" toName="image" required="true">
    <Choice value="有缺陷"/><Choice value="无缺陷"/>
  </Choices>
  <View visibleWhen="choice-selected" whenTagName="has_defect" whenChoiceValue="有缺陷">
    <Header value="缺陷类型"/>
    <Choices name="defect_type" toName="image"><Choice value="划痕"/></Choices>
    <Paragraph value="平台不认识的控制标签，用来验证 unsupported"/>
  </View>
</View>"""


def main() -> int:
    st, d = req("/api/v1/auth/login", "POST",
                {"phone": "13800138001", "password": "Manager@2026"})
    tok = d["access_token"]

    st, proj = req("/api/v1/projects", "POST",
                   {"name": TITLE, "client": "内部验证", "modality": "image",
                    "total_items": 1, "unit_price": 1}, tok)
    pid = proj.get("project", {}).get("id")
    print("探针项目", pid)

    # A. 未绑定
    print("\n=== A. 未绑定时如实说明 ===")
    st, r = req(f"/api/v1/projects/{pid}/flow/from-ls", token=tok)
    check(st == 200 and (r or {}).get("ok") is False, "未绑定不假装有配置", f"{st}")
    check((r or {}).get("reason") == "not_bound", "回的是 not_bound")

    # 造一个 LS 项目并直接写配置（模拟"甲方当年在 LS 后台配的"），
    # 然后把它绑到平台项目上，但不写 project_flows —— 这就是老项目的真实状态。
    ltok = ls_token()
    st, created = ls_call(ltok, "/api/projects/", "POST", {
        "title": TITLE, "label_config": CFG, "description": "模拟 LS 后台直接配置"})
    ls_pid = created["id"]
    print("LS 项目", ls_pid)

    import os
    import subprocess
    env = dict(os.environ, PGCLIENTENCODING="UTF8", PGPASSWORD="postgres")
    sql = f"update forge.projects set labeling_config_id = '{ls_pid}' where id = '{pid}';"
    subprocess.run(["C:/Program Files/PostgreSQL/17/bin/psql.exe", "-h", "127.0.0.1",
                    "-U", "postgres", "-d", "forge_rls_test", "-c", sql],
                   env=env, capture_output=True, check=True)

    try:
        # B. 读回 + 解析
        print("\n=== B. 读回现有配置 ===")
        st, r = req(f"/api/v1/projects/{pid}/flow/from-ls", token=tok)
        check(st == 200 and (r or {}).get("ok") is True, "读回成功", f"{st}")
        check((r or {}).get("ls_project_id") == ls_pid, "指向正确的 LS 项目")
        check("是否有缺陷" in ((r or {}).get("label_config") or ""), "拿到的是 LS 里那份原文")
        steps = ((r or {}).get("steps") or {}).get("steps") or []
        check(len(steps) == 2, "解析出 2 步", f"{len(steps)}")
        names = [s.get("name") for s in steps]
        check(names == ["has_defect", "defect_type"], "步骤顺序与字段名对", str(names))
        titles = [s.get("title") for s in steps]
        check(titles == ["是否有缺陷", "缺陷类型"], "中文标题保住了", str(titles))
        check(steps[0].get("required") is True, "必填标记保住了")
        check(steps[1].get("depends_on") == "has_defect", "条件分支保住了")

        # C. 不认识的控件
        print("\n=== C. 不认识的控件如实列出来 ===")
        un = (r or {}).get("unsupported") or []
        check("Paragraph" in un, "Paragraph 被点名，不会被静默丢掉", str(un))
    finally:
        print("\n=== 清理 ===")
        try:
            print("  删 LS 项目", ls_call(ls_token(), f"/api/projects/{ls_pid}/", "DELETE")[0])
        except Exception as exc:  # noqa: BLE001
            print("  清理 LS 失败:", exc)
        print("  平台项目行:", pid)

    print()
    print("全部通过" if not FAILS else f"未通过：{FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
