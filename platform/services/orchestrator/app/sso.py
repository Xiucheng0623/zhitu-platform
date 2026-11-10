"""
FORGE Orchestrator · 外部 IdP 单点登录（OIDC Relying Party）
================================================================
架构决策（2026-11，与用户确认过的口径）：

    浏览器 → 对方 IdP 登录 → 回调本服务 → 本服务用 code 换 id_token
                                                    ↓
                                          校验签名/iss/aud/nonce
                                                    ↓
                                          取出 sub（只是身份证号）
                                                    ↓
                                    查 forge.identities → user_id
                                                    ↓
                                  从 forge.users 读 roles（我方权威）
                                                    ↓
                                       签发**本服务自己的** JWT

三条不可让步的约束，写在这里是因为它们最容易在"图省事"时被破坏：

1. **对方的 token 不作为本服务 API 的凭证。**
   它只在回调那一刻被消费一次。理由：对方 token 的 TTL 我们控制不了，
   角色变更/停用也没法即时生效；而本服务的 token 带 token_version，
   改密、停用、降权都能立刻让旧会话失效。

2. **角色不从 claim 里取。**
   如果 `roles` 来自 id_token，那么对方 IdP 的管理员就有权把某人变成
   本平台的 platform_admin —— 那是能审批付款、能看全部审计的角色。
   跨公司的身份源不该握有本系统的授权能力。
   （security.py 里 `_parse_jwt` 仍会读 claim 里的 roles，
     那条路径是给服务账号直连用的，SSO 不走它。）

3. **不按手机号自动合并账号。**
   对方把 phone_number 声明成别人的号码，就能把自己并到那个账号上。
   所以首次撞到已有本地账号时只登记 `identity_link_requests`，
   要求本人再输一次本地密码（证明他本来就拥有该账号）或管理员批准。

失败语义：一律抛 SsoError(status, reason)，由路由层转成统一错误体。
不把底层异常（httpx、jwt 的具体类型）透给前端 —— 那些信息对攻击者
比对运维更有用。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from jwt import PyJWKClient

from .config import settings

log = logging.getLogger("forge.sso")

_DISCOVERY_TTL = 3600
_discovery_cache: Dict[str, Any] = {"at": 0.0, "doc": None}
_jwks_client: Optional[PyJWKClient] = None


class SsoError(Exception):
    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# ---------------------------------------------------------------------------
# 1. 发现（OIDC Discovery）
# ---------------------------------------------------------------------------
def _client() -> httpx.AsyncClient:
    # trust_env 跟随 settings：机房里的全局代理会把发往内网 IdP 的请求
    # 交给代理，症状是随机超时而不是"连不上"，从报错完全看不出根因。
    return httpx.AsyncClient(timeout=settings.outbound_timeout_s,
                             trust_env=settings.http_trust_env)


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


async def discover(force: bool = False) -> Dict[str, Any]:
    """拉取 IdP 的 .well-known，带 1 小时缓存。

    缓存是必要的：前端每次打开登录页都要问一次 SSO 是否可用，
    没有缓存的话对方 IdP 抖一下我们的登录页就打不开。
    """
    now = time.time()
    if not force and _discovery_cache["doc"] and now - _discovery_cache["at"] < _DISCOVERY_TTL:
        return _discovery_cache["doc"]

    url = discovery_url(settings.sso_issuer)
    try:
        async with _client() as c:
            r = await c.get(url)
        if r.status_code != 200:
            raise SsoError(f"IdP discovery 返回 {r.status_code}（{url}）", 502)
        doc = r.json()
    except SsoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SsoError(f"无法访问 IdP discovery（{url}）：{type(exc).__name__}", 502)

    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not doc.get(k):
            raise SsoError(f"IdP discovery 文档缺少 {k}", 502)

    # issuer 必须与配置逐字一致（OIDC 规范要求）。不一致意味着 discovery
    # 被指向了另一个租户，此时接受它的 token 等于信任了错误的身份源。
    if doc["issuer"].rstrip("/") != settings.sso_issuer.rstrip("/"):
        raise SsoError(
            f"发现文档里的 issuer（{doc['issuer']}）与配置（{settings.sso_issuer}）不一致",
            502)

    _discovery_cache.update({"at": now, "doc": doc})
    return doc


def _jwks(uri: str) -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        # cache_keys=True：签名密钥按 kid 缓存，避免每次登录都回源
        _jwks_client = PyJWKClient(uri, cache_keys=True, timeout=settings.outbound_timeout_s)
    return _jwks_client


# ---------------------------------------------------------------------------
# 2. state / PKCE
# ---------------------------------------------------------------------------
def _safe_return_to(raw: str) -> str:
    """把 return_to 收敛到本前端，防止它变成开放重定向。

    目前回调接口只返回 JSON，不拿 rt 去 302，所以还没有可利用点。但这个
    值迟早会被前端用于跳转 —— 到时候如果这里没校验，`return_to=https://
    evil.example` 就是一个能借我们域名骗人的跳板。校验放在写入侧，
    读的地方就不必反复想"这个值可信吗"。
    """
    base = settings.sso_frontend_redirect or "/"
    if not raw:
        return base
    b = urlsplit(base)
    # 相对路径：挂到配置的前端源上，绝不原样返回
    if raw.startswith("/") and not raw.startswith("//"):
        return f"{b.scheme}://{b.netloc}{raw}" if b.scheme and b.netloc else raw
    r = urlsplit(raw)
    if r.scheme in ("http", "https") and r.scheme == b.scheme and r.netloc == b.netloc:
        return raw
    log.warning("忽略不可信的 return_to：%s", raw[:120])
    return base


def make_state(return_to: str = "") -> Tuple[str, str]:
    """返回 (state, code_challenge)。

    state 是一个**自签的短命 JWT**，里面带着 code_verifier 与回跳地址。
    这样不需要在服务端维护会话表 —— 单实例够用，多实例也不用粘性会话。

    安全性说明：state 是签名而非加密，理论上可被读到 code_verifier。
    但 code_verifier 单独没有用 —— 它必须配上那个一次性 authorization code，
    而 code 与 redirect_uri 绑定且只能用一次。所以这个取舍是可接受的，
    换来的是无状态实现。
    """
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(48)                       # 64 字符
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    now = int(time.time())
    claims = {
        "purpose": "sso_state",
        "cv": verifier,
        "rt": _safe_return_to(return_to),
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + settings.sso_state_ttl_seconds,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256"), challenge


def read_state(state: str) -> Dict[str, Any]:
    try:
        claims = jwt.decode(
            state, settings.jwt_secret, algorithms=["HS256"],
            audience=settings.jwt_audience, issuer=settings.jwt_issuer,
            options={"require": ["exp", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise SsoError("登录会话已过期，请重新点击登录", 400)
    except jwt.InvalidTokenError:
        raise SsoError("登录状态无效（state 校验失败）", 400)
    if claims.get("purpose") != "sso_state":
        raise SsoError("登录状态用途不符", 400)
    return claims


def build_authorize_url(state: str, challenge: str) -> str:
    """注意：这里不缓存 discovery，调用方应先 await discover()。"""
    doc = _discovery_cache["doc"]
    if not doc:
        raise SsoError("IdP 尚未初始化，请先调用 discover()", 500)
    params = {
        "response_type": "code",
        "client_id": settings.sso_client_id,
        "redirect_uri": settings.sso_redirect_uri,
        "scope": settings.sso_scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return doc["authorization_endpoint"] + sep + urlencode(params)


# ---------------------------------------------------------------------------
# 3. 换 token + 验签
# ---------------------------------------------------------------------------
async def exchange_code(code: str, verifier: str) -> Dict[str, Any]:
    doc = await discover()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.sso_redirect_uri,
        "client_id": settings.sso_client_id,
        "client_secret": settings.sso_client_secret,
        "code_verifier": verifier,
    }
    try:
        async with _client() as c:
            r = await c.post(doc["token_endpoint"], data=data,
                             headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        raise SsoError(f"换取 token 失败（网络）：{type(exc).__name__}", 502)

    if r.status_code != 200:
        # 常见原因：redirect_uri 与登记值不一致、client_secret 过期、
        # code 已被用过（浏览器回退/重复回调）。
        detail = ""
        try:
            detail = r.json().get("error_description") or r.json().get("error") or ""
        except Exception:  # noqa: BLE001
            detail = r.text[:200]
        log.warning("token 交换失败 %s: %s", r.status_code, detail)
        raise SsoError(f"IdP 拒绝换取 token：{detail or r.status_code}", 401)

    body = r.json()
    if not body.get("id_token"):
        raise SsoError("IdP 未返回 id_token（确认 scope 里含 openid）", 502)
    return body


def verify_id_token(id_token: str, nonce: Optional[str] = None) -> Dict[str, Any]:
    """验证 id_token 并返回 claims。任何一项不过都当失败，不给"降级放行"。"""
    doc = _discovery_cache.get("doc") or {}
    try:
        key = _jwks(doc["jwks_uri"]).get_signing_key_from_jwt(id_token).key
    except Exception as exc:  # noqa: BLE001
        raise SsoError(f"取 IdP 签名公钥失败：{type(exc).__name__}", 502)

    try:
        claims = jwt.decode(
            id_token, key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "PS256"],
            audience=settings.sso_client_id,
            issuer=settings.sso_issuer.rstrip("/"),
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise SsoError("IdP 返回的凭证已过期", 401)
    except jwt.InvalidTokenError as exc:
        raise SsoError(f"IdP 凭证校验失败（{type(exc).__name__}）", 401)

    if nonce is not None and claims.get("nonce") != nonce:
        raise SsoError("nonce 不匹配（疑似重放）", 401)
    return claims


# ---------------------------------------------------------------------------
# 4. claims → 本地画像
# ---------------------------------------------------------------------------
def profile_from_claims(claims: Dict[str, Any]) -> Dict[str, str]:
    """按配置的 claim 名取字段。取不到就是空串，不猜。"""
    def pick(name: str) -> str:
        v = claims.get(name)
        return str(v).strip() if isinstance(v, (str, int)) else ""

    sub = pick(settings.sso_claim_sub)
    if not sub:
        raise SsoError(f"IdP 返回的声明里没有 {settings.sso_claim_sub}，无法识别身份", 401)

    return {
        "subject": sub,
        "phone": pick(settings.sso_claim_phone),
        "name": pick(settings.sso_claim_name),
        "email": pick(settings.sso_claim_email),
    }


def safe_claims_snapshot(claims: Dict[str, Any]) -> str:
    """存档用的声明快照。剔掉容易变长/无意义的字段，并确保可 JSON 序列化。"""
    drop = {"nonce", "at_hash", "c_hash", "azp", "s_hash"}
    slim = {k: v for k, v in claims.items()
            if k not in drop and isinstance(v, (str, int, float, bool, list, dict, type(None)))}
    return json.dumps(slim, ensure_ascii=False)[:4000]


# ---------------------------------------------------------------------------
# 5. 映射到本地账号
# ---------------------------------------------------------------------------
async def lookup_by_identity(provider: str, subject: str) -> Optional[Dict[str, Any]]:
    from . import db

    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select * from forge.identity_lookup(%s, %s)", (provider, subject))
        return await cur.fetchone()


async def candidates_by_phone(phone: str) -> list[Dict[str, Any]]:
    from . import db

    if not phone:
        return []
    async with db.pool().connection() as conn:
        cur = await conn.execute(
            "select * from forge.identity_candidates_by_phone(%s)", (phone,))
        return list(await cur.fetchall())


async def bind_identity(provider: str, subject: str, user_id: str,
                        issuer: str, profile: Dict[str, str],
                        claims_json: str) -> None:
    from . import db

    async with db.pool().connection() as conn:
        await conn.execute(
            """insert into forge.identities
                 (provider, subject, user_id, issuer, email, phone, claims, last_login_at)
               values (%s,%s,%s,%s,%s,%s,%s::jsonb, now())
               on conflict (provider, subject) do nothing""",
            (provider, subject, user_id, issuer,
             profile.get("email") or None, profile.get("phone") or None, claims_json))
        await conn.commit()


async def jit_create_user(profile: Dict[str, str], issuer: str) -> Dict[str, Any]:
    """首次见到该 sub 且没有可合并账号时，建一个最小权限账号。

    刻意用 `sso_jit_role` 而不是去读 claim 里的角色 —— IdP 说什么角色
    都不算数，本平台的权限只能由本平台的管理员授予。
    """
    from . import auth, db
    from .security import ROLE_MATRIX
    import psycopg

    role = settings.sso_jit_role if settings.sso_jit_role in ROLE_MATRIX else "annotator"

    async with db.pool().connection() as conn:
        # users 无 RLS，可直接查；但 enterprises 有 RLS，所以租户直接用配置值
        tenant = settings.default_tenant
        await conn.execute(
            "select set_config('forge.enterprise_id', %s, true)", (tenant,))
        cur = await conn.execute(
            "select id, name from forge.enterprises where id = %s", (tenant,))
        if not await cur.fetchone():
            raise SsoError(f"租户 {tenant} 不存在，无法为新 SSO 用户建号", 500)

        uid = "usr_" + uuid.uuid4().hex[:16]
        name = profile.get("name") or (auth.mask_phone(profile["phone"])
                                       if profile.get("phone") else "SSO 用户")
        try:
            await conn.execute(
                """insert into forge.users
                     (id, enterprise_id, phone, password_hash, name, email, roles,
                      status, must_change_pwd)
                   values (%s, %s, %s, null, %s, %s, %s, 'active', false)""",
                (uid, tenant,
                 # phone 是 NOT NULL 且唯一。SSO 没带手机号时用带前缀的合成值，
                 # 避免多个无手机号的 SSO 用户互相撞唯一索引。
                 profile.get("phone") or ("sso:" + profile["subject"])[:32],
                 name, profile.get("email") or None, [role]))
            await conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            # 手机号撞了本地已有账号。走到这里说明上游没拦住（比如那个
            # 账号没设本地密码、进不了 need_link 分支），绝不能靠"改个号
            # 建进去"糊过去 —— 那样就凭空多出一个冒名账号。
            await conn.rollback()
            raise SsoError("该手机号已被本平台账号占用，请联系管理员处理", 409) from exc

    log.info("SSO 自动建号 user=%s sub=%s role=%s", uid, profile["subject"], role)
    return {"user_id": uid, "enterprise_id": tenant, "name": name,
            "email": profile.get("email") or "", "phone": profile.get("phone") or "",
            "roles": [role], "status": "active", "supplier_id": None,
            "token_version": 0, "must_change_pwd": False}


async def create_link_request(provider: str, subject: str, user_id: str,
                              profile: Dict[str, str], claims_json: str) -> str:
    from . import db

    rid = "lnk_" + uuid.uuid4().hex[:16]
    async with db.pool().connection() as conn:
        await conn.execute(
            """insert into forge.identity_link_requests
                 (id, provider, subject, user_id, phone, email, claims)
               values (%s,%s,%s,%s,%s,%s,%s::jsonb)
               on conflict (provider, subject) do update
                  set status = 'pending',
                      created_at = now(),
                      expires_at = now() + interval '30 minutes',
                      decided_at = null,
                      decided_by = null""",
            (rid, provider, subject, user_id,
             profile.get("phone") or None, profile.get("email") or None, claims_json))
        await conn.commit()
    return rid


async def note_login(provider: str, subject: str) -> None:
    from . import db

    try:
        async with db.pool().connection() as conn:
            await conn.execute(
                "select forge.note_sso_login(%s, %s)", (provider, subject))
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        # 记账失败不能让人登不进来
        log.warning("记录 SSO 登录时间失败（不影响登录）: %s", exc)
