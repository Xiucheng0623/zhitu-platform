"""
FORGE Orchestrator · 认证与授权
================================================================
唯一规则源：ROLE_MATRIX 必须与 web/src/lib/rbac.ts 完全一致。
契约测试（tests/test_contract.py）会解析两边的角色×动作组合做比对，
任何一侧新增权限而另一侧没跟上，CI 直接失败。

越权语义：
  - 未认证            → 401（无 token / token 过期）
  - 已认证但无权限    → 404（不泄露资源存在性，与前端行为一致）
  - 有权限但状态不允许 → 409 state_conflict
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request

from .config import settings

# 未改初始密码时仍然放行的端点（白名单而非黑名单：
# 新加的业务端点默认是被拦住的，这才是安全的方向）
MUST_CHANGE_ALLOWED = {
    "/api/v1/auth/me",
    "/api/v1/auth/change-password",
    "/healthz",
    "/docs",
    "/openapi.json",
}

# ---------------------------------------------------------------------------
# RBAC 规则表（10 角色 × 30 动作）
# 逐条镜像 web/src/lib/rbac.ts 的 ROLE_MATRIX —— 以该文件为正本。
# tests/test_contract.py 会解析两侧并比对，任何漂移都会让 CI 失败。
#
# 为什么单独有 finance 角色：
#   移除 ERPNext 后，开票核验与付款审批落在本平台。如果让 ops_director
#   同时握有「发起付款」和「审批付款」，四眼原则（4-eyes principle）就没了。
#   会计域的基本内控不是可选装饰，所以宁可多一个角色。
# ---------------------------------------------------------------------------
ROLE_MATRIX: Dict[str, set] = {
    "platform_admin": {
        "project:read", "project:write", "project:delete", "labeling:write",
        "supplier:read", "supplier:write", "supplier:qualify",
        "member:read", "member:write",
        "task:read", "task:assign", "task:submit",
        "review:read", "review:decide",
        "asset:read", "asset:import",
        "settlement:read", "settlement:write", "settlement:approve",
        "invoice:read", "invoice:write", "invoice:verify",
        "payment:read", "payment:write", "payment:approve",
        "collection:read", "collection:write",
        "performance:read", "settings:write", "audit:read",
    },
    "ops_director": {
        "project:read", "project:write", "labeling:write",
        "supplier:read", "supplier:write", "supplier:qualify",
        "member:read", "member:write",
        "task:read", "task:assign",
        "review:read", "review:decide",
        "asset:read",
        "settlement:read", "settlement:write", "settlement:approve",
        "invoice:read", "invoice:write",
        "payment:read", "payment:approve",
        "collection:read", "collection:write",
        "performance:read", "audit:read",
    },
    # 财务岗：管票据核验与付款执行，不碰业务派单
    "finance": {
        "project:read", "supplier:read", "task:read",
        "settlement:read", "settlement:write", "settlement:approve",
        "invoice:read", "invoice:write", "invoice:verify",
        "payment:read", "payment:write", "payment:approve",
        "performance:read", "audit:read",
    },
    "project_manager": {
        "project:read", "project:write", "labeling:write",
        "supplier:read", "member:read",
        "task:read", "task:assign",
        "review:read", "review:decide",
        "asset:read", "asset:import",
        "settlement:read", "invoice:read",
        "collection:read", "collection:write",
        "performance:read", "audit:read",
    },
    "data_engineer": {"project:read", "task:read", "asset:read", "asset:import",
                      "supplier:read", "collection:read"},
    "reviewer": {
        "project:read", "task:read", "review:read", "review:decide",
        "asset:read", "member:read", "collection:read",
    },
    "supplier_admin": {
        "project:read", "member:read", "member:write", "task:read", "task:assign",
        "settlement:read", "invoice:read", "invoice:write",
        "collection:read", "performance:read",
    },
    "supplier_lead": {"project:read", "member:read", "task:read", "task:assign",
                      "invoice:read", "collection:read"},
    "annotator": {"task:read", "task:submit", "asset:read"},
    # 甲方（客户方）只能看项目与绩效，**外加**改标注流程。
    # 为什么单独给 labeling:write 而不是整体给 project:write：
    #   project:write 连合同金额、单价、交付日期一起放开，甲方改流程是合理诉求，
    #   改报价不是。权限粒度按「能做什么事」切，不按「能进哪个页面」切。
    # 为什么甲方可以直接「应用到标注引擎」：
    #   如果是「甲方提需求 → 内部审核 → 才生效」，就变成了要等别人点头的流程。
    #   而改坏的代价已经有闸门挡住：已有标注时 apply 会 409，必须显式勾确认。
    "client_viewer": {"project:read", "performance:read", "labeling:write"},
}

ALL_ACTIONS = sorted({a for perms in ROLE_MATRIX.values() for a in perms if a != "*"})


@dataclass(frozen=True)
class Principal:
    sub: str                      # 主体唯一 id（User.id 或 SupplierMember.id）
    tenant_id: str                # enterprise_id，RLS 第一道边界
    roles: List[str]
    name: str = ""
    email: str = ""
    supplier_id: Optional[str] = None   # 供应商侧主体：RLS 第二道边界
    is_service: bool = False            # 服务账号（LS webhook 等）
    # 首次登录 / 管理员重置后必须改密。来源是 JWT 的 mp claim（native 模式），
    # 由 current_principal 统一拦截 —— 放这里而不是各路由里各写一遍，
    # 是因为"忘记加拦截"这种疏漏在权限系统里代价太高。
    must_change_pwd: bool = False

    @property
    def permissions(self) -> set:
        acc: set = set()
        for r in self.roles:
            acc |= ROLE_MATRIX.get(r, set())
        return acc

    def can(self, action: str) -> bool:
        perms = self.permissions
        return "*" in perms or action in perms

    def can_any(self, *actions: str) -> bool:
        return any(self.can(a) for a in actions)

    @property
    def is_internal(self) -> bool:
        return self.supplier_id is None

    def as_rls(self) -> Dict[str, str]:
        """RLS 上下文字典：空串表示「未限定」，由 forge.ctx 归一化为 null。"""
        return {
            "forge.enterprise_id": self.tenant_id,
            "forge.supplier_id": self.supplier_id or "",
            # 只有标注员主体才收窄到「分配给自己的任务」；
            # 供应商管理员虽非内部人员，但要能看到旗下全部成员的任务，故不设此边界。
            "forge.assignee_id": self.sub if "annotator" in self.roles else "",
        }


# ---------------------------------------------------------------------------
# Token 解析
# ---------------------------------------------------------------------------
def _parse_dev_token(token: str) -> Principal:
    """开发态演示 token：tenant::role[|role...][@supplier_id][#sub]

    例：
      project_manager
      ops_director|reviewer
      supplier_admin@sup_002
      annotator@sup_002#mem_a1
      ent_other::project_manager          ← 跨租户隔离测试用
    """
    tenant = settings.default_tenant
    if "::" in token:
        tenant, _, token = token.partition("::")
    raw, _, sub = token.partition("#")
    roles_part, _, supplier = raw.partition("@")
    roles = [r.strip() for r in roles_part.split("|") if r.strip()]
    roles = [r for r in roles if r in ROLE_MATRIX] or ["annotator"]
    return Principal(
        sub=sub or f"dev_{roles[0]}",
        tenant_id=tenant or settings.default_tenant,
        roles=roles,
        name=sub or roles[0],
        email=f"{sub or roles[0]}@dev.local",
        supplier_id=supplier or None,
    )


_jwks_client = None


def _jwks() -> Any:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True)
    return _jwks_client


def _parse_jwt(token: str) -> Principal:
    try:
        if settings.oidc_jwks_url:
            key = _jwks().get_signing_key_from_jwt(token).key
            alg = "RS256"
        else:
            key = settings.oidc_hs256_secret
            alg = "HS256"
        claims = jwt.decode(
            token, key, algorithms=[alg],
            audience=settings.oidc_audience or None,
            issuer=settings.oidc_issuer or None,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, f"invalid token: {type(exc).__name__}")

    # role 可能来自 Keycloak realm_access.roles / 顶层 roles / 单值 role
    raw_roles = claims.get("roles") or (claims.get("realm_access") or {}).get("roles")
    if not raw_roles:
        single = claims.get("role")
        raw_roles = [single] if single else []
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]
    roles = [r for r in raw_roles if r in ROLE_MATRIX]

    tenant = claims.get("tenant_id") or claims.get("forge_tenant") or settings.default_tenant
    return Principal(
        sub=str(claims["sub"]),
        tenant_id=str(tenant),
        roles=roles,
        name=claims.get("name") or claims.get("preferred_username") or "",
        email=claims.get("email") or "",
        supplier_id=claims.get("supplier_id") or None,
        is_service=bool(claims.get("is_service")),
    )


async def authenticate(authorization: str) -> Principal:
    """把 Authorization 头解析成 Principal。

    **async 的原因**：native 模式下 roles 与账号状态要每次从库里读
    （改角色/停用立即生效），而查库是异步的。
    dev 与 jwt 模式并不需要 IO，但这个签名统一成 async，
    避免"某种模式下是协程、某种模式下是对象"这种调用方灾难。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, "empty bearer token")
    if settings.auth_mode == "dev":
        return _parse_dev_token(token)
    if settings.auth_mode == "native":
        # 延迟导入：db.py 在模块级 import security（为了 Principal 类型），
        # 这里若在模块级 import auth 就成环。函数内导入是明确且安全的解法。
        from . import auth as _auth

        try:
            claims = _auth.decode_native_token(token)
            return await _auth.load_principal(claims)
        except _auth.NativeTokenError as exc:
            raise HTTPException(exc.status, exc.reason)
    return _parse_jwt(token)


async def current_principal(request: Request,
                            authorization: str = Header(default="")) -> Principal:
    p = await authenticate(authorization)
    # 「必须改密」是一个准入门槛，不是权限点：只放行查看自己、改密、退出。
    # 用 403 + 结构化 detail，前端据此跳 /change-password 而不是报"无权限"。
    if p.must_change_pwd and request.url.path not in MUST_CHANGE_ALLOWED:
        raise HTTPException(403, detail={
            "code": "password_change_required",
            "message": "首次登录或密码已被重置，请先修改密码",
        })
    return p


def require(action: str):
    """声明式权限依赖。无权限返回 404，与前端「看不见即为不存在」一致。"""
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(action):
            raise HTTPException(404, "not found")
        return p
    return dep


def require_any(*actions: str):
    """满足任一动作即可。用于「多个角色都能进」的页面。

    仍然返回 404 而不是 403 —— 与 require 保持同一套越权语义。
    """
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can_any(*actions):
            raise HTTPException(404, "not found")
        return p
    return dep
