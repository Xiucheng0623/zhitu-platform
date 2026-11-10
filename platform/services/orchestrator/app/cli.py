"""
FORGE Orchestrator · 运维命令行
============================================================================
只有部署/运维期才用得到的命令，全部**幂等**，可以重复执行。

为什么要有这个模块（而不是让 install.sh 直接 psql 插用户）：
  用户密码是 scrypt 哈希（app/auth.py 的格式 `scrypt$n$r$p$salt$dk`），
  让 SQL 或 shell 去算哈希等于把口令明文写进部署脚本与 shell 历史。
  这里复用 auth.hash_password / normalize_phone，保证与登录接口同源，
  改算法时不会出现「登录永远失败，因为脚本用的是旧格式」。

用法（容器内）：
    python -m app.cli create-admin --phone 13800138001 --password 'Str0ng!Pass'
    python -m app.cli create-admin --phone 13800138001 --password 'x' --force
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid

import psycopg

from .auth import hash_password, normalize_phone
from .config import settings

DEFAULT_ENTERPRISE = "ent_forge"
ADMIN_ROLES = ["platform_admin", "ops_director"]


def _dsn() -> str:
    if not settings.db_dsn:
        raise SystemExit("FORGE_DB_DSN 未配置")
    return settings.db_dsn


def cmd_create_admin(args: argparse.Namespace) -> int:
    try:
        phone = normalize_phone(args.phone)
    except ValueError as exc:
        raise SystemExit(f"手机号非法：{exc}")
    if len(args.password) < 8:
        raise SystemExit("密码至少 8 位")
    if not re.fullmatch(r"1[3-9]\d{9}", phone.lstrip("+").removeprefix("86")):
        raise SystemExit("仅支持中国大陆手机号")

    pw_hash = hash_password(args.password)
    with psycopg.connect(_dsn()) as conn:
        cur = conn.cursor()
        cur.execute("select id, status from forge.users where phone = %s", (phone,))
        row = cur.fetchone()
        if row:
            if not args.force:
                print(f"已存在：{phone}（id={row[0]}，status={row[1]}）。"
                      f"要重置密码请加 --force。")
                return 0
            cur.execute(
                "update forge.users set password_hash=%s, status='active', "
                "  must_change_pwd=true, token_version=token_version+1, "
                "  failed_attempts=0, locked_until=null, updated_at=now() "
                " where phone=%s",
                (pw_hash, phone))
            conn.commit()
            print(f"已重置密码并激活：{phone}（id={row[0]}）")
            return 0

        uid = "usr_" + uuid.uuid4().hex[:16]
        # must_change_pwd=false：bootstrap 账号的密码是部署者自己设（或自己收到
        # install.sh 打印的值），不存在「别人替我设了密码」的信任问题。
        # 强制首登改密只会把一键部署后的第一次登录堵在改密页上。
        # 相对地，--force 重置（见上）保持 must_change_pwd=true —— 那是管理员
        # 替人重置，旧密码已不可信，必须轮换。
        cur.execute(
            """insert into forge.users
                 (id, enterprise_id, phone, password_hash, name, roles,
                  status, must_change_pwd)
               values (%s,%s,%s,%s,%s,%s,'active',false)""",
            (uid, args.enterprise, phone, pw_hash, args.name or "管理员", ADMIN_ROLES))
        conn.commit()
        print(f"已创建管理员：{phone}（id={uid}）roles={ADMIN_ROLES}")
        return 0


def cmd_check_db(args: argparse.Namespace) -> int:
    with psycopg.connect(_dsn()) as conn:
        cur = conn.cursor()
        cur.execute("select current_database(), version()")
        db, ver = cur.fetchone()
        print(f"数据库 {db} 连接正常：{ver.split(' on ')[0]}")
        # 业务表全部开着 RLS，不设租户上下文会读成 0 行（看起来像没数据）。
        cur.execute("select set_config('forge.enterprise_id', %s, true)",
                    (args.enterprise,))
        for t in ("enterprises", "users", "projects", "tasks"):
            try:
                cur.execute(f"select count(*) from forge.{t}")
                print(f"  forge.{t}: {cur.fetchone()[0]} 行")
            except psycopg.errors.UndefinedTable:
                print(f"  forge.{t}: 未建表（迁移未跑完？）")
        cur.execute("select id, name, short_name from forge.enterprises")
        for r in cur.fetchall():
            print(f"  企业：{r[0]} {r[1]} ({r[2]})")
        cur.execute("select phone, name, roles, status from forge.users order by phone")
        for r in cur.fetchall():
            print(f"  账号：{r[0]} {r[1]} {r[2]} {r[3]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.cli", description="FORGE 平台运维命令")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="创建/重置管理员账号（幂等）")
    p.add_argument("--phone", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--name", default="管理员")
    p.add_argument("--enterprise", default=DEFAULT_ENTERPRISE)
    p.add_argument("--force", action="store_true", help="账号已存在时重置密码并激活")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("check-db", help="检查数据库连接与基础数据")
    p.add_argument("--enterprise", default=DEFAULT_ENTERPRISE,
                   help="RLS 租户上下文（业务表按它过滤）")
    p.set_defaults(func=cmd_check_db)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
