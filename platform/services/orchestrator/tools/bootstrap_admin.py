"""
FORGE · 初始管理员引导
================================================================
为什么必须有这个脚本，而不是"上线后手工 insert 一行"：

1. 密码哈希的格式只有 app/auth.py 知道（scrypt$n$r$p$salt$hash）。
   手工拼 SQL 很容易拼出格式不对的串，症状是"登录永远提示密码错误"，
   而错误信息里又不能打印哈希 —— 排查成本极高。
2. 第一个账号没有任何人能邀请它（鸡生蛋）。所以必须有一个绕过
   邀请码的入口，而这个入口只能是命令行（有服务器权限 = 有资格）。
3. 幂等：重复执行不会建出第二个管理员，只会把密码重置为给定值。
   部署脚本可以放心地每次都跑一遍。

用法
----
  # 建（或重置）一个平台管理员，密码自动生成并打印一次
  python -m tools.bootstrap_admin --phone 13800138000 --name 张伟

  # 指定密码 + 多个角色
  python -m tools.bootstrap_admin --phone 13800138000 --name 张伟 \
      --password 'MyLongPass2026' --roles platform_admin,ops_director

  # 只查有没有管理员，不做修改
  python -m tools.bootstrap_admin --check

注意
----
  · 走的是 forge_app 账号（不是 postgres 超级用户）—— 与线上路径一致，
    避免"用超级用户跑通了、换应用账号就炸"。
  · forge.users 上没有 RLS（登录时没有租户上下文），所以这里不注入
    forge.ctx()。这是设计使然，不是漏了。
  · 生成的密码只在标准输出出现一次，库里只有哈希。丢了就重置。
"""
from __future__ import annotations

import argparse
import secrets
import sys
import uuid

from app import db
from app.auth import hash_password, mask_phone, normalize_phone, password_problem
from app.config import settings
from app.looputil import run_async
from app.security import ROLE_MATRIX

DEFAULT_ROLES = ["platform_admin"]


async def _load_enterprise(conn, wanted: str) -> str:
    """确认租户存在。

    forge.enterprises 是 **force RLS**，策略是 id = forge.ctx('forge.enterprise_id')。
    没有租户上下文时它一行都看不见 —— 所以"查不到"既可能是 id 写错，
    也可能只是没注入上下文。这里先注入目标租户再查，把两种原因分开。
    """
    await conn.execute(
        "select set_config('forge.enterprise_id', %s, true)", (wanted,))
    cur = await conn.execute("select id, name from forge.enterprises where id = %s", (wanted,))
    row = await cur.fetchone()
    if row:
        print(f"[info] 租户：{row['id']}（{row['name']}）")
        return row["id"]
    sys.exit(
        f"租户 {wanted!r} 不存在，或当前账号无权访问。\n"
        f"  · 确认 id 拼写：psql -U postgres -d forge -c 'select id,name from forge.enterprises'\n"
        f"  · 或显式指定：--enterprise <id>"
    )


async def main_async(args: argparse.Namespace) -> int:
    err = settings.validate()
    if err:
        # 引导脚本只在 native 模式有意义：dev 模式没有真实账号体系。
        print(f"[fail] 配置校验失败：{err}")
        return 2
    if settings.auth_mode != "native":
        print(f"[fail] 当前 AUTH_MODE={settings.auth_mode}，引导脚本只用于 native 模式")
        return 2

    # --check 不需要手机号，先处理掉再校验入参
    if args.check:
        await db.open_pool()
        try:
            async with db.pool().connection() as conn:
                cur = await conn.execute(
                    """select id, phone, name, roles, status
                         from forge.users
                        where 'platform_admin' = any(roles) and status = 'active'
                        order by created_at""")
                rows = await cur.fetchall()
            if not rows:
                print("[warn] 没有任何处于 active 状态的平台管理员")
                return 1
            print(f"活跃平台管理员 {len(rows)} 个：")
            for r in rows:
                print(f"  {r['id']}  {mask_phone(r['phone'])}  {r['name'] or '-'}  {r['roles']}")
            return 0
        finally:
            await db.close_pool()

    try:
        phone = normalize_phone(args.phone)
    except ValueError as exc:
        print(f"[fail] {exc}")
        return 2

    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    unknown = [r for r in roles if r not in ROLE_MATRIX]
    if unknown:
        print(f"[fail] 未知角色：{unknown}")
        print(f"       可用：{', '.join(sorted(ROLE_MATRIX))}")
        return 2

    await db.open_pool()
    try:
        async with db.pool().connection() as conn:
            enterprise = await _load_enterprise(conn, args.enterprise)

            cur = await conn.execute(
                "select id, status from forge.users where phone = %s", (phone,))
            existing = await cur.fetchone()

            if existing and not args.reset:
                print(f"[warn] {mask_phone(phone)} 已存在（{existing['id']}, {existing['status']}），"
                      f"未做修改。要重置密码请加 --reset")
                return 1

            password = args.password or ("Fg" + secrets.token_urlsafe(12))
            bad = password_problem(password)
            if bad:
                print(f"[fail] 密码不合规：{bad}")
                return 2

            # 默认强制首登改密。--no-force-change 关掉它，用于「运维共用账号」
            # 这类密码本来就由管理员直管、不存在"临时密码"语义的场景。
            # 关掉后这个密码就是长期密码，请自行保证其强度。
            force = not args.no_force_change

            if existing:
                uid = existing["id"]
                await conn.execute(
                    """update forge.users
                          set password_hash = %s, roles = %s, status = 'active',
                              must_change_pwd = %s, token_version = token_version + 1,
                              failed_attempts = 0, locked_until = null, updated_at = now()
                        where id = %s""",
                    (hash_password(password), roles, force, uid))
                action = "重置"
            else:
                uid = "usr_" + uuid.uuid4().hex[:16]
                await conn.execute(
                    """insert into forge.users
                         (id, enterprise_id, phone, password_hash, name, roles,
                          status, must_change_pwd)
                       values (%s,%s,%s,%s,%s,%s,'active',%s)""",
                    (uid, enterprise, phone, hash_password(password),
                     args.name or mask_phone(phone), roles, force))
                action = "创建"
            await conn.commit()

        print(f"[ ok ] 已{action}账号")
        if "platform_admin" not in roles:
            print("       [note] 该账号没有 platform_admin 角色，仅能用于普通登录场景")
        print(f"       用户 id   : {uid}")
        print(f"       手机号    : {mask_phone(phone)}")
        print(f"       租户      : {enterprise}")
        print(f"       角色      : {', '.join(roles)}")
        if not args.password:
            print(f"       初始密码  : {password}")
            print("       ↑ 只显示这一次，库里只有哈希。")
        else:
            print("       初始密码  : 使用命令行指定的值")
        if force:
            print("       首登改密  : 需要（登录后会被钉在改密页）")
        else:
            print("       首登改密  : 不需要（该密码即长期密码）")
        return 0
    finally:
        await db.close_pool()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="bootstrap_admin", description="创建/重置 FORGE 平台管理员")
    ap.add_argument("--phone", help="11 位手机号，或 +86 开头")
    ap.add_argument("--name", default="", help="显示名，缺省用手机号掩码")
    ap.add_argument("--password", default="", help="不填则随机生成并打印一次")
    ap.add_argument("--roles", default=",".join(DEFAULT_ROLES),
                    help=f"逗号分隔，默认 {','.join(DEFAULT_ROLES)}")
    ap.add_argument("--enterprise", default=settings.default_tenant,
                    help=f"租户 id，默认 {settings.default_tenant}")
    ap.add_argument("--reset", action="store_true",
                    help="手机号已存在时重置密码并激活（不改密码会拒绝）")
    ap.add_argument("--no-force-change", action="store_true",
                    help="不要求首次登录改密（运维共用账号等场景）")
    ap.add_argument("--check", action="store_true", help="只查有没有活跃管理员")
    args = ap.parse_args()

    if not args.check and not args.phone:
        ap.error("--phone 必填（--check 除外）")
    return run_async(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
