"""
FORGE Orchestrator · 审核 / 绩效 / 资产入湖
================================================================
这三个域此前只有表没有接口（ADR-001 里记为待补）。
放在一起是因为它们共同回答一个问题：交付质量到底靠什么证明 ——
审核记录（reviews）→ 绩效快照（performance_records）→ 入湖资产（assets）。

/assets/import 是预签名直传的收尾动作：文件先直传 MinIO（不经过中间层），
上传完成后由前端回调本接口登记元数据。没有这一步，MinIO 里的对象
就是一堆没人认领的字节。
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db
from .finance import _money, _row
from .netutil import client_ip
from .security import Principal, require

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# 审核记录
# ---------------------------------------------------------------------------
@router.get("/reviews")
async def list_reviews(task_id: Optional[str] = None,
                       stage: Optional[str] = None,
                       result: Optional[str] = None,
                       limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                       p: Principal = Depends(require("review:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select r.id, r.task_id, t.batch_no as task_code, t.name as task_name,
                   p.name as project_name,
                   r.stage, r.result, r.sampled, r.defects, r.note,
                   r.reviewer_id,
                   coalesce(u.name, m.name) as reviewer_name,
                   r.reviewed_at
              from forge.reviews r
              join forge.tasks t on t.id = r.task_id
              left join forge.projects p on p.id = t.project_id
              left join forge.users u on u.id = r.reviewer_id
              left join forge.supplier_members m on m.id = r.reviewer_id
             where (%(task_id)s::text is null or r.task_id = %(task_id)s)
               and (%(stage)s::text is null or r.stage = %(stage)s)
               and (%(result)s::text is null or r.result = %(result)s)
             order by r.reviewed_at desc
             limit %(limit)s offset %(offset)s
            """,
            {"task_id": task_id, "stage": stage, "result": result,
             "limit": limit, "offset": offset},
        )
        rows = [_row(r, ts=("reviewed_at",)) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows), "page": offset // limit + 1, "page_size": limit}


@router.get("/reviews/quality")
async def review_quality(period: Optional[str] = None,
                         p: Principal = Depends(require("review:read"))):
    """缺陷率按阶段拆开：L1 漏检率高说明供应商自检形同虚设，
    QC 漏检率高说明我们的抽检比例不够。两个数字指向完全不同的动作。"""
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select r.stage,
                   count(*)                                        as reviews,
                   coalesce(sum(r.sampled), 0)                     as sampled,
                   coalesce(sum(r.defects), 0)                     as defects,
                   round(coalesce(sum(r.defects), 0)::numeric
                         / nullif(sum(r.sampled), 0) * 100, 3)     as defect_rate_pct,
                   count(*) filter (where r.result = 'pass')       as passed,
                   count(*) filter (where r.result = 'rework')     as rework,
                   count(*) filter (where r.result = 'reject')     as rejected
              from forge.reviews r
             where (%(period)s::text is null
                    or to_char(r.reviewed_at, 'YYYY-MM') = %(period)s)
             group by r.stage
             order by r.stage
            """,
            {"period": period},
        )
        rows = [_row(r, money=("defect_rate_pct",)) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# 绩效
# ---------------------------------------------------------------------------
@router.get("/performance")
async def list_performance(period: Optional[str] = None,
                           supplier_id: Optional[str] = None,
                           p: Principal = Depends(require("performance:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select pr.id, pr.supplier_id, s.name as supplier_name, s.tier,
                   pr.period, pr.quality_score, pr.on_time_rate, pr.rework_rate,
                   pr.throughput, pr.composite, pr.rank
              from forge.performance_records pr
              join forge.suppliers s on s.id = pr.supplier_id
             where (%(period)s::text is null or pr.period = %(period)s)
               and (%(supplier_id)s::text is null or pr.supplier_id = %(supplier_id)s)
             order by pr.period desc, pr.rank
            """,
            {"period": period, "supplier_id": supplier_id},
        )
        rows = [_row(r, money=("quality_score", "on_time_rate", "rework_rate", "composite"))
                for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


@router.get("/performance/live")
async def performance_live(period: Optional[str] = None,
                           p: Principal = Depends(require("performance:read"))):
    """实时绩效：直接从任务与审核记录现算，不依赖快照表是否跑过。
    快照表用于出报表和排名趋势；这个接口用于供应商当面质问「凭什么给我 B 级」。
    """
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            with scope as (
              select t.* from forge.tasks t
               where (%(period)s::text is null
                      or to_char(coalesce(t.updated_at, t.created_at), 'YYYY-MM') = %(period)s)
            ), agg as (
              select supplier_id,
                     count(*)                                                        as tasks,
                     count(*) filter (where status = 'accepted')                      as accepted,
                     count(*) filter (where status = 'rejected')                      as rejected,
                     count(*) filter (where status = 'rework')                        as rework_tasks,
                     coalesce(sum(done_count), 0)                                     as items_done,
                     coalesce(sum(rework_round), 0)                                   as rework_rounds,
                     count(*) filter (where status not in ('accepted','rejected')
                                        and due_at is not null and due_at < now())    as overdue,
                     count(*) filter (where due_at is not null)                        as dated
                from scope
               where supplier_id is not null
               group by supplier_id
            )
            select a.supplier_id, s.name as supplier_name, s.tier,
                   a.tasks, a.accepted, a.rejected, a.rework_tasks,
                   a.items_done, a.rework_rounds, a.overdue, a.dated,
                   round(a.accepted::numeric / nullif(a.tasks, 0) * 100, 1)          as accept_rate,
                   round((a.dated - a.overdue)::numeric / nullif(a.dated, 0) * 100, 1) as on_time_rate,
                   round(a.rework_rounds::numeric / nullif(a.tasks, 0), 2)           as avg_rework_round,
                   round((coalesce(s.quality_score, 0) * 0.5
                        + coalesce((a.dated - a.overdue)::numeric / nullif(a.dated, 0) * 100, 0) * 0.3
                        + least(100, a.accepted::numeric / nullif(a.tasks, 0) * 100) * 0.2)::numeric, 1)
                                                                                     as composite
              from agg a
              join forge.suppliers s on s.id = a.supplier_id
             order by composite desc nulls last
            """,
            {"period": period},
        )
        rows = [_row(r, money=("accept_rate", "on_time_rate", "avg_rework_round",
                               "composite")) for r in await cur.fetchall()]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"rows": rows, "total": len(rows), "period": period or "all"}


# ---------------------------------------------------------------------------
# 资产入湖
# ---------------------------------------------------------------------------
class AssetImport(BaseModel):
    project_id: str = Field(min_length=1)
    batch_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    modality: str = Field(default="image",
                          pattern="^(image|video|audio|text|multimodal)$")
    size_mb: int = Field(default=0, ge=0)
    item_count: int = Field(default=0, ge=0)
    storage_key: Optional[str] = None


@router.get("/assets")
async def list_assets(project_id: Optional[str] = None,
                      status: Optional[str] = None,
                      p: Principal = Depends(require("asset:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select id, project_id, batch_no, name, modality, status, size_mb,
                   item_count, storage_key, qc_score, imported_at
              from forge.assets
             where (%(project_id)s::text is null or project_id = %(project_id)s)
               and (%(status)s::text is null or status = %(status)s)
             order by imported_at desc
             limit 300
            """,
            {"project_id": project_id, "status": status},
        )
        rows = [_row(r, money=("qc_score",)) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


@router.post("/assets/import", status_code=201)
async def import_asset(body: AssetImport, request: Request,
                       p: Principal = Depends(require("asset:import"))):
    """登记一次入湖批次。同一项目 + 批次 + 名称视为同一条，重复登记返回既有 id
    （前端重试、网络抖动都不会造出两条并行的资产记录）。"""
    aid = f"ast_{uuid.uuid4().hex[:10]}"
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute("select 1 from forge.projects where id = %s",
                                 (body.project_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, "not found")

        cur = await conn.execute(
            """
            insert into forge.assets
                   (id, enterprise_id, project_id, batch_no, name, modality, status,
                    size_mb, item_count, storage_key)
            values (%s, %s, %s, %s, %s, %s, 'raw', %s, %s, %s)
            on conflict (project_id, batch_no, name) do nothing
            returning id
            """,
            (aid, p.tenant_id, body.project_id, body.batch_no, body.name, body.modality,
             body.size_mb, body.item_count, body.storage_key),
        )
        inserted = await cur.fetchone()
        if inserted is None:
            cur = await conn.execute(
                "select id from forge.assets where project_id = %s and batch_no = %s and name = %s",
                (body.project_id, body.batch_no, body.name))
            existing = await cur.fetchone()
            return {"id": existing["id"], "duplicate": True, "status": "raw"}

        await db.write_audit(conn, p, "asset.import", aid,
                             f"{body.batch_no}/{body.name} {body.item_count} items "
                             f"{body.size_mb}MB", client_ip(request))
    return {"id": aid, "duplicate": False, "status": "raw"}


class AssetStatus(BaseModel):
    status: str = Field(pattern="^(raw|in_annotation|in_review|accepted|archived)$")
    qc_score: Optional[Decimal] = Field(default=None, ge=0, le=100)


@router.post("/assets/{aid}/status")
async def set_asset_status(aid: str, body: AssetStatus, request: Request,
                           p: Principal = Depends(require("asset:import"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute("select id, status from forge.assets where id = %s", (aid,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        await conn.execute(
            """
            update forge.assets
               set status = %s, qc_score = coalesce(%s, qc_score)
             where id = %s
            """,
            (body.status, body.qc_score, aid))
        await db.write_audit(conn, p, f"asset.{body.status}", aid,
                             f"{row['status']} -> {body.status}", client_ip(request))
    return {"ok": True, "status": body.status, "previous_status": row["status"]}
