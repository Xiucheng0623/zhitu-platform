"""
FORGE Orchestrator · 身份与认证（手机号登录）
================================================================
这一版替换掉 auth_mode=dev 的假身份。三条硬约束决定了实现形状：

1. **登录名是手机号，不是邮箱。**
   手机号统一规范化成 E.164（+8613800138000）后入库，唯一索引建在它上面。
   规范化放在服务端而不是前端 —— 前端传什么形态都可能（13800138000 /
   138 0013 8000 / +86 138... / 8613800138000），只在入口归一化一次，
   后面所有查询都拿同一个形态比。

2. **Label Studio 不认手机号。**
   LS 的 USERNAME_FIELD='email'，且 CE 版没有 OIDC/SAML。所以每个员工在 LS
   里仍需要一个账号，email 用合成地址（13800138000@staff.forge.local），
   由平台在开通流程里建好、凭证由平台代持。员工全程只看见手机号。
   见 forge.users.ls_email / ls_user_id。

3. **密码哈希不能出现在应用可读列里。**
   010_identity.sql 用列级授权把 password_hash 从 forge_app 的 SELECT
   列表里摘掉了，校验只能走 forge.login_lookup()。所以本模块**不自己查
   password_hash**，一律经函数取。

撤销语义：
   users.token_version 每次改密/停用 +1，JWT 里带 ver。
   校验时不匹配即失效 —— 不用维护 refresh token 表就能让"改完密码，
   其它设备上的会话立刻过期"。

越权语义沿用 security.py：未认证 401，已认证无权限 404。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from . import db
from . import sso as sso_module
from .config import settings
from .netutil import client_ip
from .security import ROLE_MATRIX, Principal, current_principal, require

log = logging.getLogger("forge.auth")
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ===========================================================================
# 1. 手机号规范化
# ===========================================================================
_CN_MOBILE = re.compile(r"1[3-9]\d{9}")


def normalize_phone(raw: str) -> str:
    """把各种写法统一成 E.164。非法则抛 ValueError（由路由转成 400）。

    只支持中国大陆号段（1[3-9] 开头 11 位）。这不是"偷懒"：境内外号码规则
    完全不同，混在一个字段里会让唯一索引和验证码路由都变得含糊。
    将来要支持境外员工，应该加 country_code 列而不是放宽正则。
    """
    if not raw:
        raise ValueError("手机号不能为空")
    # 去掉空格、横线、括号；保留开头的 +
    s = re.sub(r"[^\d+]", "", raw.strip())
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("86") and len(s) == 13:
        s = s[2:]
    if not _CN_MOBILE.fullmatch(s):
        raise ValueError("请输入 11 位中国大陆手机号")
    return "+86" + s


def mask_phone(phone: str) -> str:
    """日志与前端展示用：+8613800138000 → 138****8000。"""
    d = phone.lstrip("+")
    if d.startswith("86"):
        d = d[2:]
    if len(d) != 11:
        return phone
    return f"{d[:3]}****{d[-4:]}"


# ===========================================================================
# 2. 密码哈希（scrypt，标准库）
# ===========================================================================
# 为什么用 scrypt 而不是 bcrypt/argon2：
#   两者都要额外依赖（兼容性问题在 Windows + Python 3.13 上尤其多），
#   而 hashlib.scrypt 是标准库自带的 memory-hard KDF，参数可控、无编译依赖。
#   N=2^14, r=8 → 单次约 16 MB 内存、几十毫秒，足以让离线爆破变贵。
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
                        maxmem=SCRYPT_MAXMEM)
    b64 = lambda b: base64.b64encode(b).decode("ascii")   # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """常量时间比较。格式不符一律返回 False，不抛异常（避免把解析细节
    变成可观测的旁路信号）。

    stored 为 None 是合法输入：011 迁移起，纯 SSO 账号的 password_hash
    就是 NULL（没有本地密码）。这类账号必须**永远**密码登录失败，
    而不是因为 None.split 抛 AttributeError 变成 500 —— 500 会让攻击者
    知道"这个手机号存在且是 SSO 账号"，那本身就是信息泄露。
    """
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n), r=int(r), p=int(p), dklen=len(expected),
                                maxmem=SCRYPT_MAXMEM)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# 用于"用户不存在"时也跑一次哈希，让响应时间与"密码错误"接近，
# 避免通过耗时差异枚举手机号是否注册过。
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


# ---------------------------------------------------------------------------
# 2.1 把 scrypt 挪出事件循环（稳定性关键，别改回同步调用）
# ---------------------------------------------------------------------------
# hashlib.scrypt 是**同步 CPU 密集**调用：N=2^14 单次约 0.1~0.9 秒（视 CPU 而定）。
# 直接在 async 路由里调用，等于把整个事件循环按住 —— 一个人登录，全公司
# 所有请求一起卡住。实测：8 个并发登录时，一个无关的 /healthz 被卡了 6.6 秒。
#
# 放进线程池后：
#   * 事件循环不再被阻塞，别人的请求照常响应；
#   * 多个登录可以真正并行（Python 3.13 的 scrypt 实现会释放 GIL）。
# 代价是要注意别在持有数据库连接时做这件事（见 change_password 的重构）。
async def verify_password_async(password: str, stored: Optional[str]) -> bool:
    return await asyncio.to_thread(verify_password, password, stored)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


def password_problem(password: str) -> Optional[str]:
    """返回不合规原因，None 表示通过。

    刻意不要求特殊字符：内部系统里强制特殊符号的实际后果是
    "Spring2026!" 这类可预测密码 + 便利贴。长度和不可预测性更重要。
    """
    if len(password) < settings.password_min_length:
        return f"密码至少 {settings.password_min_length} 位"
    if password.isdigit():
        return "密码不能是纯数字"
    if len(set(password)) < 4:
        return "密码太简单（重复字符过多）"
    return None


# ===========================================================================
# 3. JWT 签发/校验
# ===========================================================================
def issue_token(user_id: str, tenant_id: str, token_version: int,
                must_change_pwd: bool = False) -> Tuple[str, int]:
    """返回 (token, 过期时间戳)。claims 里只放**稳定**的标识，
    不放 roles —— roles 每次请求从库里读，改角色立即生效。

    mp（must change password）放进 claim 是为了让"未改初始密码"这个状态
    能被中间件零 IO 识别，不必为了判断它而多查一次库。
    """
    now = int(time.time())
    exp = now + settings.jwt_ttl_minutes * 60
    claims = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "ver": token_version,
        "mp": bool(must_change_pwd),
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": exp,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    return token, exp


class NativeTokenError(Exception):
    def __init__(self, reason: str, status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def decode_native_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"],
            audience=settings.jwt_audience, issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "ver"]},
        )
    except jwt.ExpiredSignatureError:
        raise NativeTokenError("登录已过期，请重新登录")
    except jwt.InvalidTokenError as exc:
        raise NativeTokenError(f"凭证无效（{type(exc).__name__}）")


async def load_principal(claims: Dict[str, Any]) -> Principal:
    """按 JWT 的 sub 从库里取当前状态，构造 Principal。

    每次请求都查，是为了让"改角色 / 停用账号"立即生效 ——
    如果 roles 写死在 token 里，权限变更要等到 token 过期才起作用，
    这在有人离职的当天是不可接受的。
    """
    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise NativeTokenError("凭证缺少主体标识")

    async with db.pool().connection() as conn:
        cur = await conn.execute(
            """select id, enterprise_id, name, email, roles, status,
                      supplier_id, token_version, must_change_pwd
                 from forge.users where id = %s""",
            (user_id,),
        )
        row = await cur.fetchone()

    if not row:
        raise NativeTokenError("账号不存在")
    if row["status"] != "active":
        raise NativeTokenError("账号未激活或已停用", status=403)
    if int(claims.get("ver", -1)) != int(row["token_version"]):
        # 改过密码 / 被停用过：旧 token 一律作废
        raise NativeTokenError("登录已失效，请重新登录")

    clean_roles = [r for r in (row["roles"] or []) if r in ROLE_MATRIX]
    return Principal(
        sub=row["id"],
        tenant_id=row["enterprise_id"],
        roles=clean_roles,
        name=row["name"] or "",
        email=row["email"] or "",
        supplier_id=row["supplier_id"],
        is_service=False,
        # 以库为准而不是以 token 里的 mp 为准：
        # token 是登录那一刻的快照，库是实时状态，且这里本来就查了库，多读一列零成本。
        must_change_pwd=bool(row["must_change_pwd"]),
    )


# ===========================================================================
# 4. 数据库操作
# ===========================================================================
# 注意：db.py 的连接池用的是 psycopg.rows.dict_row，所有游标返回 dict，
# 不是元组。这里统一用列名访问，别再写下标 —— 下标在 dict 上会静默取到
# 第 N 个**键名**而不是值，症状是"KeyError: 0"或者更难查的脏数据。

async def _login_lookup(phone: str) -> Optional[Dict[str, Any]]:
    """按手机号取登录所需的一行。

    走 forge.login_lookup() 这个 SECURITY DEFINER 函数，因为
    010_identity.sql 用列级授权把 password_hash 从 forge_app 的 SELECT
    列表里摘掉了 —— 直接 select password_hash 会被 PG 拒绝。
    函数内部以定义者身份读表，只回一行，不给列表接口留口子。
    """
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select * from forge.login_lookup(%s)", (phone,))
        return await cur.fetchone()


async def _bump_failure(user_id: str) -> None:
    async with db.pool().connection() as conn:
        await conn.execute(
            "select forge.note_login_failure(%s, %s, %s)",
            (user_id, settings.login_max_failures, settings.login_lock_minutes))
        await conn.commit()


async def _note_success(user_id: str) -> None:
    async with db.pool().connection() as conn:
        await conn.execute(
            """update forge.users
                  set failed_attempts = 0, locked_until = null,
                      last_login_at = now(), updated_at = now()
                where id = %s""", (user_id,))
        await conn.commit()


# ===========================================================================
# 5. 请求模型
# ===========================================================================
class LoginIn(BaseModel):
    phone: str = Field(..., max_length=32)
    password: str = Field(..., max_length=200)


class RegisterIn(BaseModel):
    phone: str = Field(..., max_length=32)
    password: str = Field(..., max_length=200)
    name: str = Field(default="", max_length=64)
    invite_code: str = Field(..., max_length=64)

    @field_validator("name")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()


class ChangePasswordIn(BaseModel):
    old_password: str = Field(..., max_length=200)
    new_password: str = Field(..., max_length=200)


class CreateInviteIn(BaseModel):
    roles: List[str] = Field(default_factory=lambda: ["annotator"])
    supplier_id: Optional[str] = None
    note: str = Field(default="", max_length=200)
    max_uses: int = Field(default=1, ge=1, le=200)
    expires_in_days: int = Field(default=7, ge=1, le=90)


class SetStatusIn(BaseModel):
    status: str = Field(..., pattern="^(active|suspended|pending)$")


class ResetPasswordIn(BaseModel):
    new_password: Optional[str] = Field(default=None, max_length=200)


# ===========================================================================
# 6. 路由
# ===========================================================================
@router.post("/register")
async def register(body: RegisterIn, request: Request) -> Dict[str, Any]:
    """凭邀请码自助注册。默认 status=pending，需管理员激活后才能登录。

    为什么要邀请码：内部企业不该对公网开放注册（任意手机号都能进），
    但员工又需要自助开户。邀请码把"可控"和"自助"同时满足。
    """
    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    bad = password_problem(body.password)
    if bad:
        raise HTTPException(400, bad)

    async with db.pool().connection() as conn:
        # 邀请码必须在同一个事务里校验并占用，否则并发注册能超额使用
        cur = await conn.execute(
            """select code, enterprise_id, roles, supplier_id, max_uses,
                      used_count, expires_at
                 from forge.invites where code = %s for update""",
            (body.invite_code.strip(),))
        inv = await cur.fetchone()
        if not inv:
            raise HTTPException(400, "邀请码无效")
        if inv["expires_at"] is not None and inv["expires_at"] < _now_ts():
            raise HTTPException(400, "邀请码已过期")
        if inv["used_count"] >= inv["max_uses"]:
            raise HTTPException(400, "邀请码已用尽")

        cur = await conn.execute(
            "select 1 from forge.users where phone = %s", (phone,))
        if await cur.fetchone():
            raise HTTPException(409, "该手机号已注册")

        clean_roles = [r for r in (inv["roles"] or []) if r in ROLE_MATRIX] or ["annotator"]
        uid = "usr_" + uuid.uuid4().hex[:16]
        await conn.execute(
            """insert into forge.users
                 (id, enterprise_id, phone, password_hash, name, roles,
                  status, supplier_id, must_change_pwd)
               values (%s,%s,%s,%s,%s,%s,'pending',%s,true)""",
            (uid, inv["enterprise_id"], phone, await hash_password_async(body.password),
             body.name or mask_phone(phone), clean_roles, inv["supplier_id"]))
        await conn.execute(
            "update forge.invites set used_count = used_count + 1 where code = %s",
            (inv["code"],))
        await conn.commit()

    log.info("注册待激活 user=%s phone=%s invite=%s", uid, mask_phone(phone), inv["code"])
    return {
        "ok": True,
        "user_id": uid,
        "status": "pending",
        "message": "注册已提交，等待管理员激活后即可登录",
    }


@router.post("/login")
async def login(body: LoginIn, request: Request) -> Dict[str, Any]:
    try:
        phone = normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    _t0 = time.perf_counter()
    row = await _login_lookup(phone)
    _t1 = time.perf_counter()

    # 账号不存在：也跑一次 scrypt，避免用响应时间探测手机号是否注册过。
    if not row:
        await verify_password_async(body.password, _DUMMY_HASH)
        raise HTTPException(401, "手机号或密码错误")

    if row["locked_until"] is not None and row["locked_until"] > _now_ts():
        raise HTTPException(423, "尝试次数过多，账号已临时锁定，请稍后再试")

    if not row["password_hash"]:
        # 纯 SSO 账号：没有本地密码。这里不能返回"密码错误"就完事 ——
        # 内部员工看到那句话会一直重试，正确提示是去走统一登录。
        raise HTTPException(403, "该账号使用企业统一登录，请点击「用企业账号登录」")

    if not await verify_password_async(body.password, row["password_hash"]):
        await _bump_failure(row["id"])
        left = max(0, settings.login_max_failures - (row["failed_attempts"] + 1))
        hint = f"，还可尝试 {left} 次" if left > 0 else "，账号即将被临时锁定"
        raise HTTPException(401, "手机号或密码错误" + hint)
    _t2 = time.perf_counter()

    if row["status"] == "pending":
        raise HTTPException(403, "账号尚未激活，请联系管理员")
    if row["status"] == "suspended":
        raise HTTPException(403, "账号已停用，请联系管理员")

    await _note_success(row["id"])
    _t3 = time.perf_counter()
    token, exp = issue_token(row["id"], row["enterprise_id"],
                             int(row["token_version"]),
                             bool(row["must_change_pwd"]))

    await _audit(row, "auth.login", "session",
                 {"phone": mask_phone(phone)}, ip=client_ip(request))
    _t4 = time.perf_counter()
    # 只在明显异常时告警。阈值定 1.5 秒：服务器上正常登录 100~300 ms，
    # 笔记本（大小核 + 平衡电源）上 scrypt 本来就慢到 0.8 s 左右，那不是
    # 故障，阈值压太低会天天报警，最后没人看。真出问题（数据库锁、磁盘、
    # CPU 被限死）通常直接冲过 1.5 s。
    _total_ms = (_t4 - _t0) * 1000
    if _total_ms > 1500:
        log.warning(
            "登录耗时异常 %.0f ms（查库 %.0f / 验密 %.0f / 记账 %.0f / 审计 %.0f）",
            _total_ms, (_t1 - _t0) * 1000, (_t2 - _t1) * 1000,
            (_t3 - _t2) * 1000, (_t4 - _t3) * 1000)
    else:
        log.debug("登录耗时 %.0f ms（验密 %.0f）", _total_ms, (_t2 - _t1) * 1000)
    log.info("登录成功 user=%s phone=%s", row["id"], mask_phone(phone))

    return {
        "ok": True,
        "access_token": token,
        "token_type": "bearer",
        "expires_at": exp,
        "must_change_password": bool(row["must_change_pwd"]),
        "user": {
            "id": row["id"],
            "name": row["name"],
            "phone": mask_phone(row["phone"]),
            "roles": [r for r in (row["roles"] or []) if r in ROLE_MATRIX],
        },
    }


@router.get("/me")
async def auth_me(p: Principal = Depends(current_principal)) -> Dict[str, Any]:
    """当前登录者。比 /api/v1/me 多带一个手机号掩码，供顶栏展示。"""
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select phone, must_change_pwd, last_login_at from forge.users where id = %s",
            (p.sub,))
        row = await cur.fetchone()
    return {
        "id": p.sub,
        "name": p.name,
        "email": p.email,
        "phone": mask_phone(row["phone"]) if row else "",
        "roles": p.roles,
        "tenant_id": p.tenant_id,
        "supplier_id": p.supplier_id,
        "must_change_password": bool(row["must_change_pwd"]) if row else False,
        "last_login_at": row["last_login_at"].isoformat() if row and row["last_login_at"] else None,
    }


@router.post("/change-password")
async def change_password(body: ChangePasswordIn,
                          p: Principal = Depends(current_principal)) -> Dict[str, Any]:
    """改密后 token_version +1，其它设备上的会话立即失效；
    但给当前调用方回一个新 token，免得自己把自己踢掉。

    刻意分三段、中间不留数据库连接：
      1. 取旧哈希（很短）
      2. 校验 + 计算新哈希（scrypt，几百毫秒，全程在线程池、不占连接）
      3. 写回并取新 token_version（很短）
    如果图省事把三段塞进一个 `with connection()`，那几百毫秒里连接是被
    占死的 —— 12 个人同时改密就能把连接池抽干，其他人连查询都排不上。
    """
    bad = password_problem(body.new_password)
    if bad:
        raise HTTPException(400, bad)

    # --- 1. 取旧哈希 -----------------------------------------------------
    # 这里**不能**直接 select password_hash —— 010_identity.sql 用列级授权
    # 把它从 forge_app 的 SELECT 列表里摘掉了，直接读会 InsufficientPrivilege。
    # 绕法：先取手机号（有授权），再走 login_lookup() 拿哈希。
    # 这不是开新口子 —— login_lookup 本来就是登录路径上必有的原语，
    # 它已经能按手机号返回哈希，这里只是复用它。
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select phone, enterprise_id from forge.users where id = %s", (p.sub,))
        me = await cur.fetchone()
    if not me:
        raise HTTPException(401, "账号不存在")

    lookup = await _login_lookup(me["phone"])
    if not lookup:
        raise HTTPException(401, "账号不存在")
    stored = lookup["password_hash"]

    # --- 2. 校验 + 算新哈希（不占数据库连接）-----------------------------
    if not await verify_password_async(body.old_password, stored):
        raise HTTPException(400, "原密码不正确")
    # "新旧是否相同"直接比字符串：scrypt 每次带随机 salt，同一明文算出来的
    # 哈希必然不同，用哈希比对既判断不了相等，又白白多跑一次 scrypt。
    if body.new_password == body.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    new_hash = await hash_password_async(body.new_password)

    # --- 3. 写回 ---------------------------------------------------------
    async with db.pool().connection() as conn:
        await conn.execute(
            """update forge.users
                  set password_hash = %s, must_change_pwd = false,
                      token_version = token_version + 1, updated_at = now()
                where id = %s""", (new_hash, p.sub))
        cur = await conn.execute(
            "select token_version from forge.users where id = %s", (p.sub,))
        ver = (await cur.fetchone())["token_version"]
        await conn.commit()

    token, exp = issue_token(p.sub, me["enterprise_id"], int(ver), must_change_pwd=False)
    return {"ok": True, "access_token": token, "token_type": "bearer",
            "expires_at": exp, "message": "密码已更新，其它设备的登录已失效"}


# ---------------------------------------------------------------------------
# 管理员：邀请码与用户开通
# ---------------------------------------------------------------------------
@admin.post("/invites")
async def create_invite(body: CreateInviteIn,
                        p: Principal = Depends(require("member:write"))) -> Dict[str, Any]:
    roles = [r for r in body.roles if r in ROLE_MATRIX]
    if not roles:
        raise HTTPException(400, "角色无效")
    # 只有平台管理员能发管理员邀请码，避免"管理员发码给管理员"被越权利用
    if "platform_admin" in roles and "platform_admin" not in p.roles:
        raise HTTPException(404, "not found")

    code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:10].upper()
    async with db.tenant_conn(p) as conn:
        await conn.execute(
            """insert into forge.invites
                 (code, enterprise_id, roles, supplier_id, note, created_by,
                  max_uses, expires_at)
               values (%s,%s,%s,%s,%s,%s,%s, now() + make_interval(days => %s))""",
            (code, p.tenant_id, roles, body.supplier_id, body.note, p.sub,
             body.max_uses, body.expires_in_days))
        await db.write_audit(conn, p, "invite.create", code,
                             json.dumps({"roles": roles, "max_uses": body.max_uses},
                                        ensure_ascii=False))
    return {"ok": True, "code": code,
            "roles": roles, "max_uses": body.max_uses,
            "expires_in_days": body.expires_in_days}


@admin.get("/invites")
async def list_invites(p: Principal = Depends(require("member:read"))) -> Dict[str, Any]:
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """select code, roles, max_uses, used_count, note, expires_at, created_at
                 from forge.invites order by created_at desc limit 100""")
        rows = await cur.fetchall()
    return {"items": [
        {"code": r["code"], "roles": r["roles"], "max_uses": r["max_uses"],
         "used_count": r["used_count"], "note": r["note"],
         "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
         "created_at": r["created_at"].isoformat()} for r in rows]}


@admin.get("/users")
async def list_users(status: Optional[str] = None,
                     p: Principal = Depends(require("member:read"))) -> Dict[str, Any]:
    sql = """select id, name, phone, roles, status, supplier_id,
                    must_change_pwd, last_login_at, created_at
               from forge.users where enterprise_id = %s"""
    args: List[Any] = [p.tenant_id]
    if status:
        sql += " and status = %s"
        args.append(status)
    sql += " order by created_at desc limit 200"
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(sql, args)
        rows = await cur.fetchall()
    return {"items": [
        {"id": r["id"], "name": r["name"], "phone": mask_phone(r["phone"]),
         "roles": r["roles"], "status": r["status"], "supplier_id": r["supplier_id"],
         "must_change_password": r["must_change_pwd"],
         "last_login_at": r["last_login_at"].isoformat() if r["last_login_at"] else None,
         "created_at": r["created_at"].isoformat()} for r in rows]}


@admin.patch("/users/{user_id}/status")
async def set_user_status(user_id: str, body: SetStatusIn,
                          p: Principal = Depends(require("member:write"))) -> Dict[str, Any]:
    if user_id == p.sub and body.status != "active":
        raise HTTPException(400, "不能停用自己的账号")
    async with db.tenant_conn(p) as conn:
        # 停用时 bump token_version，正在使用的 token 立即失效
        cur = await conn.execute(
            """update forge.users
                  set status = %s,
                      token_version = token_version + case when %s = 'active' then 0 else 1 end,
                      failed_attempts = 0, locked_until = null, updated_at = now()
                where id = %s and enterprise_id = %s
                returning id, status""",
            (body.status, body.status, user_id, p.tenant_id))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "not found")
        await db.write_audit(conn, p, f"user.{body.status}", user_id, None)
    return {"ok": True, "id": row["id"], "status": row["status"]}


@admin.post("/users/{user_id}/reset-password")
async def reset_password(user_id: str, body: ResetPasswordIn,
                         p: Principal = Depends(require("member:write"))) -> Dict[str, Any]:
    """管理员重置密码。不给 new_password 就生成一个临时密码返回一次。"""
    temp = body.new_password or ("Fg" + secrets.token_urlsafe(9))
    bad = password_problem(temp)
    if bad:
        raise HTTPException(400, bad)
    # 先把哈希算完再进事务：scrypt 要几百毫秒，放在事务里会让连接白白被占住
    temp_hash = await hash_password_async(temp)
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """update forge.users
                  set password_hash = %s, must_change_pwd = true,
                      token_version = token_version + 1,
                      failed_attempts = 0, locked_until = null, updated_at = now()
                where id = %s and enterprise_id = %s
                returning id""",
            (temp_hash, user_id, p.tenant_id))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "not found")
        await db.write_audit(conn, p, "user.reset_password", user_id, None)
    return {"ok": True, "id": row["id"], "temp_password": temp if not body.new_password else None,
            "message": "已重置，用户下次登录需修改密码"}


# ===========================================================================
# 7. 企业统一登录（OIDC Relying Party）
# ===========================================================================
# 完整背景与不可让步的约束见 app/sso.py 的文件头。这里只做编排：
#   前端点"用企业账号登录" → start 拿 authorize_url → 浏览器去 IdP
#   → IdP 带 code 回调前端 → 前端 POST callback → 本服务换 token、映射账号
#   → 签发**本服务自己的** JWT → 前端和手机号登录一样存起来用。
#
# 三种结局都要明确返回，不要让前端靠猜：
#   ok=true                        直接登录成功
#   need_link=true + link_token    撞到已有账号，需要本人输一次本地密码确认
#   need_admin=true                无法自动判定，转人工

class SsoStartIn(BaseModel):
    return_to: str = Field(default="", max_length=400)


class SsoCallbackIn(BaseModel):
    code: str = Field(..., max_length=4000)
    state: str = Field(..., max_length=4000)


class SsoLinkIn(BaseModel):
    link_token: str = Field(..., max_length=4000)
    password: str = Field(..., max_length=200)


def _sign_link_token(provider: str, subject: str, user_id: str,
                     phone: str, name: str) -> str:
    """待确认绑定的短期凭证（10 分钟）。

    里面带 user_id 而不是只带 sub —— 因为用户已经证明了手机号归属，
    这一步只需要他再证明"我知道这个账号的密码"。凭证明细由我们签，
    前端改不动。
    """
    now = int(time.time())
    return jwt.encode({
        "purpose": "sso_link",
        "p": provider, "s": subject, "u": user_id,
        "ph": phone, "nm": name,
        "jti": uuid.uuid4().hex, "iat": now, "exp": now + 600,
        "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
    }, settings.jwt_secret, algorithm="HS256")


def _read_link_token(token: str) -> Dict[str, Any]:
    try:
        c = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                       audience=settings.jwt_audience, issuer=settings.jwt_issuer,
                       options={"require": ["exp", "jti"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(400, "确认超时，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(400, "确认凭证无效")
    if c.get("purpose") != "sso_link":
        raise HTTPException(400, "确认凭证用途不符")
    return c


def _sso_login_payload(row: Dict[str, Any], token: str, exp: int) -> Dict[str, Any]:
    return {
        "ok": True,
        "access_token": token,
        "token_type": "bearer",
        "expires_at": exp,
        "must_change_password": False,   # SSO 账号的凭证由 IdP 管，本地没有初始密码
        "via": "sso",
        "user": {
            "id": row["id"],
            "name": row["name"],
            "phone": mask_phone(row["phone"]) if row.get("phone") else "",
            "roles": [r for r in (row["roles"] or []) if r in ROLE_MATRIX],
        },
    }


@router.get("/sso/status")
async def sso_status() -> Dict[str, Any]:
    """公开端点：前端据此决定是否显示「用企业账号登录」按钮。

    不暴露 issuer / client_id —— 那些属于部署信息，前端不需要知道，
    多给一个就多一个被拿去钓鱼的素材。
    """
    if not settings.sso_enabled:
        return {"enabled": False}
    try:
        await sso_module.discover()
        return {"enabled": True, "provider": settings.sso_provider_name}
    except sso_module.SsoError as exc:
        # 配置完整但 IdP 不可达：如实说"当前不可用"，而不是把按钮藏起来
        # 让人以为系统没这功能。
        log.warning("SSO 状态检查失败: %s", exc.reason)
        return {"enabled": False, "error": exc.reason}


@router.post("/sso/start")
async def sso_start(body: SsoStartIn) -> Dict[str, Any]:
    if not settings.sso_enabled:
        raise HTTPException(404, "未启用企业统一登录")
    try:
        await sso_module.discover()
        state, challenge = sso_module.make_state(body.return_to)
        url = sso_module.build_authorize_url(state, challenge)
    except sso_module.SsoError as exc:
        raise HTTPException(exc.status, exc.reason)
    return {"ok": True, "authorize_url": url}


@router.post("/sso/callback")
async def sso_callback(body: SsoCallbackIn, request: Request) -> Dict[str, Any]:
    if not settings.sso_enabled:
        raise HTTPException(404, "未启用企业统一登录")

    # state 校验失败是"有人在改参数"，不是什么内部错误，必须是 400 而不是
    # 500。之前这一行漏在 try 外面，被打成 500，日志里还带着完整 traceback，
    # 等于把实现细节送给了攻击者。
    try:
        st = sso_module.read_state(body.state)
    except sso_module.SsoError as exc:
        log.warning("SSO state 校验失败: %s", exc.reason)
        raise HTTPException(exc.status, exc.reason)

    try:
        await sso_module.discover()
        tokens = await sso_module.exchange_code(body.code, st["cv"])
        claims = sso_module.verify_id_token(tokens["id_token"])
    except sso_module.SsoError as exc:
        log.warning("SSO 回调失败: %s", exc.reason)
        raise HTTPException(exc.status, exc.reason)

    try:
        prof = sso_module.profile_from_claims(claims)
    except sso_module.SsoError as exc:
        # IdP 少给一个必需 claim 属于配置问题，但对调用方就是"你给的凭证
        # 不够用"，同样要走 4xx 而不是 500。
        log.warning("SSO claim 不完整: %s", exc.reason)
        raise HTTPException(exc.status, exc.reason)
    provider = settings.sso_provider_name
    snapshot = sso_module.safe_claims_snapshot(claims)

    # --- 已绑定：直接登录 -----------------------------------------------
    bound = await sso_module.lookup_by_identity(provider, prof["subject"])
    if bound:
        # 同名 provider 被换到另一个 IdP 时必须拒绝：否则旧的 subject
        # 映射会被新 IdP 冒用（对方的 sub 空间与我们无关）。
        if bound["bound_issuer"] and \
                bound["bound_issuer"].rstrip("/") != settings.sso_issuer.rstrip("/"):
            raise HTTPException(403, "该身份绑定的 IdP 与当前配置不一致，请联系管理员")
        if bound["status"] != "active":
            raise HTTPException(403, "账号未激活或已停用，请联系管理员")

        await sso_module.note_login(provider, prof["subject"])
        row = {"id": bound["user_id"], "name": bound["name"],
               "email": bound["email"] or "", "phone": bound["phone"] or "",
               "roles": bound["roles"], "enterprise_id": bound["enterprise_id"],
               "supplier_id": bound["supplier_id"]}
        token, exp = issue_token(bound["user_id"], bound["enterprise_id"],
                                 int(bound["token_version"]), must_change_pwd=False)
        await _audit(row, "auth.login.sso", "session",
                     {"provider": provider, "sub": prof["subject"]},
                     ip=client_ip(request))
        log.info("SSO 登录成功 user=%s provider=%s", bound["user_id"], provider)
        return _sso_login_payload(row, token, exp)

    # --- 未绑定：找可合并的本地账号 -------------------------------------
    # 分两层判断，不能只看"有没有密码"就往下走：
    #   cands     —— 手机号命中的**所有**本地账号
    #   provable  —— 其中能用"知道本地密码"自证的那部分
    # 只要 cands 非空，就说明这个手机号在本平台已经有主，此时**绝不能**
    # 走到 JIT 自动建号（会撞唯一索引，或者更糟：建出一个冒名账号）。
    cands = await sso_module.candidates_by_phone(prof["phone"])
    provable = [c for c in cands if c["bound_count"] == 0 and c["has_password"]]

    if len(provable) > 1:
        return {"ok": False, "need_admin": True,
                "message": "该手机号对应多个未绑定账号，请联系管理员处理"}

    if len(provable) == 1:
        c = provable[0]
        await sso_module.create_link_request(provider, prof["subject"],
                                            c["user_id"], prof, snapshot)
        return {
            "ok": False,
            "need_link": True,
            "link_token": _sign_link_token(provider, prof["subject"],
                                          c["user_id"], prof["phone"], c["name"]),
            "name": c["name"],
            "phone": mask_phone(prof["phone"]),
            "message": "该手机号已有一个本地账号。请输入该账号的密码完成首次绑定。",
        }

    if cands:
        # 手机号有主，但没法靠密码自证：账号还没设过本地密码，或已被
        # 另一个 IdP 身份绑定。这两种都只能人工审，不给自动合并的口子。
        return {"ok": False, "need_admin": True,
                "message": "该手机号在本平台已有账号，但需要管理员确认后才能关联，"
                           "请联系管理员处理"}

    # --- 全新用户：按策略自动建号 ---------------------------------------
    if not settings.sso_allow_jit:
        return {"ok": False, "need_admin": True,
                "message": "你的企业身份尚未在本平台开通，请联系管理员"}

    try:
        created = await sso_module.jit_create_user(prof, settings.sso_issuer)
    except sso_module.SsoError as exc:
        raise HTTPException(exc.status, exc.reason)
    await sso_module.bind_identity(settings.sso_provider_name, prof["subject"],
                                   created["user_id"], settings.sso_issuer,
                                   prof, snapshot)
    # 重新走一次已绑定分支，保证两条路径的鉴权/审计逻辑完全一致
    bound = await sso_module.lookup_by_identity(provider, prof["subject"])
    if not bound:
        raise HTTPException(500, "绑定后仍查不到身份，请联系管理员")
    token, exp = issue_token(bound["user_id"], bound["enterprise_id"],
                             int(bound["token_version"]), must_change_pwd=False)
    row = {"id": bound["user_id"], "name": bound["name"],
           "email": bound["email"] or "", "phone": bound["phone"] or "",
           "roles": bound["roles"], "enterprise_id": bound["enterprise_id"],
           "supplier_id": bound["supplier_id"]}
    await _audit(row, "auth.login.sso.jit", "session",
                 {"provider": provider, "sub": prof["subject"],
                  "granted_role": bound["roles"]},
                 ip=client_ip(request))
    log.info("SSO 首登自动建号并绑定 user=%s", bound["user_id"])
    return _sso_login_payload(row, token, exp)


@router.post("/sso/link")
async def sso_link(body: SsoLinkIn, request: Request) -> Dict[str, Any]:
    """首次绑定确认：用本地密码证明"这个账号本来就是我的"。

    为什么要求输密码而不是直接信 IdP 的手机号：
    如果对方的 phone_number 声明可以被改成任意值（或对方管理员能改），
    那他就可把自己并到 platform_admin 名下完成提权。多这一步，
    提权的前提就变成"还得知道那个本地账号的密码"，代价高得多。
    """
    if not settings.sso_enabled:
        raise HTTPException(404, "未启用企业统一登录")

    c = _read_link_token(body.link_token)
    phone, user_id = c["ph"], c["u"]

    lookup = await _login_lookup(phone)
    if not lookup or lookup["id"] != user_id:
        raise HTTPException(400, "账号已变更，请重新登录")
    if lookup["status"] != "active":
        raise HTTPException(403, "账号未激活或已停用，请联系管理员")
    if not await verify_password_async(body.password, lookup["password_hash"]):
        await _bump_failure(user_id)
        raise HTTPException(400, "该账号的密码不正确")

    await sso_module.bind_identity(c["p"], c["s"], user_id, settings.sso_issuer,
                                   {"phone": phone, "email": lookup["email"] or "",
                                    "name": lookup["name"] or ""}, "{}")
    await _approve_link_request(c["p"], c["s"], user_id, decided_by=user_id)

    token, exp = issue_token(user_id, lookup["enterprise_id"],
                             int(lookup["token_version"]), must_change_pwd=False)
    row = {"id": user_id, "name": lookup["name"], "email": lookup["email"] or "",
           "phone": lookup["phone"], "roles": lookup["roles"],
           "enterprise_id": lookup["enterprise_id"],
           "supplier_id": lookup["supplier_id"]}
    await _audit(row, "auth.sso.link", subject_of(c["s"]),
                 {"provider": c["p"], "sub": c["s"]}, ip=client_ip(request))
    log.info("SSO 首次绑定完成 user=%s provider=%s", user_id, c["p"])
    return _sso_login_payload(row, token, exp)


def subject_of(s: str) -> str:
    """审计 target 字段用。sub 本身可能很长，截断但保留可辨识前缀。"""
    return f"idp:{s[:48]}"


async def _approve_link_request(provider: str, subject: str, user_id: str,
                               decided_by: str) -> None:
    async with db.pool().connection() as conn:
        await conn.execute(
            """update forge.identity_link_requests
                  set status = 'approved', decided_at = now(), decided_by = %s
                where provider = %s and subject = %s and user_id = %s
                  and status = 'pending'""",
            (decided_by, provider, subject, user_id))
        await conn.commit()


# ---------------------------------------------------------------------------
# 管理员：绑定申请与本地密码开关
# ---------------------------------------------------------------------------
@admin.get("/sso/link-requests")
async def list_link_requests(status_filter: str = "pending",
                             p: Principal = Depends(require("member:read"))) -> Dict[str, Any]:
    """待确认的 SSO 绑定申请。

    连 forge.users 取本租户，避免跨租户看到别人的绑定申请
    （这两张表没有 RLS，隔离必须靠这个 join 显式实现）。
    """
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """select r.id, r.provider, r.subject, r.user_id, r.phone, r.email,
                      r.status, r.created_at, r.expires_at, u.name, u.roles
                 from forge.identity_link_requests r
                 join forge.users u on u.id = r.user_id
                where u.enterprise_id = %s
                  and (%s = '' or r.status = %s)
                order by r.created_at desc limit 100""",
            (p.tenant_id, status_filter, status_filter))
        rows = await cur.fetchall()
    return {"items": [{
        "id": r["id"], "provider": r["provider"], "subject": subject_of(r["subject"]),
        "user_id": r["user_id"], "name": r["name"], "roles": r["roles"],
        "phone": mask_phone(r["phone"]) if r["phone"] else "",
        "email": r["email"], "status": r["status"],
        "created_at": r["created_at"].isoformat(),
        "expires_at": r["expires_at"].isoformat(),
    } for r in rows]}


@admin.post("/sso/link-requests/{rid}/approve")
async def approve_link_request(rid: str, p: Principal = Depends(require("member:write"))) -> Dict[str, Any]:
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """select r.provider, r.subject, r.user_id, r.phone, r.email, r.claims,
                      u.enterprise_id, u.name, u.email as u_email
                 from forge.identity_link_requests r
                 join forge.users u on u.id = r.user_id
                where r.id = %s""", (rid,))
        r = await cur.fetchone()
        if not r or r["enterprise_id"] != p.tenant_id:
            raise HTTPException(404, "not found")
        if r["user_id"] == p.sub:
            raise HTTPException(400, "不能批准自己的绑定申请")

    await sso_module.bind_identity(r["provider"], r["subject"], r["user_id"],
                                   settings.sso_issuer,
                                   {"phone": r["phone"] or "", "email": r["email"] or "",
                                    "name": r["name"] or ""},
                                   json.dumps(r["claims"], ensure_ascii=False)
                                   if not isinstance(r["claims"], str) else r["claims"])
    await _approve_link_request(r["provider"], r["subject"], r["user_id"], p.sub)

    async with db.tenant_conn(p) as conn:
        await db.write_audit(conn, p, "sso.link.approve", subject_of(r["subject"]),
                             {"provider": r["provider"], "user_id": r["user_id"]})
    return {"ok": True, "user_id": r["user_id"]}


@admin.post("/sso/link-requests/{rid}/reject")
async def reject_link_request(rid: str, p: Principal = Depends(require("member:write"))) -> Dict[str, Any]:
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """update forge.identity_link_requests r
                  set status = 'rejected', decided_at = now(), decided_by = %s
                 from forge.users u
                where u.id = r.user_id and u.enterprise_id = %s
                  and r.id = %s and r.status = 'pending'
                returning r.id""",
            (p.sub, p.tenant_id, rid))
        if not await cur.fetchone():
            raise HTTPException(404, "not found")
        await db.write_audit(conn, p, "sso.link.reject", rid, None)
    return {"ok": True, "id": rid}


@admin.post("/users/{user_id}/disable-local-password")
async def disable_local_password(user_id: str,
                                 p: Principal = Depends(require("member:write"))) -> Dict[str, Any]:
    """清掉本地密码，让该账号只能走企业统一登录。

    为什么需要这个：SSO 绑上之后，原来那个"首登未改"的弱本地密码仍然能用，
    等于 SSO 只是多了一条路，旧的弱口令还在。要真正做到"以 SSO 为准"，
    必须显式清掉本地密码，而不是靠人记得去改。
    """
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """update forge.users
                  set password_hash = null, must_change_pwd = false,
                      token_version = token_version + 1, updated_at = now()
                where id = %s and enterprise_id = %s
                returning id""",
            (user_id, p.tenant_id))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "not found")
        await db.write_audit(conn, p, "user.local_password.disable", user_id, None)
    return {"ok": True, "id": user_id, "message": "已清除本地密码，该账号只能通过企业统一登录"}


# ===========================================================================
# 8. 小工具
# ===========================================================================
def _now_ts() -> datetime:
    return datetime.now(timezone.utc)


async def _audit(row: Dict[str, Any], action: str, target: str,
                 detail: Optional[Dict[str, Any]] = None,
                 ip: Optional[str] = None) -> None:
    """登录审计。用真实用户构造 Principal 以便 RLS 上下文正确。

    注意 audit_log.detail 是 text、ip 是 inet，所以 detail 要序列化成字符串；
    直接传 dict 会让 psycopg 报类型错。

    审计写失败绝不能让登录失败 —— 记日志然后继续。
    """
    try:
        p = Principal(
            sub=row["id"], tenant_id=row["enterprise_id"],
            roles=[r for r in (row["roles"] or []) if r in ROLE_MATRIX],
            name=row.get("name") or "", email=row.get("email") or "",
            supplier_id=row.get("supplier_id"),
        )
        payload = json.dumps(detail, ensure_ascii=False) if detail else ""
        async with db.tenant_conn(p) as conn:
            await db.write_audit(conn, p, action, target, payload, ip)
    except Exception as exc:  # noqa: BLE001
        log.warning("登录审计写入失败（不影响登录）: %s", exc)
