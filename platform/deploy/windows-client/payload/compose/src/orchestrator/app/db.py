"""
FORGE Orchestrator · 数据库访问层
================================================================
两条铁律：
  1. 每个事务开头必须注入 RLS 上下文（enterprise_id / supplier_id / assignee_id），
     且第三参数为 true（事务级），连接复用时不会串味。
  2. 业务代码不允许自己拼 enterprise_id 条件 —— 隔离由 PG 策略强制，
     应用层过滤只是「减少无谓 IO」，不是安全边界。

应用连接使用 forge_app（无 create / 无 bypassrls），
即使代码写错也不会跨租户读写；这是比任何应用层判断都硬的兜底。
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings
from .security import Principal

log = logging.getLogger("forge.db")

_pool: Optional[AsyncConnectionPool] = None


async def open_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = AsyncConnectionPool(
        conninfo=settings.db_dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        open=False,
        kwargs={
            "row_factory": dict_row,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
            "application_name": "forge-orchestrator",
        },
        name="forge",
    )
    await _pool.open(wait=True, timeout=15)
    log.info("db pool ready (min=%s max=%s)", settings.db_pool_min, settings.db_pool_max)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db pool closed")


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("db pool 未初始化（应在 lifespan 中调用 open_pool）")
    return _pool


@asynccontextmanager
async def tenant_conn(p: Principal) -> AsyncIterator[Any]:
    """开启一个事务并注入 RLS 上下文；异常自动回滚，正常退出提交。"""
    ctx = p.as_rls()
    async with pool().connection() as conn:
        async with conn.transaction():
            for key, val in ctx.items():
                await conn.execute("select set_config(%s, %s, true)", (key, val))
            yield conn


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
async def db_health() -> Dict[str, Any]:
    """连通性 + RLS 强制状态。

    注意：这里刻意不报告业务行数。health 查询没有租户上下文，
    而所有业务表都是 force row level security，任何计数都会是 0 ——
    把 0 当成「库是空的」是典型的误判来源。要看真实数据请用带 token 的接口。
    """
    try:
        async with pool().connection() as conn:
            cur = await conn.execute(
                """
                select current_database() as db,
                       current_setting('server_version') as pg,
                       (select count(*) from pg_class c
                          join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname = 'forge' and c.relkind = 'r') as tables,
                       (select count(*) from pg_class c
                          join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname = 'forge' and c.relkind = 'r'
                           and c.relrowsecurity and c.relforcerowsecurity) as rls_forced,
                       (select rolsuper or rolbypassrls from pg_roles
                         where rolname = current_user) as can_bypass_rls,
                       -- 未强制 RLS 的表名清单。空数组才是理想值；
                       -- 010 迁移里 users / invites 是**刻意**豁免的（登录时
                       -- 还没有租户上下文，无法求值 RLS 表达式），
                       -- 但"刻意豁免"和"忘了加"从计数上看不出区别，
                       -- 所以这里把名字列出来，让豁免成为显式事实。
                       (select coalesce(array_agg(c.relname order by c.relname), '{}')
                          from pg_class c
                          join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname = 'forge' and c.relkind = 'r'
                           and not (c.relrowsecurity and c.relforcerowsecurity)) as rls_exempt
                """
            )
            row = await cur.fetchone()
            stats = pool().get_stats()
            return {
                "ok": True,
                **row,
                "pool": {"size": stats.get("pool_size"), "available": stats.get("pool_available"),
                         "waiting": stats.get("requests_waiting")},
            }
    except Exception as exc:  # noqa: BLE001 - 健康检查要吞掉一切异常并如实上报
        log.warning("db health failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------
async def idem_lookup(conn: Any, p: Principal, key: str) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        """
        select status_code, response
          from forge.idempotency_keys
         where key = %s and enterprise_id = %s
           and created_at > now() - make_interval(hours => %s)
        """,
        (key, p.tenant_id, settings.idempotency_ttl_hours),
    )
    return await cur.fetchone()


async def idem_store(conn: Any, p: Principal, key: str, method: str, path: str,
                     status_code: int, response: Dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb
    await conn.execute(
        """
        insert into forge.idempotency_keys
               (key, enterprise_id, actor_id, method, path, status_code, response)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (key) do nothing
        """,
        (key, p.tenant_id, p.sub, method, path, status_code, Jsonb(response)),
    )


async def idem_purge(conn: Any) -> int:
    cur = await conn.execute(
        "delete from forge.idempotency_keys where created_at < now() - make_interval(hours => %s)",
        (settings.idempotency_ttl_hours * 4,),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
async def write_audit(conn: Any, p: Principal, action: str, target: str,
                      detail: str = "", ip: Optional[str] = None) -> None:
    """审计写入与业务写入同事务：业务成功则审计必在，反之一起回滚。

    audit_log.detail 是 text，而调用方很容易顺手传个 dict 进来 ——
    psycopg 不会帮你转，会直接抛 "cannot adapt type 'dict'"，把一次
    正常的业务操作变成 500。在这里统一吸收掉，比让每个调用点自己记得
    json.dumps 更可靠（已经在 create_invite 上踩过一次）。
    """
    if detail and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    await conn.execute(
        """
        insert into forge.audit_log (enterprise_id, actor_id, actor_name, action, target, detail, ip)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (p.tenant_id, p.sub, p.name or p.sub, action, target, detail, ip),
    )


async def list_audit(conn: Any, limit: int = 100, offset: int = 0,
                     action: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = """
        select id, actor_id, actor_name, action, target, detail, at
          from forge.audit_log
         where (%(action)s::text is null or action = %(action)s)
         order by at desc, id desc
         limit %(limit)s offset %(offset)s
    """
    cur = await conn.execute(sql, {"action": action, "limit": limit, "offset": offset})
    rows = await cur.fetchall()
    for r in rows:
        r["id"] = str(r["id"])
        r["at"] = r["at"].isoformat() if r.get("at") else None
    return rows
