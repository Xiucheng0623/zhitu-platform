"""
FORGE Orchestrator · 财务域 / 采集域 端到端测试
================================================================
跑在真实服务 + 真实 PostgreSQL 上，不 mock 任何东西。
断言的是「业务闭环能否真的走完」，而不是「接口返回 200」。

覆盖两条完整链路：
  财务：建票 → 提交开票 → 收票 → 核验 → 结算单自动 invoiced
        → 申请付款 → 审批 → 到账（带回单号）→ 结算单自动 paid
        → 冲销 → 结算单退回 invoiced
  采集：采集员准入 → 采集任务 → 脚本批导 → 场次排期 → 提交 → 质检通过
        → 任务完成量与采集员累计自动回写

外加：越权 404、供应商横向隔离、重复票/重复流水号、幂等重放、审计留痕。

可重复执行：fixture 用固定单据号，开场先清掉上次残留，结束再清一次。

用法：
  python tests/e2e_finance.py
  FORGE_BASE=http://host:8300 FORGE_DB_DSN=... python tests/e2e_finance.py
"""
from __future__ import annotations

import os
import sys
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

PERIOD = "2098-12"          # 测试专用账期，远离真实数据
SETTLE_CODE = "STL-E2E-2098"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    FAILURES.append(f"{name} {detail}".strip())
    print(f"  FAIL  {name}  {detail}")
    return False


def req(method: str, path: str, token: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    h = {"Authorization": f"Bearer {token}"} if token else {}
    if headers:
        h.update(headers)
    # trust_env=False：本机若配了 HTTP_PROXY，发往 127.0.0.1 的请求会被交给
    # 代理，症状是随机 ReadTimeout 而不是「连不上」，极难定位。
    with httpx.Client(timeout=20, base_url=BASE, trust_env=False) as c:
        r = c.request(method, path, json=body, headers=h)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, r.text


def _db():
    conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=False)
    conn.execute("select set_config('forge.enterprise_id', %s, true)", (TENANT,))
    conn.execute("select set_config('forge.supplier_id', '', true)")
    conn.execute("select set_config('forge.assignee_id', '', true)")
    return conn


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
def cleanup() -> None:
    """按外键反序清理。审计日志是 append-only，故意不删 —— 这是设计而非疏漏。

    关键：清理范围必须严格限定在「测试自己造的数据」上。
    早先版本按「结算单所属供应商」删发票，把种子数据里同一供应商的真实发票
    一起删掉了（验收金额凭空少一张票，三单匹配从 ready_to_pay 变 missing_invoice）。
    现在测试自带独立供应商 SUP-E2E，清理只按 code='SUP-E2E' 收口，
    摸不到任何真实数据。
    """
    e2e_sup = ("select id from forge.suppliers where enterprise_id = %s and code = 'SUP-E2E'")
    with _db() as conn, conn.transaction():
        conn.execute(
            f"""delete from forge.payment_allocations
                 where settlement_id in (select id from forge.settlements
                                          where enterprise_id = %s
                                            and supplier_id in ({e2e_sup}))""",
            (TENANT, TENANT))
        conn.execute(
            f"delete from forge.payments where enterprise_id = %s and supplier_id in ({e2e_sup})",
            (TENANT, TENANT))
        conn.execute(
            f"delete from forge.invoices where enterprise_id = %s and supplier_id in ({e2e_sup})",
            (TENANT, TENANT))
        conn.execute(
            f"delete from forge.settlements where enterprise_id = %s and supplier_id in ({e2e_sup})",
            (TENANT, TENANT))
        conn.execute(
            """
            delete from forge.collection_sessions
             where enterprise_id = %s
               and task_id in (select id from forge.collection_tasks
                                where enterprise_id = %s and code like 'CT-E2E-%%')
            """, (TENANT, TENANT))
        conn.execute(
            "delete from forge.collection_tasks where enterprise_id = %s and code like 'CT-E2E-%%'",
            (TENANT,))
        conn.execute(
            "delete from forge.collectors where enterprise_id = %s and code like 'COL-E2E-%%'",
            (TENANT,))
        conn.execute(
            "delete from forge.assets where enterprise_id = %s and batch_no = 'E2E-BATCH'",
            (TENANT,))
        # 供应商最后删：此时已无 settlement / invoice / payment 引用它
        conn.execute(
            "delete from forge.suppliers where enterprise_id = %s and code = 'SUP-E2E'",
            (TENANT,))


def setup() -> Dict[str, Any]:
    cleanup()
    with _db() as conn, conn.transaction():
        # 测试专用供应商：把测试数据和真实供应商彻底隔开，清理才有边界
        conn.execute(
            """
            insert into forge.suppliers
                   (id, enterprise_id, code, name, short_name, tier, status,
                    modalities, city, capacity_hours)
            values ('sup_e2e', %s, 'SUP-E2E', '端到端测试供应商', 'E2E', 'C', 'active',
                    '{audio,video}', '上海', 1000)
            """, (TENANT,))
        other = conn.execute(
            "select id from forge.suppliers where enterprise_id = %s and code <> 'SUP-E2E'"
            " order by id limit 1", (TENANT,)).fetchone()
        prj = conn.execute("select id from forge.projects order by id limit 1").fetchone()
        conn.execute(
            """
            insert into forge.settlements
                   (id, enterprise_id, supplier_id, code, period, accepted_items,
                    unit_price_avg, amount, deduction, payable, status, due_at)
            values ('stl_e2e', %s, 'sup_e2e', %s, %s, 1060, 10.0000,
                    10600.00, 0.00, 10600.00, 'confirmed', now() + interval '10 days')
            """,
            (TENANT, SETTLE_CODE, PERIOD),
        )
    return {"supplier_id": "sup_e2e", "supplier_name": "端到端测试供应商",
            "other_supplier_id": other["id"], "project_id": prj["id"],
            "settlement_id": "stl_e2e"}



def teardown() -> None:
    cleanup()


# ---------------------------------------------------------------------------
def main() -> int:  # noqa: C901 - 测试脚本线性铺开比拆函数更好读
    print(f"\nFORGE 财务/采集端到端测试 → {BASE}")
    code, h = req("GET", "/healthz", "")
    if code != 200 or not h.get("ok"):
        print(f"服务不可用: {code} {h}")
        return 2

    fx = setup()
    sup = fx["supplier_id"]
    stl = fx["settlement_id"]

    try:
        # -- 1. 发票 --------------------------------------------------------
        print("\n[1] 发票：开票 → 收票 → 核验")
        code, inv = req("POST", "/api/v1/invoices", "finance", {
            "supplier_id": sup, "settlement_id": stl, "invoice_no": "E2E-044001900001",
            "invoice_type": "vat_special", "amount": "10000.00", "tax_rate": "0.06",
        })
        check("创建发票 201", code == 201, f"got {code} {inv}")
        inv_id = inv.get("id") if isinstance(inv, dict) else None
        check("价税合计 = 10000 + 600 = 10600", str(inv.get("total")) == "10600.00", str(inv))
        check("初始状态 draft", inv.get("status") == "draft")

        code, bad = req("POST", "/api/v1/invoices", "finance", {
            "supplier_id": sup, "settlement_id": stl, "invoice_no": "E2E-OVER",
            "amount": "20000.00", "tax_rate": "0.06",
        })
        check("发票金额超应付被拒（422）", code == 422, f"got {code} {bad}")

        code, r = req("POST", f"/api/v1/invoices/{inv_id}/issue", "finance")
        check("draft → issued", code == 200 and r.get("status") == "issued", f"{code} {r}")
        code, r = req("POST", f"/api/v1/invoices/{inv_id}/receive", "finance")
        check("issued → received", code == 200 and r.get("status") == "received", f"{code} {r}")

        code, m = req("GET", f"/api/v1/finance/match?period={PERIOD}", "finance")
        row = next((x for x in m.get("rows", []) if x["settlement_id"] == stl), {})
        check("未核验 → awaiting_verification",
              row.get("match_state") == "awaiting_verification", str(row))

        code, r = req("POST", f"/api/v1/invoices/{inv_id}/verify", "finance")
        check("received → verified", code == 200 and r.get("status") == "verified", f"{code} {r}")

        code, m = req("GET", f"/api/v1/finance/match?period={PERIOD}", "finance")
        row = next((x for x in m.get("rows", []) if x["settlement_id"] == stl), {})
        check("核验后 → ready_to_pay", row.get("match_state") == "ready_to_pay", str(row))

        code, s = req("GET", "/api/v1/settlements?status=invoiced", "finance")
        check("结算单被自动推进为 invoiced",
              any(x["id"] == stl for x in s.get("rows", [])), str(s.get("total")))

        code, dup = req("POST", "/api/v1/invoices", "finance", {
            "supplier_id": sup, "settlement_id": stl, "invoice_no": "E2E-044001900001",
            "amount": "100.00", "tax_rate": "0.06",
        })
        check("同一发票号重复录入被拒（422）", code == 422, f"got {code} {dup}")

        # -- 2. 付款 --------------------------------------------------------
        print("\n[2] 付款：申请 → 审批 → 到账 → 冲销")
        code, pay = req("POST", "/api/v1/payments", "finance", {
            "supplier_id": sup, "amount": "10600.00", "method": "bank_transfer",
            "payee_name": fx["supplier_name"], "payee_bank": "招商银行",
            "payee_account": "6225****0001",
            "allocations": [{"settlement_id": stl, "amount": "10600.00"}],
        })
        check("创建付款 201", code == 201, f"got {code} {pay}")
        pay_id = pay.get("id") if isinstance(pay, dict) else None
        check("状态 pending", pay.get("status") == "pending", str(pay))

        code, r = req("POST", f"/api/v1/payments/{pay_id}/approve", "finance")
        check("pending → approved", code == 200 and r.get("status") == "approved", f"{code} {r}")

        code, r = req("POST", f"/api/v1/payments/{pay_id}/settle", "finance",
                      {"voucher_no": "VCH-E2E-0001"})
        check("approved → succeeded", code == 200 and r.get("status") == "succeeded", f"{code} {r}")

        code, m = req("GET", f"/api/v1/finance/match?period={PERIOD}", "finance")
        row = next((x for x in m.get("rows", []) if x["settlement_id"] == stl), {})
        check("到账后 → settled", row.get("match_state") == "settled", str(row))
        check("未付余额归零", str(row.get("outstanding")) == "0.00", str(row))

        code, s = req("GET", "/api/v1/settlements?status=paid", "finance")
        check("结算单被自动推进为 paid",
              any(x["id"] == stl for x in s.get("rows", [])), str(s.get("total")))

        # 重复流水号：换一张付款单，用同一个回单号
        code, pay2 = req("POST", "/api/v1/payments", "finance", {
            "supplier_id": sup, "amount": "1.00", "method": "bank_transfer",
        })
        code, dupv = req("POST", f"/api/v1/payments/{pay2.get('id')}/approve", "finance")
        code, dupv = req("POST", f"/api/v1/payments/{pay2.get('id')}/settle", "finance",
                         {"voucher_no": "VCH-E2E-0001"})
        check("重复银行流水号被拒（422）", code == 422, f"got {code} {dupv}")

        code, r = req("POST", f"/api/v1/payments/{pay_id}/reverse", "finance")
        check("succeeded → reversed", code == 200 and r.get("status") == "reversed", f"{code} {r}")

        code, m = req("GET", f"/api/v1/finance/match?period={PERIOD}", "finance")
        row = next((x for x in m.get("rows", []) if x["settlement_id"] == stl), {})
        check("冲销后 → 退回 ready_to_pay", row.get("match_state") == "ready_to_pay", str(row))
        code, s = req("GET", "/api/v1/settlements?status=invoiced", "finance")
        check("结算单退回 invoiced",
              any(x["id"] == stl for x in s.get("rows", [])), str(s.get("total")))

        # 冲销后必须能重新付款。若核销表把已冲销的分配仍算作占用，
        # 这笔应付款会被永久锁死 —— 这是账务系统里很难事后补救的死结。
        code, pay3 = req("POST", "/api/v1/payments", "finance", {
            "supplier_id": sup, "amount": "10600.00", "method": "bank_transfer",
            "allocations": [{"settlement_id": stl, "amount": "10600.00"}],
        })
        check("冲销后可重新发起付款", code == 201, f"got {code} {pay3}")
        req("POST", f"/api/v1/payments/{pay3.get('id')}/approve", "finance")
        code, r = req("POST", f"/api/v1/payments/{pay3.get('id')}/settle", "finance",
                      {"voucher_no": "VCH-E2E-0002"})
        check("重新付款可再次到账", code == 200 and r.get("status") == "succeeded", f"{code} {r}")
        code, m = req("GET", f"/api/v1/finance/match?period={PERIOD}", "finance")
        row = next((x for x in m.get("rows", []) if x["settlement_id"] == stl), {})
        check("重新到账后回到 settled", row.get("match_state") == "settled", str(row))

        # -- 3. 权限与隔离 --------------------------------------------------
        print("\n[3] 权限与横向隔离")
        code, r = req("POST", "/api/v1/payments", "project_manager", {
            "supplier_id": sup, "amount": "1.00",
        })
        check("项目经理无法发起付款（404）", code == 404, f"got {code}")

        code, r = req("POST", f"/api/v1/invoices/{inv_id}/verify", "project_manager")
        check("项目经理无法核验发票（404）", code == 404, f"got {code}")

        code, r = req("GET", "/api/v1/invoices", "annotator")
        check("标注员读发票返回 404", code == 404, f"got {code}")

        code, r = req("GET", "/api/v1/invoices", f"supplier_admin@{fx['other_supplier_id']}")
        ok = code == 200 and not any(x["id"] == inv_id for x in r.get("rows", []))
        check("其他供应商看不到本供应商发票", ok, f"got {code}")

        code, r = req("GET", "/api/v1/invoices", f"supplier_admin@{sup}")
        ok = code == 200 and any(x["id"] == inv_id for x in r.get("rows", []))
        check("本供应商可读自己的发票", ok, f"got {code}")

        # -- 4. 幂等 --------------------------------------------------------
        print("\n[4] 幂等重放")
        key = f"e2e-{uuid.uuid4().hex[:8]}"
        body = {"supplier_id": sup, "invoice_no": f"E2E-IDEM-{key}",
                "amount": "10.00", "tax_rate": "0.06"}
        c1, r1 = req("POST", "/api/v1/invoices", "finance", body,
                     {"Idempotency-Key": key})
        c2, r2 = req("POST", "/api/v1/invoices", "finance", body,
                     {"Idempotency-Key": key})
        check("首次创建成功", c1 == 201, f"{c1} {r1}")
        check("重放返回同一 id", r1.get("id") == r2.get("id"), f"{r1} vs {r2}")

        # -- 5. 审计 --------------------------------------------------------
        print("\n[5] 审计留痕")
        code, a = req("GET", "/api/v1/audit?limit=200", "platform_admin")
        actions = {x["action"] for x in a.get("rows", [])}
        check("记录 invoice.verify", "invoice.verify" in actions, str(sorted(actions))[:200])
        check("记录 payment.settle", "payment.settle" in actions)
        check("记录 payment.reverse", "payment.reverse" in actions)
        check("记录 settlement.advance（发票核验联动）", "settlement.advance" in actions)

        # -- 6. 采集 --------------------------------------------------------
        print("\n[6] 采集：准入 → 任务 → 脚本 → 场次 → 质检")
        code, c1r = req("POST", "/api/v1/collectors", "project_manager", {
            "code": "COL-E2E-A", "name": "端到端采集员A", "city": "上海",
            "accents": ["吴语", "普通话"], "skills": ["audio"],
            "id_verified": False, "agreement_signed": True,
        })
        check("创建采集员 201", code == 201, f"got {code} {c1r}")
        ca = c1r.get("id")
        code, r = req("POST", f"/api/v1/collectors/{ca}/status", "project_manager",
                      {"status": "active"})
        check("未实名采集员不得上岗（422）", code == 422, f"got {code} {r}")

        code, c2r = req("POST", "/api/v1/collectors", "project_manager", {
            "code": "COL-E2E-B", "name": "端到端采集员B", "city": "杭州",
            "accents": ["普通话"], "skills": ["audio"],
            "id_verified": True, "agreement_signed": True,
        })
        cb = c2r.get("id")
        code, r = req("POST", f"/api/v1/collectors/{cb}/status", "project_manager",
                      {"status": "active"})
        check("实名后可置为 active", code == 200 and r.get("status") == "active", f"{code} {r}")

        code, t = req("POST", "/api/v1/collection/tasks", "project_manager", {
            "project_id": fx["project_id"], "code": "CT-E2E-01",
            "name": "端到端车内语音采集", "modality": "audio",
            "scene": "车内怠速", "device_requirement": "48kHz/16bit 单声道",
            "script_type": "read", "target_sessions": 2, "target_valid_clips": 100,
            "unit_price": "3.5", "budget": "5000.00",
        })
        check("创建采集任务 201", code == 201, f"got {code} {t}")
        ct = t.get("id")

        code, r = req("POST", f"/api/v1/collection/tasks/{ct}/status", "project_manager",
                      {"status": "running"})
        check("draft → running 被状态机拒绝（409）", code == 409, f"got {code} {r}")
        req("POST", f"/api/v1/collection/tasks/{ct}/status", "project_manager",
            {"status": "recruiting"})
        code, r = req("POST", f"/api/v1/collection/tasks/{ct}/status", "project_manager",
                      {"status": "running"})
        check("recruiting → running", code == 200 and r.get("status") == "running", f"{code} {r}")

        code, r = req("POST", f"/api/v1/collection/tasks/{ct}/scripts", "project_manager", {
            "items": [
                {"seq": 1, "content": "请说出今天的目的地", "category": "导航", "difficulty": "easy"},
                {"seq": 2, "content": "把空调温度调到二十四度", "category": "车控", "difficulty": "normal"},
                {"seq": 3, "content": "找一下附近评价最高的川菜馆", "category": "搜索", "difficulty": "hard"},
            ]
        })
        check("批导脚本 3 条", code == 200 and r.get("upserted") == 3, f"{code} {r}")
        code, r = req("GET", f"/api/v1/collection/tasks/{ct}/scripts", "project_manager")
        rows = r.get("rows", [])
        check("脚本按 seq 有序返回", [x["seq"] for x in rows] == [1, 2, 3], str(rows)[:160])

        code, r = req("POST", "/api/v1/collection/sessions", "project_manager", {
            "task_id": ct, "collector_id": cb, "site": "张江基地 A2", "city": "上海",
            "room": "录音间1", "device": "Zoom H6", "sample_rate": 48000, "channels": 1,
        })
        check("场次排期 201", code == 201, f"got {code} {r}")
        cs = r.get("id")

        code, dup = req("POST", "/api/v1/collection/sessions", "project_manager", {
            "task_id": ct, "collector_id": cb, "site": "张江基地 A2",
        })
        check("同一采集员并行排期被拒（422）", code == 422, f"got {code} {dup}")

        code, r = req("POST", f"/api/v1/collection/sessions/{cs}/submit", "project_manager", {
            "duration_min": 60, "clip_count": 60, "valid_clip_count": 55,
            "rejected_clip_count": 5, "object_prefix": f"{TENANT}/{ct}/s1",
            "payout": "192.50",
        })
        check("场次提交 200", code == 200 and r.get("status") == "submitted", f"{code} {r}")

        code, r = req("POST", f"/api/v1/collection/sessions/{cs}/qc", "reviewer",
                      {"result": "pass", "note": "噪声底噪达标"})
        check("质检通过 → accepted", code == 200 and r.get("status") == "accepted", f"{code} {r}")

        code, r = req("GET", "/api/v1/collection/tasks?status=running", "project_manager")
        row = next((x for x in r.get("rows", []) if x["task_id"] == ct), {})
        check("任务完成场次自动回写为 1", row.get("done_sessions") == 1, str(row))
        check("有效片段数自动回写为 55", str(row.get("valid_clips")) == "55")
        check("已花费自动回写为 192.50", str(row.get("spent")) == "192.50", str(row))

        code, r = req("GET", "/api/v1/collectors?status=active", "project_manager")
        row = next((x for x in r.get("rows", []) if x["id"] == cb), {})
        check("采集员累计场次回写", row.get("sessions_done") == 1, str(row))

        code, r = req("GET", "/api/v1/collection/summary", "project_manager")
        check("采集汇总可读", code == 200 and "tasks" in r, f"got {code}")

        # -- 7. 审核 / 绩效 / 资产 ------------------------------------------
        print("\n[7] 审核 / 绩效 / 资产入湖")
        code, r = req("GET", "/api/v1/reviews", "reviewer")
        check("审核记录可读", code == 200 and "rows" in r, f"got {code}")
        code, r = req("GET", "/api/v1/reviews/quality", "reviewer")
        ok = code == 200 and all("defect_rate_pct" in x for x in r.get("rows", [])) \
            if r.get("rows") else code == 200
        check("缺陷率按阶段可算", ok, f"got {code} {str(r)[:160]}")

        code, r = req("GET", "/api/v1/performance/live", "ops_director")
        rows = r.get("rows", [])
        check("实时绩效可算且已排名",
              code == 200 and all("rank" in x for x in rows), f"got {code}")

        code, r = req("POST", "/api/v1/assets/import", "data_engineer", {
            "project_id": fx["project_id"], "batch_no": "E2E-BATCH", "name": "e2e-audio.zip",
            "modality": "audio", "size_mb": 128, "item_count": 1200,
            "storage_key": f"{TENANT}/{fx['project_id']}/E2E-BATCH/e2e-audio.zip",
        })
        check("资产入湖登记 201", code == 201 and not r.get("duplicate"), f"got {code} {r}")
        code, r2 = req("POST", "/api/v1/assets/import", "data_engineer", {
            "project_id": fx["project_id"], "batch_no": "E2E-BATCH", "name": "e2e-audio.zip",
            "modality": "audio", "size_mb": 128, "item_count": 1200,
        })
        check("重复登记返回既有记录（幂等）",
              code == 201 and r2.get("duplicate") and r2.get("id") == r.get("id"),
              f"{code} {r2}")

    finally:
        teardown()

    total = PASS + FAIL
    print(f"\n结果：{PASS}/{total} 通过，{FAIL} 失败")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
