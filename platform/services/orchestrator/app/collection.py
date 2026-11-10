"""
FORGE Orchestrator · 数据采集域路由
================================================================
采集与标注是两条并列产线（见 006_collection.sql 的说明），所以这里不
复用 tasks 的状态机，而是走 COLLECTION_TRANSITIONS / SESSION_TRANSITIONS。

关键约束落在库里，这里只做翻译：
  - 未实名 / 未签协议的采集员不得 active（CHECK）
  - 同一采集员不得同时有两场进行中的采集（部分唯一索引）
  - 场次 accepted 后自动回写任务与采集员累计（触发器）
API 层不重复实现这些判断 —— 两处判断必然漂移，漂移之后就没人信了。
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db
from .fsm import check_collection_transition, check_session_transition
from .finance import _money, _row, _ts
from .netutil import client_ip
from .security import Principal, require

router = APIRouter(prefix="/api/v1")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 采集员
# ---------------------------------------------------------------------------
class CollectorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    initials: Optional[str] = None
    gender: Optional[str] = Field(default=None, pattern="^(male|female|other)$")
    birth_year: Optional[int] = Field(default=None, ge=1900, le=2100)
    phone: Optional[str] = None
    city: Optional[str] = None
    accents: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    supplier_id: Optional[str] = None
    id_verified: bool = False
    agreement_signed: bool = False


class CollectorStatus(BaseModel):
    status: str = Field(pattern="^(screening|active|paused|blacklisted)$")
    note: str = ""


@router.get("/collectors")
async def list_collectors(status: Optional[str] = None,
                          city: Optional[str] = None,
                          accent: Optional[str] = None,
                          limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                          p: Principal = Depends(require("collection:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select c.id, c.supplier_id, c.code, c.name, c.initials, c.gender, c.birth_year,
                   c.phone, c.city, c.accents, c.skills, c.status, c.sessions_done,
                   c.valid_clips, c.quality_score, c.id_verified, c.agreement_signed,
                   c.joined_at
              from forge.collectors c
             where (%(status)s::text is null or c.status = %(status)s)
               and (%(city)s::text is null or c.city = %(city)s)
               and (%(accent)s::text is null or %(accent)s = any(c.accents))
             order by c.quality_score desc, c.id
             limit %(limit)s offset %(offset)s
            """,
            {"status": status, "city": city, "accent": accent,
             "limit": limit, "offset": offset},
        )
        rows = [_row(r, money=("quality_score",), ts=("joined_at",))
                for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows), "page": offset // limit + 1, "page_size": limit}


@router.post("/collectors", status_code=201)
async def create_collector(body: CollectorCreate, request: Request,
                           p: Principal = Depends(require("collection:write"))):
    cid = _new_id("col")
    async with db.tenant_conn(p) as conn:
        try:
            await conn.execute(
                """
                insert into forge.collectors
                       (id, enterprise_id, supplier_id, code, name, initials, gender,
                        birth_year, phone, city, accents, skills,
                        id_verified, agreement_signed, status)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'screening')
                """,
                (cid, p.tenant_id, body.supplier_id, body.code, body.name, body.initials,
                 body.gender, body.birth_year, body.phone, body.city,
                 body.accents, body.skills, body.id_verified, body.agreement_signed),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "ux_collectors_code" in msg or "unique" in msg.lower():
                raise HTTPException(422, "采集员编号已存在")
            raise
        await db.write_audit(conn, p, "collector.create", cid,
                             f"{body.code} {body.name}", client_ip(request))
    return {"id": cid, "code": body.code, "status": "screening"}


@router.post("/collectors/{cid}/status")
async def set_collector_status(cid: str, body: CollectorStatus, request: Request,
                               p: Principal = Depends(require("collection:write"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, status, id_verified, agreement_signed from forge.collectors where id = %s",
            (cid,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        if body.status == "active" and not (row["id_verified"] and row["agreement_signed"]):
            # 提前拦一次是为了给出可读原因；库里那条 CHECK 才是最终边界
            raise HTTPException(422, "未实名认证或未签授权协议，不得置为 active")
        try:
            await conn.execute(
                "update forge.collectors set status = %s, updated_at = now() where id = %s",
                (body.status, cid))
        except Exception as exc:  # noqa: BLE001
            if "ck_collector_can_work" in str(exc):
                raise HTTPException(422, "未实名认证或未签授权协议，不得置为 active")
            raise
        await db.write_audit(conn, p, f"collector.{body.status}", cid,
                             f"{row['status']} -> {body.status} {body.note}".strip(),
                             client_ip(request))
    return {"ok": True, "status": body.status, "previous_status": row["status"]}


# ---------------------------------------------------------------------------
# 采集任务
# ---------------------------------------------------------------------------
class CollectionTaskCreate(BaseModel):
    project_id: str = Field(min_length=1)
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    modality: str = Field(default="audio",
                          pattern="^(audio|image|video|text|multimodal)$")
    scene: Optional[str] = None
    device_requirement: Optional[str] = None
    environment: Optional[str] = None
    script_type: str = Field(default="read",
                             pattern="^(read|spontaneous|dialogue|keyword|guided)$")
    target_sessions: int = Field(default=0, ge=0)
    target_valid_clips: int = Field(default=0, ge=0)
    target_duration_min: int = Field(default=0, ge=0)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    budget: Decimal = Field(default=Decimal("0"), ge=0)
    start_at: Optional[str] = None
    due_at: Optional[str] = None


@router.get("/collection/tasks")
async def list_collection_tasks(status: Optional[str] = None,
                                project_id: Optional[str] = None,
                                p: Principal = Depends(require("collection:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select task_id, project_id, code, name, modality, scene, status,
                   target_sessions, done_sessions, target_valid_clips, valid_clips,
                   coalesce(session_pct, 0) as session_pct, coalesce(clip_pct, 0) as clip_pct,
                   budget, spent, pending_sessions, failed_sessions, due_at
              from forge.v_collection_progress
             where (%(status)s::text is null or status = %(status)s)
               and (%(project_id)s::text is null or project_id = %(project_id)s)
             order by due_at nulls last, task_id
            """,
            {"status": status, "project_id": project_id},
        )
        rows = [_row(r, money=("budget", "spent", "session_pct", "clip_pct"),
                     ts=("due_at",)) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


@router.post("/collection/tasks", status_code=201)
async def create_collection_task(body: CollectionTaskCreate, request: Request,
                                 p: Principal = Depends(require("collection:write"))):
    tid = _new_id("ct")
    async with db.tenant_conn(p) as conn:
        # 项目必须可见，否则等于往别人的项目里塞采集方案
        cur = await conn.execute("select 1 from forge.projects where id = %s",
                                 (body.project_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, "not found")
        try:
            await conn.execute(
                """
                insert into forge.collection_tasks
                       (id, enterprise_id, project_id, code, name, modality, scene,
                        device_requirement, environment, script_type, target_sessions,
                        target_valid_clips, target_duration_min, unit_price, budget,
                        start_at, due_at, status)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::timestamptz, %s::timestamptz, 'draft')
                """,
                (tid, p.tenant_id, body.project_id, body.code, body.name, body.modality,
                 body.scene, body.device_requirement, body.environment, body.script_type,
                 body.target_sessions, body.target_valid_clips, body.target_duration_min,
                 body.unit_price, body.budget, body.start_at, body.due_at),
            )
        except Exception as exc:  # noqa: BLE001
            if "ux_ct_code" in str(exc) or "unique" in str(exc).lower():
                raise HTTPException(422, "采集任务编号已存在")
            raise
        await db.write_audit(conn, p, "collection.task.create", tid,
                             f"{body.code} {body.name}", client_ip(request))
    return {"id": tid, "code": body.code, "status": "draft"}


class CollectionTaskStatus(BaseModel):
    status: str = Field(pattern="^(recruiting|running|paused|completed|cancelled)$")
    note: str = ""


@router.post("/collection/tasks/{tid}/status")
async def set_collection_task_status(tid: str, body: CollectionTaskStatus, request: Request,
                                     p: Principal = Depends(require("collection:write"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, status from forge.collection_tasks where id = %s", (tid,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        check_collection_transition(row["status"], body.status)
        await conn.execute(
            "update forge.collection_tasks set status = %s, updated_at = now() where id = %s",
            (body.status, tid))
        await db.write_audit(conn, p, f"collection.task.{body.status}", tid,
                             f"{row['status']} -> {body.status} {body.note}".strip(),
                             client_ip(request))
    return {"ok": True, "status": body.status, "previous_status": row["status"]}


# ---------------------------------------------------------------------------
# 采集脚本
# ---------------------------------------------------------------------------
class ScriptItem(BaseModel):
    seq: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=2000)
    category: Optional[str] = None
    difficulty: str = Field(default="normal", pattern="^(easy|normal|hard)$")
    required: bool = True


class ScriptBulk(BaseModel):
    items: List[ScriptItem] = Field(min_length=1)


@router.get("/collection/tasks/{tid}/scripts")
async def list_scripts(tid: str, p: Principal = Depends(require("collection:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select seq, content, category, difficulty, required
              from forge.collection_scripts where task_id = %s order by seq
            """,
            (tid,))
        rows = [dict(r) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


@router.post("/collection/tasks/{tid}/scripts")
async def upsert_scripts(tid: str, body: ScriptBulk, request: Request,
                         p: Principal = Depends(require("collection:write"))):
    """批导脚本用 on conflict 覆盖，允许采集方案反复迭代而不产生重复条目。"""
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute("select 1 from forge.collection_tasks where id = %s", (tid,))
        if await cur.fetchone() is None:
            raise HTTPException(404, "not found")
        for it in body.items:
            await conn.execute(
                """
                insert into forge.collection_scripts
                       (enterprise_id, task_id, seq, content, category, difficulty, required)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (task_id, seq) do update
                   set content = excluded.content,
                       category = excluded.category,
                       difficulty = excluded.difficulty,
                       required = excluded.required
                """,
                (p.tenant_id, tid, it.seq, it.content, it.category,
                 it.difficulty, it.required),
            )
        await db.write_audit(conn, p, "collection.script.upsert", tid,
                             f"{len(body.items)} items", client_ip(request))
    return {"ok": True, "upserted": len(body.items)}


# ---------------------------------------------------------------------------
# 采集场次
# ---------------------------------------------------------------------------
class SessionCreate(BaseModel):
    task_id: str = Field(min_length=1)
    collector_id: str = Field(min_length=1)
    site: Optional[str] = None
    city: Optional[str] = None
    room: Optional[str] = None
    device: Optional[str] = None
    sample_rate: Optional[int] = Field(default=None, gt=0)
    channels: Optional[int] = Field(default=None, gt=0)
    started_at: Optional[str] = None


class SessionSubmit(BaseModel):
    duration_min: int = Field(ge=0, default=0)
    clip_count: int = Field(ge=0, default=0)
    valid_clip_count: int = Field(ge=0, default=0)
    rejected_clip_count: int = Field(ge=0, default=0)
    object_prefix: Optional[str] = None
    payout: Decimal = Field(default=Decimal("0"), ge=0)


class SessionQC(BaseModel):
    result: str = Field(pattern="^(pass|partial|fail)$")
    note: str = ""
    valid_clip_count: Optional[int] = Field(default=None, ge=0)


@router.get("/collection/sessions")
async def list_sessions(task_id: Optional[str] = None,
                        collector_id: Optional[str] = None,
                        status: Optional[str] = None,
                        limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                        p: Principal = Depends(require("collection:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select s.id, s.task_id, t.code as task_code, t.name as task_name,
                   s.collector_id, c.name as collector_name, s.site, s.city, s.room,
                   s.device, s.sample_rate, s.channels, s.started_at, s.ended_at,
                   s.duration_min, s.clip_count, s.valid_clip_count, s.rejected_clip_count,
                   s.qc_status, s.qc_note, s.status, s.payout
              from forge.collection_sessions s
              join forge.collection_tasks t on t.id = s.task_id
              join forge.collectors c on c.id = s.collector_id
             where (%(task_id)s::text is null or s.task_id = %(task_id)s)
               and (%(collector_id)s::text is null or s.collector_id = %(collector_id)s)
               and (%(status)s::text is null or s.status = %(status)s)
             order by s.started_at desc nulls last, s.id
             limit %(limit)s offset %(offset)s
            """,
            {"task_id": task_id, "collector_id": collector_id, "status": status,
             "limit": limit, "offset": offset},
        )
        rows = [_row(r, money=("payout",), ts=("started_at", "ended_at"))
                for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows), "page": offset // limit + 1, "page_size": limit}


@router.post("/collection/sessions", status_code=201)
async def schedule_session(body: SessionCreate, request: Request,
                           p: Principal = Depends(require("collection:write"))):
    sid = _new_id("cs")
    async with db.tenant_conn(p) as conn:
        try:
            await conn.execute(
                """
                insert into forge.collection_sessions
                       (id, enterprise_id, task_id, collector_id, site, city, room,
                        device, sample_rate, channels, started_at, status)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        coalesce(%s::timestamptz, now()), 'scheduled')
                """,
                (sid, p.tenant_id, body.task_id, body.collector_id, body.site,
                 body.city, body.room, body.device, body.sample_rate, body.channels,
                 body.started_at),
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "ux_cs_collector_active" in msg:
                raise HTTPException(422, "该采集员已有进行中的采集场次，不能并行排期")
            if "foreign key" in msg.lower():
                raise HTTPException(404, "采集任务或采集员不存在")
            raise
        await db.write_audit(conn, p, "collection.session.schedule", sid,
                             f"{body.task_id} / {body.collector_id}", client_ip(request))
    return {"id": sid, "status": "scheduled"}


@router.post("/collection/sessions/{sid}/submit")
async def submit_session(sid: str, body: SessionSubmit, request: Request,
                         p: Principal = Depends(require("collection:write"))):
    if body.valid_clip_count + body.rejected_clip_count > body.clip_count:
        raise HTTPException(422, "有效 + 无效片段数不得超过总片段数")
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, status from forge.collection_sessions where id = %s", (sid,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        check_session_transition(row["status"], "submitted")
        await conn.execute(
            """
            update forge.collection_sessions
               set status = 'submitted', ended_at = coalesce(ended_at, now()),
                   duration_min = %s, clip_count = %s, valid_clip_count = %s,
                   rejected_clip_count = %s, object_prefix = %s, payout = %s,
                   updated_at = now()
             where id = %s
            """,
            (body.duration_min, body.clip_count, body.valid_clip_count,
             body.rejected_clip_count, body.object_prefix, body.payout, sid),
        )
        await db.write_audit(conn, p, "collection.session.submit", sid,
                             f"clips={body.clip_count} valid={body.valid_clip_count}",
                             client_ip(request))
    return {"ok": True, "status": "submitted"}


@router.post("/collection/sessions/{sid}/qc")
async def qc_session(sid: str, body: SessionQC, request: Request,
                     p: Principal = Depends(require("review:decide"))):
    target = {"pass": "accepted", "partial": "accepted", "fail": "rejected"}[body.result]
    qc = {"pass": "passed", "partial": "partial", "fail": "failed"}[body.result]
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, status, valid_clip_count from forge.collection_sessions where id = %s",
            (sid,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        check_session_transition(row["status"], target)

        valid = body.valid_clip_count if body.valid_clip_count is not None \
            else row["valid_clip_count"]
        await conn.execute(
            """
            update forge.collection_sessions
               set status = %s, qc_status = %s, qc_note = %s,
                   valid_clip_count = %s, updated_at = now()
             where id = %s
            """,
            (target, qc, body.note, valid, sid),
        )
        await db.write_audit(conn, p, f"collection.session.qc.{body.result}", sid,
                             f"{row['status']} -> {target} {body.note}".strip(),
                             client_ip(request))
    return {"ok": True, "status": target, "qc_status": qc,
            "previous_status": row["status"]}


@router.get("/collection/summary")
async def collection_summary(p: Principal = Depends(require("collection:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select
              count(*)                                             as tasks_total,
              count(*) filter (where status = 'running')            as tasks_running,
              coalesce(sum(target_sessions), 0)                     as target_sessions,
              coalesce(sum(done_sessions), 0)                       as done_sessions,
              coalesce(sum(target_valid_clips), 0)                  as target_clips,
              coalesce(sum(valid_clips), 0)                         as valid_clips,
              coalesce(sum(budget), 0)                              as budget,
              coalesce(sum(spent), 0)                               as spent
              from forge.collection_tasks
            """
        )
        tasks = _row(await cur.fetchone(), money=("budget", "spent"))
        cur = await conn.execute(
            """
            select count(*) filter (where status = 'active')       as collectors_active,
                   count(*) filter (where status = 'screening')    as collectors_screening,
                   count(*) filter (where status = 'blacklisted')  as collectors_blacklisted
              from forge.collectors
            """
        )
        cols = dict(await cur.fetchone())
        cur = await conn.execute(
            """
            select count(*) filter (where status in ('submitted','qc')) as pending_qc,
                   count(*) filter (where qc_status = 'failed')         as failed_qc,
                   coalesce(sum(payout) filter (where status = 'accepted'), 0) as payout_done
              from forge.collection_sessions
            """
        )
        sess = _row(await cur.fetchone(), money=("payout_done",))
    return {"tasks": tasks, "collectors": cols, "sessions": sess}
