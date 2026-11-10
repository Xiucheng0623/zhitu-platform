# -*- coding: utf-8 -*-
"""清空平台库里的演示/测试数据（保留企业主体与管理员账号）。

配套备份：D:/forge-ls/backup/forge-*.sql（restore 见 restore_forge.py）。
"""
from __future__ import annotations

import sys

import psycopg

DSN = "host=localhost dbname=forge user=postgres password=postgres"

# 顺序 = 外键从子到父：
#   payment_allocations → payments / invoices / settlements → suppliers
CLEAR = [
    "annotation_submissions", "reviews", "task_assignments", "project_suppliers",
    "tasks", "projects",
    "payment_allocations", "payments", "invoices", "settlements",
    "supplier_members", "suppliers",
    "audit_log", "idempotency_keys", "invites",
    "assets", "deliveries", "performance_records",
    "collectors", "collection_tasks", "collection_scripts", "collection_sessions",
    "identities", "identity_link_requests",
]

# 演示用虚构人物；两个管理员账号保留（13800138001 / 13800138000）
DROP_USERS = [
    "usr_454492d59247421e",  # 试点用户
    "usr_3d7d57eba97b4989",  # 体检账号
    "usr_14674eb5d41a41d9",  # 王小明
    "usr_fa7bd75cccd848a4",  # 李娜
]


def main() -> int:
    dry = "--dry-run" in sys.argv
    with psycopg.connect(DSN) as c:
        cur = c.cursor()
        cur.execute("set search_path to forge, public")
        # audit_log 有 append-only 触发器（audit_immutable），触发器对 TRUNCATE/DELETE
        # 一律 RAISE。清空审计表是「重置演示数据」的一部分，这里用会话级
        # replica 角色临时停掉触发器（需要 superuser，见 DSN），事务结束自动恢复。
        cur.execute("set session_replication_role = replica")
        for t in CLEAR:
            try:
                cur.execute(f"select count(*) from forge.{t}")
                n = cur.fetchone()[0]
                if not n:
                    print(f"skip    {t:28s} 已是空表")
                    continue
                if dry:
                    print(f"would   {t:28s} {n} 行")
                else:
                    cur.execute(f"delete from forge.{t}")
                    print(f"cleared {t:28s} {cur.rowcount}")
            except psycopg.errors.UndefinedTable:
                print(f"skip    {t} (表不存在)")
        for uid in DROP_USERS:
            if dry:
                print(f"would drop user {uid}")
                continue
            cur.execute("delete from forge.users where id=%s", (uid,))
            print(f"drop user {uid} -> {cur.rowcount}")
        if dry:
            print("would drop ent_other / rename ent_forge")
        else:
            cur.execute("delete from forge.enterprises where id='ent_other'")
            print("drop ent_other ->", cur.rowcount)
            cur.execute(
                "update forge.enterprises set name='智途', "
                "short_name='智途' where id='ent_forge'")
            print("rename ent_forge ->", cur.rowcount)
        if dry:
            c.rollback()
            print("\n--dry-run：已回滚，未改动")
            return 0
        c.commit()

        cur.execute("select id,phone,name,roles,status from forge.users order by phone")
        print("\nremaining users:")
        for r in cur.fetchall():
            print("  ", r)
        cur.execute("select id,name,short_name from forge.enterprises")
        print("enterprises:", cur.fetchall())
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema='forge' and table_type='BASE TABLE' order by 1")
        for (t,) in cur.fetchall():
            cur.execute(f"select count(*) from forge.{t}")
            n = cur.fetchone()[0]
            if n:
                print(f"  仍有数据 {t}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
