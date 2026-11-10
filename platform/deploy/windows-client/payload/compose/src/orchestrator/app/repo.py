"""
FORGE Orchestrator · 业务仓储（真实 SQL）
================================================================
所有查询都在 tenant_conn 提供的事务里执行，RLS 上下文已就绪。
因此这里的 where 子句只做「业务过滤」，不承担隔离职责。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from .fsm import (TaskState, check_supplier_transition, check_task_transition,
                  next_settlement_state)
from .security import Principal

# 任务行里补三个「名字」字段：前端表格一定要显示项目名和人名，
# 让它拿 id 自己在浏览器里 join 是错的 —— 一是要多拉两张全表，
# 二是 RLS 在服务端已经裁过一次，前端拿到的可能本来就不全。
# 这里用 left join 而不是 join：项目被删、成员被移出时任务还要能列出来，
# 只是名字为空，不能因为一个悬空外键让整行从列表里消失。
TASK_COLS = """
    t.id, t.enterprise_id, t.project_id, t.batch_no, t.name, t.modality, t.status,
    t.supplier_id, t.assignee_id, t.item_count, t.done_count, t.rework_round,
    t.unit_price, t.due_at, t.ls_project_id, t.priority, t.created_at, t.updated_at,
    p.name as project_name,
    p.code as project_code,
    m.name as assignee_name,
    -- 花名册成员绑定的登录账号 id（012 user_id）。前端「我的任务」过滤
    -- 必须拿它跟账号 id 比 —— assignee_id 存的是 mem_*，与 usr_* 天然不同，
    -- 直比会把自己的任务全滤没（实测踩过：我的批次恒 0）。
    m.user_id as assignee_user_id,
    s.name as supplier_name
"""

TASK_FROM = """
    from forge.tasks t
    left join forge.projects p on p.id = t.project_id
    left join forge.supplier_members m on m.id = t.assignee_id
    left join forge.suppliers s on s.id = t.supplier_id
"""


def _ser_task(r: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(r)
    for k in ("due_at", "created_at", "updated_at"):
        if r.get(k) is not None and hasattr(r[k], "isoformat"):
            r[k] = r[k].isoformat()
    if isinstance(r.get("unit_price"), Decimal):
        r["unit_price"] = str(r["unit_price"])
    return r


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
async def get_task(conn: Any, task_id: str) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        f"select {TASK_COLS} {TASK_FROM} where t.id = %s", (task_id,))
    row = await cur.fetchone()
    return _ser_task(row) if row else None


async def list_tasks(conn: Any, project_id: Optional[str] = None,
                     status: Optional[str] = None, supplier_id: Optional[str] = None,
                     limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    where, params = ["true"], {"limit": limit, "offset": offset}
    if project_id:
        where.append("t.project_id = %(project_id)s")
        params["project_id"] = project_id
    if status:
        where.append("t.status = %(status)s")
        params["status"] = status
    if supplier_id:
        where.append("t.supplier_id = %(supplier_id)s")
        params["supplier_id"] = supplier_id
    clause = " and ".join(where)

    cur = await conn.execute(
        f"select count(*) as n from forge.tasks t where {clause}", params)
    total = (await cur.fetchone())["n"]

    cur = await conn.execute(
        f"select {TASK_COLS} {TASK_FROM} where {clause} "
        "order by t.due_at nulls last, t.id limit %(limit)s offset %(offset)s", params)
    rows = [_ser_task(r) for r in await cur.fetchall()]
    return {"rows": rows, "total": total, "page": offset // max(limit, 1) + 1,
            "page_size": limit}


async def assign_task(conn: Any, p: Principal, task_id: str,
                      supplier_id: str, assignee_id: str,
                      note: str = "", ip: Optional[str] = None) -> Dict[str, Any]:
    """分配/改派。校验成员归属 + 状态机 + 留痕三步缺一不可。"""
    task = await get_task(conn, task_id)
    if task is None:
        return {"ok": False, "reason": "not_found"}

    # 成员必须属于该供应商，且处于可用状态（RLS 保证看不到别家的成员）
    cur = await conn.execute(
        "select id, supplier_id, status from forge.supplier_members where id = %s",
        (assignee_id,),
    )
    member = await cur.fetchone()
    if member is None:
        return {"ok": False, "reason": "member_not_found"}
    if member["supplier_id"] != supplier_id:
        return {"ok": False, "reason": "member_supplier_mismatch"}
    if member["status"] != "active":
        return {"ok": False, "reason": "member_inactive"}

    check_task_transition(task["status"], TaskState.assigned)

    # 关闭上一条分配记录（改派留痕）
    await conn.execute(
        """
        update forge.task_assignments set unassigned_at = now()
         where task_id = %s and unassigned_at is null
        """,
        (task_id,),
    )
    await conn.execute(
        """
        update forge.tasks
           set status = 'assigned', supplier_id = %s, assignee_id = %s, updated_at = now()
         where id = %s
        """,
        (supplier_id, assignee_id, task_id),
    )
    await conn.execute(
        """
        insert into forge.task_assignments
               (enterprise_id, task_id, supplier_id, assignee_id, assigned_by)
        values (%s, %s, %s, %s, %s)
        """,
        (p.tenant_id, task_id, supplier_id, assignee_id, p.sub),
    )

    from .db import write_audit
    await write_audit(conn, p, "task.assign", task_id,
                      f"{task['status']} -> assigned · {assignee_id} {note}".strip(), ip)
    return {"ok": True, "status": "assigned", "supplier_id": supplier_id,
            "assignee_id": assignee_id, "previous_status": task["status"]}


async def submit_task(conn: Any, p: Principal, task_id: str, item_count: int = 0,
                      ls_annotation_ref: Optional[str] = None,
                      ip: Optional[str] = None) -> Dict[str, Any]:
    task = await get_task(conn, task_id)
    if task is None:
        return {"ok": False, "reason": "not_found"}
    check_task_transition(task["status"], TaskState.submitted)

    done = max(0, min(item_count or task["item_count"], task["item_count"]))
    await conn.execute(
        "update forge.tasks set status = 'submitted', done_count = %s, updated_at = now() where id = %s",
        (done, task_id),
    )
    await conn.execute(
        """
        insert into forge.annotation_submissions
               (id, enterprise_id, task_id, submitter_id, item_count, ls_project_id, ls_annotation_ref)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (f"sub_{task_id}_{int(__import__('time').time())}", p.tenant_id, task_id,
         task["assignee_id"] or p.sub, done, task["ls_project_id"], ls_annotation_ref),
    )

    from .db import write_audit
    await write_audit(conn, p, "task.submit", task_id, f"{done}/{task['item_count']} items", ip)
    return {"ok": True, "status": "submitted", "done_count": done}


async def review_task(conn: Any, p: Principal, task_id: str, stage: str,
                      result: str, note: str = "", sampled: int = 0,
                      defects: int = 0, ip: Optional[str] = None) -> Dict[str, Any]:
    task = await get_task(conn, task_id)
    if task is None:
        return {"ok": False, "reason": "not_found"}

    target = {"pass": TaskState.accepted, "rework": TaskState.rework,
              "reject": TaskState.rejected}[result]
    check_task_transition(task["status"], target)

    rework_round = task["rework_round"] + (1 if result == "rework" else 0)
    await conn.execute(
        "update forge.tasks set status = %s, rework_round = %s, updated_at = now() where id = %s",
        (target.value, rework_round, task_id),
    )
    await conn.execute(
        """
        insert into forge.reviews
               (id, enterprise_id, task_id, stage, result, sampled, defects, note, reviewer_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (f"rev_{task_id}_{int(__import__('time').time())}", p.tenant_id, task_id,
         stage, result, sampled, defects, note, p.sub),
    )

    from .db import write_audit
    await write_audit(conn, p, f"review.{result}", task_id,
                      f"{stage.upper()} sampled={sampled} defects={defects} {note}".strip(), ip)
    return {"ok": True, "status": target.value, "rework_round": rework_round}


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
async def get_settlement(conn: Any, sid: str) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        """
        select id, enterprise_id, supplier_id, code, period, accepted_items,
               unit_price_avg, amount, deduction, payable, status,
               purchase_order, purchase_invoice, payment_entry, created_at, due_at, updated_at
          from forge.settlements where id = %s
        """,
        (sid,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    out = dict(row)
    for k in ("amount", "deduction", "payable", "unit_price_avg"):
        out[k] = str(out[k]) if out[k] is not None else "0"
    for k in ("created_at", "due_at", "updated_at"):
        if out.get(k) is not None and hasattr(out[k], "isoformat"):
            out[k] = out[k].isoformat()
    return out


async def advance_settlement(conn: Any, p: Principal, sid: str,
                             ip: Optional[str] = None) -> Dict[str, Any]:
    st = await get_settlement(conn, sid)
    if st is None:
        return {"ok": False, "reason": "not_found"}

    from .fsm import SETTLEMENT_STEP_PERMS
    nxt = next_settlement_state(st["status"])
    need = SETTLEMENT_STEP_PERMS[(st["status"], nxt)]
    if not p.can(need):
        # 权限不足时按「不存在」处理，避免泄露结算单状态
        return {"ok": False, "reason": "forbidden"}

    await conn.execute(
        "update forge.settlements set status = %s, updated_at = now() where id = %s",
        (nxt, sid),
    )
    from .db import write_audit
    await write_audit(conn, p, "settlement.advance", sid, f"{st['status']} -> {nxt}", ip)
    return {"ok": True, "status": nxt, "previous_status": st["status"]}


async def list_settlements(conn: Any, status: Optional[str] = None,
                           supplier_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        select s.id, s.supplier_id, sup.name as supplier_name,
               s.code, s.period, s.accepted_items, s.unit_price_avg,
               s.amount, s.deduction, s.payable, s.status,
               s.purchase_order, s.purchase_invoice, s.payment_entry,
               s.created_at, s.due_at
          from forge.settlements s
          left join forge.suppliers sup on sup.id = s.supplier_id
         where (%(status)s::text is null or s.status = %(status)s)
           and (%(supplier_id)s::text is null or s.supplier_id = %(supplier_id)s)
         order by s.period desc, s.code
        """,
        {"status": status, "supplier_id": supplier_id},
    )
    rows = []
    for r in await cur.fetchall():
        d = dict(r)
        for k in ("amount", "deduction", "payable", "unit_price_avg"):
            d[k] = str(d[k]) if d[k] is not None else "0"
        for k in ("created_at", "due_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# 项目 / 供应商
# ---------------------------------------------------------------------------
async def list_projects(conn: Any) -> List[Dict[str, Any]]:
    """列名严格对齐 001_schema.sql / v_project_progress，不臆造字段。

    v_project_progress 只带进度统计，项目页要的负责人、承接供应商、
    模板 id、允许返工次数都在 projects 表本体和 project_suppliers 里，
    一并取出来 —— 否则前端只能对这些字段显示「—」。
    """
    cur = await conn.execute(
        """
        select v.project_id as id, v.code, v.name, v.status, p.modality,
               v.total_items, v.delivered_items, v.progress_pct, v.due_at,
               v.task_count, v.accepted_tasks, v.overdue_tasks,
               p.client, p.pass_rate, p.budget, p.unit_price,
               p.labeling_config_id, p.labeling_config_name,
               p.qc_sampling_rate, p.max_rework,
               p.manager_id, u.name as manager_name,
               p.start_at,
               coalesce(
                 (select array_agg(ps.supplier_id order by ps.supplier_id)
                    from forge.project_suppliers ps
                   where ps.project_id = v.project_id),
                 '{}'::text[]
               ) as supplier_ids
          from forge.v_project_progress v
          join forge.projects p on p.id = v.project_id
          left join forge.users u on u.id = p.manager_id
         order by v.due_at nulls last, v.project_id
        """
    )
    rows = []
    for r in await cur.fetchall():
        d = dict(r)
        for k in ("due_at", "start_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        for k in ("progress_pct", "pass_rate"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        for k in ("budget", "unit_price"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        d["supplier_ids"] = list(d.get("supplier_ids") or [])
        rows.append(d)
    return rows


async def list_suppliers(conn: Any, status: Optional[str] = None) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        select id, code, name, short_name, tier, status, modalities, city,
               contact_name, contact_phone,
               capacity_hours, committed_hours, annotator_count,
               quality_score, on_time_rate, rework_rate, contract_count,
               qual_business_license, qual_nda_signed, qual_security_audit,
               qual_last_audited_at, onboarded_at
          from forge.suppliers
         where (%(status)s::text is null or status = %(status)s)
         order by quality_score desc nulls last, id
        """,
        {"status": status},
    )
    rows = []
    for r in await cur.fetchall():
        d = dict(r)
        for k in ("quality_score", "on_time_rate", "rework_rate"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        for k in ("qual_last_audited_at", "onboarded_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        rows.append(d)
    return rows


async def set_supplier_status(conn: Any, p: Principal, supplier_id: str, target: str,
                              ip: Optional[str] = None) -> Dict[str, Any]:
    cur = await conn.execute("select id, status from forge.suppliers where id = %s", (supplier_id,))
    row = await cur.fetchone()
    if row is None:
        return {"ok": False, "reason": "not_found"}
    check_supplier_transition(row["status"], target)
    await conn.execute(
        """
        update forge.suppliers
           set status = %s,
               onboarded_at = case when %s = 'active' then coalesce(onboarded_at, now())
                                   else onboarded_at end,
               updated_at = now()
         where id = %s
        """,
        (target, target, supplier_id),
    )
    from .db import write_audit
    await write_audit(conn, p, f"supplier.{target}", supplier_id,
                      f"{row['status']} -> {target}", ip)
    return {"ok": True, "status": target, "previous_status": row["status"]}


# ---------------------------------------------------------------------------
# 供应商成员
# ---------------------------------------------------------------------------
async def list_members(conn: Any, supplier_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        select m.id, m.enterprise_id, m.supplier_id, s.name as supplier_name,
               m.name, m.initials, m.role, m.skills, m.level, m.status,
               m.pass_rate, m.completed_total, m.active_tasks, m.joined_at,
               m.user_id, u.phone as user_phone
          from forge.supplier_members m
          join forge.suppliers s on s.id = m.supplier_id
          left join forge.users u on u.id = m.user_id
         where (%(supplier_id)s::text is null or m.supplier_id = %(supplier_id)s)
         order by m.active_tasks desc, m.name
        """,
        {"supplier_id": supplier_id},
    )
    rows = []
    for r in await cur.fetchall():
        d = dict(r)
        if d.get("pass_rate") is not None:
            d["pass_rate"] = float(d["pass_rate"])
        for k in ("joined_at",):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        d["skills"] = list(d.get("skills") or [])
        rows.append(d)
    return rows


async def bind_member_user(conn: Any, p: "Principal", member_id: str,
                           phone: str, ip: str = "") -> Dict[str, Any]:
    """把花名册成员绑定到平台登录账号（user_id）。

    为什么需要绑定：任务分配的 assignee 是花名册成员（mem_*），登录与 RLS
    用的是账号（usr_*），两个 id 体系靠 supplier_members.user_id 接起来。
    RLS 的 assignee 匹配在 SQL 层实时解析映射，所以绑定/解绑立即生效，
    不需要等 token 过期或重新登录。

    越权防线：users 不开 RLS（登录表，见 010 迁移注释），这里必须显式
    按 enterprise_id 过滤，否则可以绑到别的租户的账号上。
    """
    from .db import write_audit
    cur = await conn.execute(
        "select id, name, status, enterprise_id from forge.users where phone = %s",
        (phone,))
    user = await cur.fetchone()
    # 跨租户与不存在统一 404：不泄露「这个手机号在别的租户注册过」
    if not user or user["enterprise_id"] != p.tenant_id:
        return {"ok": False, "reason": "user_not_found"}
    if user["status"] != "active":
        return {"ok": False, "reason": "user_inactive"}

    cur = await conn.execute(
        "select id, name, user_id from forge.supplier_members where id = %s",
        (member_id,))
    member = await cur.fetchone()
    if not member:
        return {"ok": False, "reason": "member_not_found"}
    if member["user_id"] and member["user_id"] != user["id"]:
        return {"ok": False, "reason": "member_already_bound"}
    if member["user_id"] == user["id"]:
        return {"ok": True, "member_id": member_id,
                "user_id": user["id"], "user_phone": phone}

    # 一个登录账号只允许绑定一个花名册成员（ux_supplier_members_user 唯一索引兜底）
    cur = await conn.execute(
        "select id, name from forge.supplier_members where user_id = %s and id <> %s",
        (user["id"], member_id))
    if await cur.fetchone():
        return {"ok": False, "reason": "user_already_bound"}

    await conn.execute(
        "update forge.supplier_members set user_id = %s where id = %s",
        (user["id"], member_id))
    await write_audit(conn, p, "member.bind_user", member_id,
                      f"{member['name']} ← {user['name']} {phone}", ip)
    return {"ok": True, "member_id": member_id,
            "user_id": user["id"], "user_phone": phone}


async def unbind_member_user(conn: Any, p: "Principal", member_id: str,
                             ip: str = "") -> Dict[str, Any]:
    """解绑登录账号。解绑后该账号立刻看不到任何批次（RLS 实时查映射）。"""
    from .db import write_audit
    cur = await conn.execute(
        "select id, name, user_id from forge.supplier_members where id = %s",
        (member_id,))
    member = await cur.fetchone()
    if not member:
        return {"ok": False, "reason": "member_not_found"}
    if not member["user_id"]:
        return {"ok": True, "member_id": member_id, "user_id": None}
    await conn.execute(
        "update forge.supplier_members set user_id = null where id = %s",
        (member_id,))
    await write_audit(conn, p, "member.unbind_user", member_id,
                      f"{member['name']} 解绑 {member['user_id']}", ip)
    return {"ok": True, "member_id": member_id, "user_id": None}


# ---------------------------------------------------------------------------
# 新建项目 / 新建供应商
# ---------------------------------------------------------------------------
def _next_code(prefix: str, taken: set) -> str:
    """生成形如 IMG-2612 的业务编号。同一租户内 code 有唯一索引，
    所以撞号必须在这里解决掉，不能让数据库抛 23505 变成 500。"""
    n = 2600
    while True:
        n += 1
        cand = f"{prefix}-{n}"
        if cand not in taken:
            return cand


async def create_project(conn: Any, p: Principal, body: Dict[str, Any],
                         ip: Optional[str] = None) -> Dict[str, Any]:
    cur = await conn.execute("select code from forge.projects")
    taken = {r["code"] for r in await cur.fetchall()}
    modality = body.get("modality") or "text"
    prefix = {"image": "IMG", "video": "VID", "audio": "AUD",
              "text": "TXT", "multimodal": "MMD"}.get(modality, "PRJ")
    code = body.get("code") or _next_code(prefix, taken)
    if code in taken:
        return {"ok": False, "reason": "code_exists"}

    total = int(body.get("total_items") or 0)
    unit_price = body.get("unit_price") or 0
    budget = body.get("budget")
    if budget is None:
        budget = float(unit_price) * total

    # id 用 pg 生成：并发下应用侧自增会撞车，交给数据库最省事
    cur = await conn.execute(
        """
        insert into forge.projects
            (id, enterprise_id, code, name, client, modality, status,
             manager_id, total_items, delivered_items, start_at, due_at,
             unit_price, budget, pass_rate, labeling_config_id,
             labeling_config_name, qc_sampling_rate, max_rework)
        values
            ('prj_' || replace(gen_random_uuid()::text, '-', ''), %s, %s, %s, %s, %s,
             coalesce(%s, 'draft'), %s, %s, 0, now(), %s, %s, %s, 0, %s, %s,
             coalesce(%s, 10), coalesce(%s, 3))
        returning id
        """,
        (p.tenant_id, code, body["name"], body.get("client") or "—", modality,
         body.get("status"), p.sub, total,
         body.get("due_at"), unit_price, budget,
         body.get("labeling_config_id") or "lsc_default",
         body.get("labeling_config_name") or "标准模板",
         body.get("qc_sampling_rate"), body.get("max_rework")),
    )
    pid = (await cur.fetchone())["id"]

    for sid in (body.get("supplier_ids") or []):
        await conn.execute(
            """
            insert into forge.project_suppliers (enterprise_id, project_id, supplier_id)
            values (%s, %s, %s) on conflict do nothing
            """,
            (p.tenant_id, pid, sid),
        )

    from .db import write_audit
    await write_audit(conn, p, "project.create", pid,
                      f"{body['name']}（{code}）", ip)
    cur = await conn.execute(
        "select id, code, name, status, modality from forge.projects where id = %s", (pid,))
    row = dict(await cur.fetchone())
    row["supplier_ids"] = list(body.get("supplier_ids") or [])
    return {"ok": True, "project": row}


async def create_supplier(conn: Any, p: Principal, body: Dict[str, Any],
                          ip: Optional[str] = None) -> Dict[str, Any]:
    cur = await conn.execute("select code from forge.suppliers")
    taken = {r["code"] for r in await cur.fetchall()}
    code = body.get("code") or _next_code("SUP", taken)
    if code in taken:
        return {"ok": False, "reason": "code_exists"}

    cur = await conn.execute(
        """
        insert into forge.suppliers
            (id, enterprise_id, code, name, short_name, tier, status, modalities,
             city, contact_name, contact_phone, capacity_hours, committed_hours,
             annotator_count, quality_score, on_time_rate, rework_rate,
             contract_count)
        values
            ('sup_' || replace(gen_random_uuid()::text, '-', ''), %s, %s, %s, %s,
             coalesce(%s, 'C'), coalesce(%s, 'qualifying'),
             coalesce(%s::text[], '{}'::text[]),
             %s, %s, %s, coalesce(%s, 0), 0, 0, 0, 0, 0, 0)
        returning id
        """,
        (p.tenant_id, code, body["name"],
         body.get("short_name") or body["name"][:4],
         body.get("tier"), body.get("status"),
         body.get("modalities") or [], body.get("city"),
         body.get("contact_name"), body.get("contact_phone"),
         body.get("capacity_hours")),
    )
    sid = (await cur.fetchone())["id"]

    from .db import write_audit
    await write_audit(conn, p, "supplier.create", sid,
                      f"{body['name']}（{code}）", ip)
    cur = await conn.execute(
        "select id, code, name, short_name, tier, status from forge.suppliers where id = %s",
        (sid,))
    return {"ok": True, "supplier": dict(await cur.fetchone())}


# ---------------------------------------------------------------------------
# 标注规范（右侧侧栏的数据源）
# ---------------------------------------------------------------------------
# 存在 forge.project_guidelines 而不是 projects 的列上，原因见 013 迁移注释：
# 这张表要开放给一线标注员读，projects 里有 budget / unit_price 不能开放。
GUIDELINE_COLS = "project_id, markdown, images, updated_by, updated_at"


def _ser_guideline(r: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not r:
        return {"markdown": "", "images": [], "updated_at": None, "updated_by": None}
    return {
        "markdown": r.get("markdown") or "",
        "images": list(r.get("images") or []),
        "updated_by": r.get("updated_by"),
        "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


async def get_guideline(conn: Any, project_id: str) -> Dict[str, Any]:
    """按项目 id 取规范。没配过就返回空结构（不是 404）：
    前端要区分「没配」和「项目不存在」，后者才该 404。"""
    cur = await conn.execute(
        f"select {GUIDELINE_COLS} from forge.project_guidelines where project_id = %s",
        (project_id,))
    return _ser_guideline(await cur.fetchone())


async def guideline_for_ls_project(conn: Any, ls_project_id: int) -> Optional[Dict[str, Any]]:
    """按 LS 项目 id 取规范。先经 tasks 反查平台项目 —— 批次↔LS 项目 1:1，
    反查不到说明这条 LS 项目不是平台建的，返回 None（不猜、不兜底）。

    **projects 必须 left join，不能 inner join**：标注员的 Principal 带
    supplier_id，而 p_projects 只认 project_suppliers 承接关系。inner join
    会把「规范查到了但项目行对他不可见」的情况退化成"没有规范"，
    于是一线标注员永远看到空侧栏。left join 保留任务侧的行，
    项目名拿不到就是 null —— 反正侧栏也不靠它显示。
    """
    cur = await conn.execute(
        """
        select g.project_id, g.markdown, g.images, g.updated_by, g.updated_at,
               p.name as project_name, p.code as project_code
          from forge.tasks t
          left join forge.projects p on p.id = t.project_id
          left join forge.project_guidelines g on g.project_id = t.project_id
         where t.ls_project_id = %s
         order by t.id
         limit 1
        """,
        (ls_project_id,))
    row = await cur.fetchone()
    if not row:
        return None
    d = _ser_guideline(row if row.get("project_id") else None)
    d["project_name"] = row.get("project_name")
    d["project_code"] = row.get("project_code")
    return d


async def save_guideline(conn: Any, p: Principal, project_id: str,
                         markdown: str, images: List[str], ip: str = "") -> Dict[str, Any]:
    """写规范。写操作只给内部人员（RLS 的 with check 已兜底），
    这里再按 project:write 语义做一次业务校验 + 审计。"""
    from .db import write_audit
    cur = await conn.execute(
        "select name, code from forge.projects where id = %s", (project_id,))
    proj = await cur.fetchone()
    if not proj:
        return {"ok": False, "reason": "project_not_found"}
    await conn.execute(
        """
        insert into forge.project_guidelines
               (project_id, enterprise_id, markdown, images, updated_by, updated_at)
        values (%s, %s, %s, %s, %s, now())
        on conflict (project_id) do update
           set markdown = excluded.markdown,
               images = excluded.images,
               updated_by = excluded.updated_by,
               updated_at = now()
        """,
        (project_id, p.tenant_id, markdown, images, p.sub),
    )
    await write_audit(conn, p, "project.guideline_save", project_id,
                      f"{proj['code']} 规范 {len(markdown)} 字 / {len(images)} 图", ip)
    cur = await conn.execute(
        f"select {GUIDELINE_COLS} from forge.project_guidelines where project_id = %s",
        (project_id,))
    return {"ok": True, "guideline": _ser_guideline(await cur.fetchone())}


# ---------------------------------------------------------------------------
# 项目 ↔ 标注引擎绑定
# ---------------------------------------------------------------------------
# 表单结构（label_config）不在这里，也不在平台库里：正本在 Label Studio，
# 甲方通过一次性票据进 LS 原生编辑器改。平台只记「这个项目连到哪个 LS 项目」。


async def project_brief(conn: Any, project_id: str) -> Optional[Dict[str, Any]]:
    """项目最小信息（编号/名称/LS 绑定）。

    绑定状态读取、设计票据签发都用它；顺带把 labeling_config_id 带出来，
    避免调用方再查一次。
    """
    cur = await conn.execute(
        "select id, code, name, labeling_config_id, labeling_config_name "
        "from forge.projects where id = %s", (project_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_project_ls(conn: Any, p: Principal, project_id: str,
                         ls_project_id: int, config_name: str,
                         ip: str = "") -> None:
    """把「平台项目 ↔ LS 项目」的绑定写回 projects。

    写的是 projects.labeling_config_id —— 这一列在 001 schema 里就是存
    LS 项目 id 的（provision.py 脚本也是往这里写），不是"某个配置的 id"。
    历史命名有点误导，但改列名要动导出与下游，不值得。
    """
    from .db import write_audit
    await conn.execute(
        "update forge.projects set labeling_config_id = %s, labeling_config_name = %s,"
        " updated_at = now() where id = %s",
        (str(ls_project_id), config_name, project_id))
    await write_audit(conn, p, "project.ls_bind", project_id,
                      f"绑定标注引擎项目 #{ls_project_id}（{config_name}）", ip)


# ---------------------------------------------------------------------------
# 与标注引擎（Label Studio）对账 / 回写
# ---------------------------------------------------------------------------
# 这里只做「平台 → 数据库」的落账；调 LS HTTP 的部分在 main.py，
# 因为 repo 层不该知道网络细节（测试时也好替换）。
LS_TERMINAL_STATUS = {"accepted", "rejected"}


async def tasks_with_ls(conn: Any, p: Principal) -> List[Dict[str, Any]]:
    """所有已接入标注引擎的批次（带项目名，供对账展示）。"""
    cur = await conn.execute(
        f"select {TASK_COLS} {TASK_FROM} "
        "where t.ls_project_id is not null "
        "order by t.due_at nulls last, t.id")
    return [_ser_task(r) for r in await cur.fetchall()]


def diff_ls(task: Dict[str, Any], ls: Dict[str, Any]) -> Dict[str, Any]:
    """算一行差异。纯函数，方便单测与前端直接展示。

    三个差异各自含义不同，不能混为一谈：
      · items   —— 平台登记的条目数 vs LS 里实际导入的条目数
                    （不等说明「数据还没导全」或「合同条数写错了」）
      · done    —— 平台累计完成数 vs LS 已标注条目数
      · title   —— 两边名字不一致，一线最容易搞混
    """
    item_count = int(task.get("item_count") or 0)
    done_count = int(task.get("done_count") or 0)
    ls_tasks = int(ls.get("tasks") or 0)
    ls_done = int(ls.get("completed") or 0)

    flags = []
    if ls_tasks and item_count != ls_tasks:
        flags.append("items")
    if done_count != ls_done:
        flags.append("done")
    if (ls.get("title") or "") != desired_ls_title(task):
        flags.append("title")
    return {
        "task_id": task["id"],
        "batch_no": task.get("batch_no"),
        "name": task.get("name"),
        "project_name": task.get("project_name"),
        "status": task.get("status"),
        # item_count 是合同口径登记值，和 ls_tasks 一起给出去，
        # 前端才能显示「平台 500 条 / LS 只有 2 条」这种一眼可读的对比。
        "item_count": item_count,
        "done_count": done_count,
        "ls_project_id": task.get("ls_project_id"),
        "ls_title": ls.get("title"),
        "ls_tasks": ls_tasks,
        "ls_done": ls_done,
        "ls_annotations": int(ls.get("annotations") or 0),
        "flags": flags,
        "in_sync": not flags,
    }


def desired_ls_title(task: Dict[str, Any]) -> str:
    """LS 项目标题以平台为准：`批次号 · 批次名`。"""
    return f"{task.get('batch_no') or ''} · {task.get('name') or ''}".strip(" ·")


async def apply_ls_progress(conn: Any, p: Principal, task: Dict[str, Any],
                            ls: Dict[str, Any], source: str,
                            ip: Optional[str] = None,
                            align_items: bool = False) -> Dict[str, Any]:
    """把 LS 的实测进度写回平台批次。幂等：同一份数据反复同步结果不变。

    口径与边界（为什么这么写）：
      · 完成数取 LS 的「已标注条目数」，不是条目总数、也不是标注记录数；
      · 不覆盖 item_count —— 那是合同口径。LS 条数只在平台还没登记（0）时
        才作为初始值，避免一次同步把商务数字改掉；
        `align_items=True` 是显式例外（运营确认过按 LS 实际条数走）；
      · 已终态（accepted/rejected）的批次不回退，验收结论不能被同步冲掉；
      · 只往前推状态：0 → in_progress，做满 → submitted。
    """
    item_count = int(task.get("item_count") or 0)
    ls_tasks = int(ls.get("tasks") or 0)
    ls_done = int(ls.get("completed") or 0)
    old_item_count = item_count
    old_done = int(task.get("done_count") or 0)

    if (item_count <= 0 or align_items) and ls_tasks > 0:
        item_count = ls_tasks
        await conn.execute(
            "update forge.tasks set item_count = %s where id = %s",
            (item_count, task["id"]))

    done = max(0, min(ls_done, item_count)) if item_count else ls_done
    status = task.get("status") or "pending"
    new_status = status
    if status not in LS_TERMINAL_STATUS:
        if item_count and done >= item_count:
            new_status = "submitted"
        elif done > 0 and status in ("pending", "assigned"):
            new_status = "in_progress"

    # 幂等短路：数字没变就既不写库也不留审计。
    # 定时对账每 5 分钟跑一遍，如果每次无条件写一条审计，
    # 一个批次一天就是 288 条噪音，真出事时审计表根本没法看。
    changed = done != old_done or new_status != status or item_count != old_item_count
    if not changed:
        return {"ok": True, "done_count": done, "item_count": item_count,
                "status": new_status, "previous_status": status,
                "changed": False}

    await conn.execute(
        "update forge.tasks set done_count = %s, status = %s, updated_at = now() "
        "where id = %s",
        (done, new_status, task["id"]))

    from .db import write_audit
    await write_audit(
        conn, p, "task.sync", task["id"],
        f"{source}: ls={ls_done}/{ls_tasks} platform={old_done}/"
        f"{task.get('item_count')} → {done}/{item_count} · {status}→{new_status}"
        + (" · items 按 LS 对齐" if align_items else ""), ip)
    return {"ok": True, "done_count": done, "item_count": item_count,
            "status": new_status, "previous_status": status,
            "changed": True}


# ---------------------------------------------------------------------------
# 单题耗时统计（喂效率与计价模型）
# ---------------------------------------------------------------------------
async def task_efficiency(conn: Any, task_id: str) -> Dict[str, Any]:
    """一个批次的单题耗时汇总。

    口径（为什么这么算）：
      · 只认 leave / submit 上的 duration_ms —— enter 是"开始计时"的标记，
        它即使带了 0 也不能进统计，否则会把每题耗时覆盖成 0。
      · 同一题可能有多条 leave（来回切题），取**最后一次**记录，
        否则来回翻题会把总时长翻倍。
      · 平均耗时按题算（不是按记录算），并给出 p50/p90：
        平均值会被极慢的题拉飞，p90 才看得出「这批题是不是普遍难」。
      · 有效题 = 有时长记录的题；上报了 enter 但没有时长的不计入。
      · 「汇总」和「按人」必须落在同一份每题去重集合上，
        否则会出现总数与按人相加对不上的自相矛盾。
    """
    # 每题取一条的 CTE 两处复用（汇总 / 按人），保证「总数」和「按人相加」
    # 落在同一份集合上 —— 否则会出现「累计 0.1 分、按人 0.6 分」这种自相矛盾。
    # distinct on 必须 order by 同列打头，再按时间倒序，才拿到最后一次读数。
    per_task_cte = """
        with per_task as (
          select distinct on (ls_task_id)
                 ls_task_id, user_id, duration_ms
            from forge.task_time_logs
           where task_id = %s
             and duration_ms is not null
             and event in ('leave', 'submit')
           -- 时间是从"进入该题"开始累计的，所以最后一次离开/提交的读数才是
           -- 这题的真实耗时；来回翻题时会有多条 leave，只留最后一条。
           order by ls_task_id, created_at desc
        )
    """
    cur = await conn.execute(
        per_task_cte + """
        select count(*)                                          as items,
               coalesce(round(avg(duration_ms) / 1000.0, 1), 0)   as avg_seconds,
               -- 秒是**精确口径**，分只是给对账用的展示口径。
               -- 分被 round 到 0.1（= 6 秒粒度），2 题各 2 秒会被写成 0.1 分，
               -- 前端一旦拿分反算秒就会显示成 6 秒 —— 所以两个都给。
               coalesce(round(sum(duration_ms) / 1000.0, 1), 0) as total_seconds,
               coalesce(round(sum(duration_ms) / 60000.0, 1), 0) as total_minutes,
               coalesce(percentile_cont(0.5) within group (order by duration_ms), 0) as p50_ms,
               coalesce(percentile_cont(0.9) within group (order by duration_ms), 0) as p90_ms
          from per_task
        """,
        (task_id,))
    agg = dict(await cur.fetchone() or {})

    cur = await conn.execute(
        per_task_cte + """
        select p.user_id, coalesce(u.name, p.user_id) as user_name,
               count(*)                               as items,
               coalesce(round(sum(p.duration_ms) / 1000.0, 1), 0) as seconds,
               coalesce(round(sum(p.duration_ms) / 60000.0, 1), 0) as minutes
          from per_task p
          left join forge.users u on u.id = p.user_id
         group by p.user_id, u.name
         order by seconds desc, items desc
        """,
        (task_id,))
    by_user = []
    for r in await cur.fetchall():
        d = dict(r)
        d["seconds"] = float(d["seconds"] or 0)
        d["minutes"] = float(d["minutes"] or 0)
        by_user.append(d)

    return {
        "task_id": task_id,
        "items": int(agg.get("items") or 0),
        "avg_seconds": float(agg.get("avg_seconds") or 0),
        "total_seconds": float(agg.get("total_seconds") or 0),
        "total_minutes": float(agg.get("total_minutes") or 0),
        "p50_seconds": round(float(agg.get("p50_ms") or 0) / 1000.0, 1),
        "p90_seconds": round(float(agg.get("p90_ms") or 0) / 1000.0, 1),
        "by_user": by_user,
    }
