"""
FORGE Orchestrator · HTTP 契约与隔离测试
================================================================
跑在真实服务 + 真实 PostgreSQL 上，不 mock 任何东西。

做的事：
  1. 健康检查与 RLS 强制状态
  2. 四道隔离边界（租户 / 供应商 / 标注员 / 越权 404）
  3. 状态机（非法转移 409、成员归属校验 422、终态保护）
  4. 结算的分步权限（财务动作不开放给项目经理）
  5. 幂等重放（同一 Idempotency-Key 第二次不再执行）
  6. 审计留痕（每个写操作都有记录）

可重复执行：自带 fixture（直接插一条测试任务，结束时删除），
不依赖也不破坏 003_seed_verify.sql 的种子数据。

用法：
  python tests/smoke_http.py                     # 默认 http://127.0.0.1:8300
  FORGE_BASE=http://host:8300 python tests/smoke_http.py
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import httpx
import psycopg
from psycopg.rows import dict_row

BASE = os.environ.get("FORGE_BASE", "http://127.0.0.1:8300")
DSN = os.environ.get(
    "FORGE_DB_DSN",
    "postgresql://forge_app:forge_dev_2026@localhost:5432/forge",
)
TENANT = os.environ.get("FORGE_DEFAULT_TENANT", "ent_forge")

PASS = 0
FAIL = 0
SKIP = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}".strip())
        print(f"  FAIL  {name}  {detail}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP  {name}  ({why})")


def req(method: str, path: str, token: str, body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
    h = {"Authorization": f"Bearer {token}"} if token else {}
    if headers:
        h.update(headers)
    with httpx.Client(timeout=15, base_url=BASE, trust_env=False) as c:
        r = c.request(method, path, json=body, headers=h)
    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        payload = r.text
    return r.status_code, payload, dict(r.headers)


# ---------------------------------------------------------------------------
# fixture：直连数据库造一条待分配任务（不经 API，因为平台没有「建任务」端点）
# ---------------------------------------------------------------------------
def _db():
    conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=False)
    conn.execute("select set_config('forge.enterprise_id', %s, true)", (TENANT,))
    conn.execute("select set_config('forge.supplier_id', '', true)")
    conn.execute("select set_config('forge.assignee_id', '', true)")
    return conn


def setup_fixture() -> Dict[str, Any]:
    tsk = f"tsk_smoke_{int(time.time())}"
    stl = f"stl_smoke_{int(time.time())}"
    # 期间用随机年份，避开 ux_settle_supplier_period 唯一约束，保证可重复跑
    period = f"{1900 + int(time.time()) % 200:04d}-{1 + int(time.time()) % 12:02d}"
    with _db() as conn, conn.transaction():
        row = conn.execute(
            "select id from forge.projects order by id limit 1").fetchone()
        row2 = conn.execute(
            "select id from forge.suppliers order by id limit 1").fetchone()
        row3 = conn.execute(
            "select id from forge.supplier_members where supplier_id = %s and status = 'active'"
            " order by id limit 1", (row2["id"],)).fetchone()
        other_member = conn.execute(
            "select id, supplier_id from forge.supplier_members"
            " where supplier_id <> %s and status = 'active' order by id limit 1",
            (row2["id"],)).fetchone()
        conn.execute(
            """
            insert into forge.tasks
                   (id, enterprise_id, project_id, batch_no, name, modality,
                    status, item_count, unit_price, due_at, priority)
            values (%s, %s, %s, %s, %s, 'image', 'pending', 500, 3.5, now() + interval '5 days', 'normal')
            """,
            (tsk, TENANT, row["id"], f"SMOKE-{int(time.time())}", "冒烟测试批次"),
        )
        conn.execute(
            """
            insert into forge.settlements
                   (id, enterprise_id, supplier_id, code, period, accepted_items,
                    unit_price_avg, amount, deduction, payable, status, due_at)
            values (%s, %s, %s, %s, %s, 1000, 1.0000, 1000.00, 100.00, 900.00,
                    'draft', now() + interval '15 days')
            """,
            (stl, TENANT, row2["id"], f"STL-SMOKE-{int(time.time())}", period),
        )
    return {"task_id": tsk, "project_id": row["id"],
            "settlement_id": stl,
            "supplier_id": row2["id"], "member_id": row3["id"],
            "other_supplier_id": other_member["supplier_id"],
            "other_member_id": other_member["id"]}


def teardown_fixture(fx: Dict[str, Any]) -> None:
    with _db() as conn, conn.transaction():
        conn.execute("delete from forge.task_assignments where task_id = %s", (fx["task_id"],))
        conn.execute("delete from forge.reviews where task_id = %s", (fx["task_id"],))
        conn.execute("delete from forge.annotation_submissions where task_id = %s", (fx["task_id"],))
        conn.execute("delete from forge.tasks where id = %s", (fx["task_id"],))
        conn.execute("delete from forge.settlements where id = %s", (fx["settlement_id"],))


def main() -> int:
    print(f"\nFORGE orchestrator 契约测试 → {BASE}\n")

    # -- 1. 健康 ------------------------------------------------------------
    print("[1] 健康检查与 RLS")
    code, h, _ = req("GET", "/healthz", "")
    check("healthz 200", code == 200, f"got {code}")
    check("数据库连通", bool(h.get("db", {}).get("ok")), str(h.get("db"))[:120])
    db = h.get("db", {})
    # users / invites 是 010 迁移里**刻意**不开 RLS 的两张表：
    # 登录发生在"还不知道自己属于哪个租户"的时刻，RLS 表达式无从求值。
    # 豁免清单写死在这里，而不是用 tables==forced 一刀切 ——
    # 否则要么把新增的漏保护表一起放过，要么因为这两张表永远红。
    # 豁免清单是**设计决定**，不是"漏配了 RLS"。每次新增豁免都必须在这里
    # 明写一行，否则这个断言会红 —— 目的是逼人解释"为什么这张表不需要租户隔离"。
    #   users / invites            : 登录链路，还没有租户上下文（010）
    #   identities / link_requests : 同上，SSO 回调发生在会话建立之前（011）
    EXPECTED_RLS_EXEMPT = {"users", "invites",
                           "identities", "identity_link_requests"}
    exempt = set(db.get("rls_exempt") or [])
    check("业务表强制 RLS，且豁免清单与设计一致",
          db.get("tables", 0) >= 15
          and db.get("rls_forced", 0) + len(exempt) == db.get("tables", 0)
          and exempt == EXPECTED_RLS_EXEMPT,
          f"tables={db.get('tables')} forced={db.get('rls_forced')} "
          f"exempt={sorted(exempt)}")
    check("应用账号无法绕过 RLS", db.get("can_bypass_rls") is False)

    # 集成健康探测：外部系统不可达时必须降级为逐系统状态，而不是整体 502
    code, ih, _ = req("GET", "/api/v1/integrations/health", "ops_director")
    check("集成健康接口自身可用（外部系统不可达也返回 200）", code == 200, f"got {code} {ih}")
    rows_ih = ih.get("rows", {}) if isinstance(ih, dict) else {}
    check("ERPNext 已移除（不应再出现 erpnext 字段）",
          "erpnext" not in rows_ih, str(rows_ih)[:160])
    check("返回 labelstudio 状态字段", "ok" in rows_ih.get("labelstudio", {}), str(rows_ih)[:160])
    check("返回 storage 状态字段", "ok" in (ih.get("storage") or {}), str(ih.get("storage"))[:120])

    # -- 2. 身份 ------------------------------------------------------------
    print("[2] 身份解析")
    code, me, _ = req("GET", "/api/v1/me", "ops_director")
    check("ops_director 解析成功", code == 200 and "ops_director" in me.get("roles", []))
    check("ops_director 具备 settlement:approve", "settlement:approve" in me.get("permissions", []))
    code, me2, _ = req("GET", "/api/v1/me", "project_manager")
    check("project_manager 不具备 settlement:approve",
          "settlement:approve" not in me2.get("permissions", []))
    code, _, _ = req("GET", "/api/v1/me", "")
    check("无 token 返回 401", code == 401, f"got {code}")

    # -- 3. 隔离 ------------------------------------------------------------
    print("[3] 四道隔离边界")
    _, all_tasks, _ = req("GET", "/api/v1/tasks", "ops_director")
    n_all = all_tasks.get("total", 0)
    check("内部视角可见全部任务", n_all >= 3, f"total={n_all}")

    _, sup_tasks, _ = req("GET", "/api/v1/tasks", "supplier_admin@sup_a1")
    only_a1 = all(r.get("supplier_id") == "sup_a1" for r in sup_tasks.get("rows", []))
    check("供应商标注 view 只见自己承接的任务", only_a1, str(sup_tasks)[:160])
    check("供应商视角条数少于内部视角", sup_tasks.get("total", 0) < n_all,
          f"{sup_tasks.get('total')} vs {n_all}")

    code, _, _ = req("GET", "/api/v1/tasks/tsk_a1", "supplier_admin@sup_a1")
    check("供应商访问他人任务返回 404", code == 404, f"got {code}")

    _, other, _ = req("GET", "/api/v1/tasks", "ent_other::ops_director")
    other_ids = [r["id"] for r in other.get("rows", [])]
    check("跨租户看不到本租户任务", not any(i.startswith("tsk_a") for i in other_ids),
          str(other_ids))

    code, _, _ = req("GET", "/api/v1/audit", "annotator@sup_a1")
    check("标注员访问审计返回 404", code == 404, f"got {code}")

    code, _, _ = req("POST", f"/api/v1/tasks/tsk_a1/assign", "reviewer",
                     {"supplier_id": "sup_a1", "assignee_id": "mem_a1"})
    check("审核员无 task:assign 权限返回 404", code == 404, f"got {code}")

    # -- 4. fixture + 状态机 -------------------------------------------------
    print("[4] 状态机")
    fx = setup_fixture()
    tid = fx["task_id"]
    try:
        code, out, _ = req("POST", f"/api/v1/tasks/{tid}/assign", "ops_director",
                           {"supplier_id": "sup_a2", "assignee_id": fx["member_id"]})
        check("成员不属于所选供应商返回 422", code == 422, f"got {code} {out}")

        code, out, _ = req("POST", f"/api/v1/tasks/{tid}/assign", "ops_director",
                           {"supplier_id": fx["supplier_id"], "assignee_id": fx["member_id"]})
        check("合法分配成功", code == 200 and out.get("status") == "assigned",
              f"got {code} {out}")

        # 供应商侧主体只能提交分配给自己的任务 —— 越权提交必须是 404 而非 403
        other_tok = f"annotator@{fx['other_supplier_id']}#{fx['other_member_id']}"
        code, out, _ = req("POST", f"/api/v1/tasks/{tid}/submit", other_tok,
                           {"item_count": 500})
        check("他人标注员提交本任务返回 404", code == 404, f"got {code} {out}")

        my_tok = f"annotator@{fx['supplier_id']}#{fx['member_id']}"
        code, out, _ = req("POST", f"/api/v1/tasks/{tid}/submit", my_tok,
                           {"item_count": 500, "ls_annotation_ref": "ls:ann:1"})
        check("本人标注员可提交", code == 200 and out.get("status") == "submitted",
              f"got {code} {out}")

        code, out, _ = req("POST", f"/api/v1/tasks/{tid}/review", "reviewer",
                           {"stage": "l1", "result": "rework", "note": "L1 抽样返工",
                            "sampled": 50, "defects": 4})
        check("审核员裁定返工", code == 200 and out.get("status") == "rework",
              f"got {code} {out}")
        check("返工轮次 +1", out.get("rework_round") == 1, str(out))

        code2, out2, _ = req("POST", f"/api/v1/tasks/{tid}/assign", "ops_director",
                             {"supplier_id": fx["supplier_id"], "assignee_id": fx["member_id"]})
        check("返工后可重新分配", code2 == 200, f"got {code2} {out2}")

        # 造一个终态，验证终态不可再流转
        with _db() as conn, conn.transaction():
            conn.execute("update forge.tasks set status = 'accepted' where id = %s", (tid,))
        code3, out3, _ = req("POST", f"/api/v1/tasks/{tid}/review", "ops_director",
                             {"stage": "l2", "result": "pass"})
        check("终态任务不可再流转（409）", code3 == 409, f"got {code3} {out3}")
        check("409 错误体含 state_conflict",
              isinstance(out3, dict) and out3.get("error", {}).get("code") == "state_conflict",
              str(out3)[:160])
    finally:
        teardown_fixture(fx)

    # -- 5. 结算分步权限 -----------------------------------------------------
    print("[5] 结算流转权限")
    fx3 = setup_fixture()
    try:
        sid = fx3["settlement_id"]
        code, out, _ = req("POST", f"/api/v1/settlements/{sid}/advance", "project_manager")
        check("项目经理推进结算被拒（需 settlement:write）", code == 404,
              f"got {code} {out}")
        code, out, _ = req("POST", f"/api/v1/settlements/{sid}/advance", "ops_director")
        check("运营总监可 draft → submitted", code == 200 and out.get("status") == "submitted",
              f"got {code} {out}")
        code, out, _ = req("GET", f"/api/v1/settlements?supplier_id={fx3['supplier_id']}",
                           "ops_director")
        row = next((r for r in out.get("rows", []) if r["id"] == sid), None)
        check("状态已落库", bool(row) and row["status"] == "submitted", str(row)[:160])
        check("应付金额 = 金额 - 扣款", row and row["payable"] == "900.00", str(row)[:120])
    finally:
        teardown_fixture(fx3)

    # -- 6. 幂等 ------------------------------------------------------------
    print("[6] 幂等重放")
    fx2 = setup_fixture()
    try:
        key = f"smoke-{uuid.uuid4()}"
        hdr = {"Idempotency-Key": key}
        body = {"supplier_id": fx2["supplier_id"], "assignee_id": fx2["member_id"]}
        c1, o1, _ = req("POST", f"/api/v1/tasks/{fx2['task_id']}/assign", "ops_director", body, hdr)
        c2, o2, h2 = req("POST", f"/api/v1/tasks/{fx2['task_id']}/assign", "ops_director", body, hdr)
        check("首次请求成功", c1 == 200, f"got {c1} {o1}")
        replay = h2.get("X-Idempotent-Replay") or h2.get("x-idempotent-replay")
        check("第二次为幂等重放", c2 == 200 and replay == "true",
              f"got {c2} replay={replay}")
        check("重放响应与首次一致", o1 == o2)
        with _db() as conn, conn.transaction():
            n = conn.execute(
                "select count(*) as n from forge.task_assignments where task_id = %s",
                (fx2["task_id"],)).fetchone()["n"]
        check("重放未产生重复分配记录", n == 1, f"rows={n}")
    finally:
        teardown_fixture(fx2)

    # -- 7. 审计 ------------------------------------------------------------
    print("[7] 审计留痕")
    code, au, _ = req("GET", "/api/v1/audit?limit=50", "ops_director")
    actions = [r["action"] for r in au.get("rows", [])]
    check("审计可读", code == 200 and au.get("total", 0) > 0, f"got {code}")
    check("记录到 settlement.advance", "settlement.advance" in actions, str(actions[:8]))
    check("记录到 review 裁定", any(a.startswith("review.") for a in actions), str(actions[:8]))

    # -- 汇总 --------------------------------------------------------------
    total = PASS + FAIL + SKIP
    print(f"\n结果：{PASS}/{total} 通过，{FAIL} 失败，{SKIP} 跳过")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
