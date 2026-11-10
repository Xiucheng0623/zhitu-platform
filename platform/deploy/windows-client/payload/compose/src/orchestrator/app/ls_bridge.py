"""
智途平台 → Label Studio 单点登录桥（编排层侧）
============================================================================
配套文件：platform/labelstudio/brand/forge_sso.py（LS 侧中间件）

为什么存在
----------------------------------------------------------------------------
平台用手机号 JWT，LS 用 Django session，两套凭证互不相认。以前 Annotate.tsx
直接把 iframe 指向 `{LS_BASE}/projects/{id}/data`，浏览器没有 LS session，
LS 就 302 到 /user/login —— 员工得再登录一次，甚至能看到 LS 自带的注册页。

现在的链路：
    前端带平台 JWT 调 GET /api/v1/ls/ticket?project_id=N
      ↓ 本模块校验身份、取出手机号、生成合成邮箱
      ↓ 用 FORGE_LS_SSO_SECRET 签一张 90 秒、一次性的 HMAC 票据
      ↓ 返回浏览器可达的 LS 公网地址 + 票据
    iframe 打开 {LS_PUBLIC}/forge/sso/?ticket=...
      ↓ LS 侧中间件验签、建/找 LS 用户、django.login() 写 sessionid
      ↓ 302 进 /projects/N/data?labeling=1（直接进标注流，不是数据管理表格）
    之后画布里所有请求都带该员工的 LS 会话。

边界（很重要）
----------------------------------------------------------------------------
* 这不是「把两套账号合并成一套」：LS 里仍需要一行用户，用来承载
  annotation.completed_by、任务锁、审计归属。平台只是**代管**它：
  员工永远看不到 LS 密码（set_unusable_password），也不自己注册。
* 编排层原有的 FORGE_LS_REFRESH_TOKEN 服务账号保持不变，继续用于建项目、
  导数据、收 webhook。那是机器身份，与浏览器 SSO 是两条独立的路。
* 票据里除身份与 next 外，只有权限相关的极简集合：布尔 `can_import`
  （能否建项目 / 导数据）与项目可见性 `ls_project_id` / `ls_projects`
  （本次项目 + RLS 下全部可见项目，供 LS 侧做标注员的可见范围收口）。
  它们都不是 LS 角色、不给任何提权入口；LS 侧据此把受限标记与项目
  白名单写进 session，在服务端拦截写接口与越权可见面。
  规则见 LS_RESTRICTED_ROLES。
* 浏览器可达地址用 FORGE_LS_PUBLIC_BASE_URL，与后端内部调用用的
  FORGE_LS_BASE_URL 分开。生产里后者常是 http://labelstudio:8080 这种
  容器内网地址，直接给浏览器会连不上。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import Any, Dict
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from . import db, repo
from .clients import IntegrationError, ls
from .config import settings
from .security import Principal, require, require_any

log = logging.getLogger("forge.ls_bridge")
router = APIRouter(prefix="/api/v1/ls", tags=["ls-bridge"])

PURPOSE = "ls-sso"
TICKET_TTL_S = 90
SSO_PATH = "/forge/sso/"

# LS 合成邮箱的域名。改它等于改身份（LS 按 email 认人），必须与
# labelstudio/brand/forge_sso.py 的 EMAIL_RE / _ensure_user 同步。
LS_EMAIL_DOMAIN = "staff.zhitu.local"

# ---------------------------------------------------------------------------
# 标注页上下文（ctx）凭证
# ---------------------------------------------------------------------------
# 标注页里的 UX 增强层（platform/labelstudio/brand/ls-ux-enhance.js）要从
# LS 的源跨域回调本服务：取「标注规范」、回传「单题耗时」。
#
# 为什么不能复用平台 JWT：LS 是另一个源，拿不到平台的 localStorage；
# 而且把长效 JWT 塞进标注页 HTML 等于把它暴露给第三方前端。
# 做法是：签发 90 秒 SSO 票据时，顺手签一张**只有这两件事权限**的短期上下文
# （purpose=ls-ctx），由 LS 侧 forge_sso.py 注入页面。它不能调任何业务接口
# —— 每个端点各自校验 purpose 与归属，不是一个"万能弱 token"。
CTX_PURPOSE = "ls-ctx"
CTX_TTL_S = 12 * 3600          # 一个工作班次；过期后刷新标注页即续
CTX_MAX_DURATION_MS = 4 * 3600 * 1000

# 在 LS 里被禁止「建项目 / 导入数据 / 连接云存储」的角色。
# 业务口径：供应商要能导入自己承接的批次，普通标注员只能做标注。
# 与 web/src/lib/rbac.ts 的 project:write 故意不同 —— 供应商在平台侧
# 没有项目写权限，但在标注引擎内必须能上传数据。改这里要同步改 forge_sso.py
# 的文档与前端隐藏规则。
LS_RESTRICTED_ROLES = frozenset({"annotator", "client_viewer"})


class TicketOut(BaseModel):
    ok: bool
    url: str
    expires_at: int
    ttl_seconds: int


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_ticket(claims: Dict[str, Any], secret: str) -> str:
    """`base64url(payload).base64url(HMAC-SHA256(payload))`，与 LS 侧完全对称。

    不用 PyJWT 是刻意的：LS 侧只依赖标准库，两边就不会因为 JWT 库版本 /
    algorithm 白名单不一致而互相验不过。
    """
    body = _b64u(json.dumps(claims, separators=(",", ":"),
                           ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = _b64u(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                         hashlib.sha256).digest())
    return f"{body}.{sig}"


def ls_public_base() -> str:
    """浏览器可达的 LS 地址。没单独配就退回内部地址（本机联调即如此）。"""
    return (settings.ls_public_base_url or settings.ls_base_url).rstrip("/")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_ctx(token: str, secret: str) -> Dict[str, Any] | None:
    """验标注页上下文票据。与 forge_sso._verify 完全同构（那边是 LS 侧副本）。

    任何异常都返回 None：解析细节不回给浏览器，也不写进日志。
    """
    body, dot, sig = (token or "").partition(".")
    if not dot or not body or not sig or len(token) > 4096:
        return None
    expected = _b64u(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                              hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        claims = json.loads(_b64u_decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict) or claims.get("purpose") != CTX_PURPOSE:
        return None
    try:
        if int(claims.get("exp", 0)) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    return claims


def make_ctx_token(p: Principal, user: Dict[str, Any], ls_project_id: int,
                   ls_projects: list, task_id: str, secret: str) -> str:
    """签发标注页上下文。内容只有「你是谁 + 能碰哪些批次」，不含角色能力。

    roles / supplier_id 要带上：编排层要用它们重建 RLS 上下文（标注员只看
    自己的行）。它们本来就写在用户自己的 JWT 里，不是额外泄露。

    ls_projects 也带上：标注员在一个 LS 会话里可以翻自己的其他批次（票据
    签发时 RLS 已把范围收窄），页面换项目后必须能取到对应批次的规范，
    否则翻到第二个批次时右侧显示的会是第一个批次的规范。服务端只认
    这张签名名单里的项目 id。
    """
    now = int(time.time())
    return sign_ticket({
        "v": 1,
        "purpose": CTX_PURPOSE,
        "sub": p.sub,
        "tenant": p.tenant_id,
        "roles": list(user.get("roles") or []),
        "supplier_id": user.get("supplier_id"),
        "name": user.get("name") or "",
        "task_id": task_id,
        "ls_project_id": int(ls_project_id),
        "ls_projects": [int(x) for x in ls_projects],
        "iat": now,
        "exp": now + CTX_TTL_S,
    }, secret)


def _pick_project(claims: Dict[str, Any], requested) -> int:
    """决定这次请求作用于哪个 LS 项目。

    优先用调用方指定的项目（页面可能已经翻到下一个批次），但**必须**在
    签名名单 ls_projects 里；不在名单里就退回票据自己的项目，绝不按请求
    的参数去查任意项目 —— 那等于把越权入口开在参数上。
    """
    try:
        want = int(requested) if requested is not None else None
    except (TypeError, ValueError):
        want = None
    allowed = {int(x) for x in (claims.get("ls_projects") or [])
               if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}
    if want is not None and want in allowed:
        return want
    try:
        return int(claims.get("ls_project_id"))
    except (TypeError, ValueError):
        raise HTTPException(401, "标注会话上下文不完整，请重新进入标注工作台")



def _ctx_claims(request: Request) -> Dict[str, Any]:
    """取并校验标注页上下文。头优先，query 兜底（方便用 curl 复现问题）。"""
    secret = settings.ls_sso_secret.strip()
    if len(secret) < 32:
        raise HTTPException(503, "标注工作台单点登录未配置（FORGE_LS_SSO_SECRET）")
    token = (request.headers.get("X-Forge-Ctx")
             or request.query_params.get("ctx") or "")
    claims = verify_ctx(token, secret)
    if not claims:
        # 401 而不是 403：这是「凭证缺失/过期」，前端据此提示重新进入工作台
        raise HTTPException(401, "标注会话已过期，请从平台重新进入标注工作台")
    return claims


def _principal(claims: Dict[str, Any]) -> Principal:
    """从上下文票据重建 Principal，供 tenant_conn 注入 RLS。

    注意 roles 必须带上：Principal.as_rls() 靠 "annotator" in roles 决定
    要不要收窄到「分配给自己的行」。这里若图省事不传，标注员就会以
    「无 assignee 边界」的身份读写，隔离直接失效。
    """
    return Principal(
        sub=str(claims.get("sub") or ""),
        tenant_id=str(claims.get("tenant") or ""),
        roles=list(claims.get("roles") or []),
        name=str(claims.get("name") or ""),
        supplier_id=claims.get("supplier_id") or None,
    )


def synthetic_email(phone: str) -> str:
    """手机号 → LS 合成邮箱。LS 的 USERNAME_FIELD 是 email，不认手机号。

    用独立域名 staff.zhitu.local，保证不会和真实企业邮箱撞车；
    LS 侧 forge_sso.py 用同一个正则白名单校验，别处签的票据塞不进来。

    ⚠️ 这个域名改过（原 staff.shiyan.local，随品牌改名）。改域名看着只是
    换个字符串，实际是**换身份**：LS 按 email 找用户，找不到就新建一个，
    历史标注全留在旧账号上 —— 标注员一登录会发现自己之前干的活"没了"。
    所以两边必须配套：
      * 这里只负责发新域名；
      * LS 侧 EMAIL_RE 同时认新旧两个域名，且 _ensure_user 会把老域名
        账号就地改名接回来（见 forge_sso._ensure_user 的迁移分支）。
    只改一边 = 全员数据脱钩，别只改这里。
    """
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if not re.fullmatch(r"1[3-9]\d{9}", digits):
        raise HTTPException(500, "账号手机号格式异常，无法开通标注账号")
    return f"{digits}@{LS_EMAIL_DOMAIN}"


@router.get("/ticket", response_model=TicketOut)
async def ls_ticket(
    task_id: str = Query(..., min_length=1, max_length=64),
    ls_task_id: int | None = Query(default=None, ge=1),
    p: Principal = Depends(require_any("task:read", "review:read", "asset:read")),
) -> TicketOut:
    """签发一次性 LS 登录票据。前端只拿 URL，看不到密钥也改不了内容。

    **为什么收 task_id 而不是 project_id**：LS 项目 id 由服务端从任务行里
    反查，前端不能指定。否则任何有 task:read 的人都能把 project_id 改成
    任意一个数字，换到一张进别人项目的票据 —— 平台的 RLS 就形同虚设。
    这里再用 db.tenant_conn 查一次任务，RLS 会把供应商/标注员的可见范围
    落实到底（annotator 只能拿到分配给自己的批次）。

    `ls_task_id` 是「从平台数据行列表里点第 N 条」用的：服务端先向 LS
    核实这条任务确实属于该账号可见的项目，再把 `next` 精确到
    `/projects/<id>/data?labeling=1&task=<ls_task_id>`。LS 1.23 的前端
    认这个参数（帮助面板的快捷键规则里就有 `.../data?.*task=\\d+`），
    会直接打开这一条，而不是把人丢回任务队列头部。这样平台里看到的那
    一行，和标注员真正打开的那一条是同一个对象。

    权限：至少要能读任务 / 读审核 / 读资产。纯 client_viewer 只能看报表，
    不该进标注工作台，所以不在放行列表里。
    """
    secret = settings.ls_sso_secret.strip()
    if len(secret) < 32:
        # 配置缺失是运维问题，不是调用方问题：明确报 503 并说明怎么修，
        # 绝不悄悄降级成"用一个固定密钥签"。
        log.error("FORGE_LS_SSO_SECRET 未配置或过短，拒绝签发 LS 票据")
        raise HTTPException(
            503, "标注工作台单点登录未配置（FORGE_LS_SSO_SECRET），请联系系统管理员")

    async with db.tenant_conn(p) as conn:
        # roles 以库里当前值为准，不用 JWT 里的旧快照：管理员刚把某人从
        # 标注员提成供应商，不应该因为 token 没过期就继续被 LS 挡在门外。
        cur = await conn.execute(
            "select phone, name, status, roles, supplier_id from forge.users where id = %s",
            (p.sub,))
        user = await cur.fetchone()
        cur = await conn.execute(
            "select ls_project_id, status from forge.tasks where id = %s", (task_id,))
        task = await cur.fetchone()
        # 该账号当前可见的全部 LS 项目（RLS 已按登录人收窄：标注员只拿到
        # 分配给自己的批次）。票据把整份名单带给 LS 侧做项目白名单 ——
        # 这样一个批次里干完活，同会话里翻自己的其他批次不用回平台换票，
        # 而别人的批次在 LS 里根本不存在。名单以签发时刻为准，解绑后
        # 旧会话最长存活到 LS 会话过期，这是会话模型的固有边界。
        cur = await conn.execute(
            "select distinct ls_project_id from forge.tasks "
            "where ls_project_id is not null")
        visible = await cur.fetchall()
    if not user or user["status"] != "active":
        raise HTTPException(403, "账号不可用")
    if not task:
        # 越权与不存在统一 404，沿用全局语义，不泄露"这个批次是否存在"。
        raise HTTPException(404, "not found")
    ls_project_id = task["ls_project_id"]
    if not ls_project_id:
        raise HTTPException(409, "该批次尚未同步到标注引擎，请先在项目管理里创建 LS 项目")

    # 逐条进工作台：必须先向 LS 核实这条任务属于该账号 RLS 可见的项目。
    # LS CE 没有项目成员隔离，少了这一步，前端传任意 task id 就能打开
    # 别人的数据 —— 票据白名单只挡项目，挡不住项目号被夹带。
    next_path = f"/projects/{int(ls_project_id)}/data?labeling=1"
    if ls_task_id is not None:
        labeled = False
        try:
            remote_task = await ls.get_task(ls_task_id)
            remote_pid = int(remote_task.get("project") or 0)
            labeled = bool(remote_task.get("is_labeled"))
        except (IntegrationError, TypeError, ValueError, AttributeError):
            remote_pid = 0
        if remote_pid != int(ls_project_id):
            log.warning("拒绝跨项目逐条进入：user=%s task=%s ls_task=%s want=%s got=%s",
                        p.sub, task_id, ls_task_id, ls_project_id, remote_pid)
            # 越权与不存在统一 404，不泄露这条任务到底存不存在
            raise HTTPException(404, "not found")
        if labeled:
            # 标注流队列只排未标注任务：labeling=1&task=<已标注id> 会被 LS
            # 静默跳到下一条，平台点第 N 行却打开第 N+1 行。落到数据管理
            # 视图才能精确选中这一条，并显示它已有的标注结果。
            next_path = f"/projects/{int(ls_project_id)}/data?task={ls_task_id}"
        else:
            next_path += f"&task={ls_task_id}"

    email = synthetic_email(user["phone"])

    # ---------------------------------------------------------------------
    # LS 内的「建项目 / 导入数据」权限
    # ---------------------------------------------------------------------
    # 需求：不是所有人都能自己上传项目；供应商可以，普通标注员不行。
    # 这里不复用 rbac 的 project:write —— 供应商角色在平台侧本来就没有
    # project:write（它们只管成员与任务），但按业务口径供应商必须能在
    # 标注引擎里导入自己承接的批次数据。所以用「受限角色黑名单」：
    #   annotator / client_viewer      → 禁止
    #   管理员 / 项目经理 / 审核 / 数据 / 供应商 → 允许
    # 这个布尔值签进票据，LS 侧写进 session 并在服务端拦截写接口，
    # 前端只是顺手把入口藏起来，绝不是安全边界。
    roles = list(user["roles"] or []) or list(p.roles or [])
    can_import = bool(roles) and any(r not in LS_RESTRICTED_ROLES for r in roles)

    now = int(time.time())
    # 白名单：RLS 可见项目 + 本次目标项目（兜底，两者通常一致）。
    visible_projects = sorted({int(r["ls_project_id"]) for r in visible if r["ls_project_id"]})
    if int(ls_project_id) not in visible_projects:
        visible_projects.append(int(ls_project_id))
        visible_projects.sort()
    # 标注页上下文：给 UX 增强层取规范、回传耗时用。LS 侧中间件会把它注入
    # 页面，浏览器拿到的只是一张只能做这两件事的短期票据。
    ctx_token = make_ctx_token(p, dict(user), int(ls_project_id),
                               visible_projects, task_id, secret)
    claims = {
        "v": 1,
        "purpose": PURPOSE,
        "sub": p.sub,
        "email": email,
        "name": user["name"] or p.name or email.split("@")[0],
        "can_import": can_import,
        # 项目可见性：ls_project_id 是本次进入的批次项目，ls_projects 是
        # 该账号此刻全部可见项目（RLS 收窄后的事实）。LS 侧 forge_sso.py
        # 用它们做白名单，标注员在标注引擎里只碰得到自己的批次。
        "ls_project_id": int(ls_project_id),
        "ls_projects": visible_projects,
        # 标注页上下文票据（LS 侧注入 window.__FORGE_CTX__.token）
        "ctx": ctx_token,
        # 平台侧接口的浏览器可达前缀。留空时 LS 侧不会注入这两个 URL，
        # 增强层会明确显示「未配置」，而不是打到 8080 上去吃 404。
        "api_base": (settings.platform_api_base or "").rstrip("/"),
        # LS 1.23 的项目卡片「Label」用的就是这个地址：?labeling=1 会把
        # Data Manager 直接切进标注流。不带参数时落到的是任务表格，员工还得
        # 再点一次「标注全部 Tasks」才看得到评分界面。
        "next": next_path,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + TICKET_TTL_S,
    }
    ticket = sign_ticket(claims, secret)
    url = f"{ls_public_base()}{SSO_PATH}?ticket={quote(ticket, safe='')}"
    log.info("签发 LS 票据 user=%s ls_email=%s task=%s project=%s ls_task=%s ttl=%ds can_import=%s",
             p.sub, email, task_id, ls_project_id, ls_task_id,
             TICKET_TTL_S, can_import)
    return TicketOut(ok=True, url=url, expires_at=claims["exp"],
                     ttl_seconds=TICKET_TTL_S)


@router.get("/design-ticket")
async def design_ticket(
    project_id: str = Query(..., min_length=1, max_length=64),
    p: Principal = Depends(require("labeling:write")),
) -> TicketOut:
    """签发「甲方进 LS 原生标注界面编辑器」的一次性票据。

    为什么走这条路，而不是在平台里再做一个表单设计器：
      Label Studio 自带的 Visual / Code 编辑器覆盖全部控件、条件分支、
      模板浏览和实时预览，且随 LS 升级演进。平台自己实现的那套只能覆盖
      8 种控件 + 一级条件，长尾还得退回手写 XML —— 等于长期维护一份
      永远追不上上游的半成品。正确的做法是让甲方**进 LS 用原生的**，
      平台只负责发一张被严格收窄的门票。

    为什么不是"给甲方开一个 LS 账号"：
      CE 版没有 RBAC / 项目成员隔离 / 自定义权限（全是 Enterprise）。
      一个原生 LS 账号能看所有项目、建项目、导数据、删任务、导出标注。
      所以这里签的是 SSO 票据：mode=design、can_import=False、
      ls_projects 只含**这一个**项目；LS 侧 forge_sso.py 的
      _guard_designer 据此把写操作默认全拒，只留
      `PATCH /api/projects/<id>/` 且请求体只有 label_config 一条例外。
      权限边界仍在服务端，前端隐藏按钮不算数。

    为什么收 project_id 而不是 ls_project_id：
      和 /ls/ticket 收 task_id 同理 —— 让服务端从库里反查，前端不能
      指定任意 LS 项目号换一张进别人项目的票。RLS 在 tenant_conn 里生效。
    """
    secret = settings.ls_sso_secret.strip()
    if len(secret) < 32:
        log.error("FORGE_LS_SSO_SECRET 未配置或过短，拒绝签发设计票据")
        raise HTTPException(
            503, "标注工作台单点登录未配置（FORGE_LS_SSO_SECRET），请联系系统管理员")

    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, code, name, labeling_config_id from forge.projects where id = %s",
            (project_id,))
        proj = await cur.fetchone()
        cur = await conn.execute(
            "select phone, name, status, roles from forge.users where id = %s",
            (p.sub,))
        user = await cur.fetchone()
    if not proj:
        raise HTTPException(404, "not found")
    if not user or user["status"] != "active":
        raise HTTPException(403, "账号不可用")

    # labeling_config_id 身兼两职：新建项目时是占位串 "lsc_default"，
    # 真正绑定 LS 之后才是数字项目号。不是数字就不能签设计票 ——
    # 否则甲方会进到一个还不存在的 LS 项目里，看到的是 404。
    raw = str(proj.get("labeling_config_id") or "").strip()
    if not raw.isdigit():
        raise HTTPException(
            409, "这个项目还没绑定标注引擎。先在平台创建/绑定 LS 项目，再进编辑器。")
    ls_id = int(raw)

    now = int(time.time())
    claims = {
        "v": 1,
        "purpose": PURPOSE,
        "sub": p.sub,
        "email": synthetic_email(user["phone"]),
        "name": user["name"] or p.name or project_id,
        "mode": "design",
        # 设计会话一律不带导入能力：_guard_designer 靠 mode 分流，
        # 但 restricted 标记仍由这个字段置上，旧版 LS 中间件也只会更严。
        "can_import": False,
        "ls_project_id": ls_id,
        "ls_projects": [ls_id],
        # 实测 LS 1.23.2：只有这一个前端路由会渲染「浏览模板 / 代码 /
        # 可视化」编辑器；/settings/ 是通用设置，/labeling-interface 是过渡页。
        "next": f"/projects/{ls_id}/settings/labeling",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + TICKET_TTL_S,
    }
    ticket = sign_ticket(claims, secret)
    url = f"{ls_public_base()}{SSO_PATH}?ticket={quote(ticket, safe='')}"
    log.info("签发 LS 设计票据 user=%s project=%s ls#%s ttl=%ds",
             p.sub, project_id, ls_id, TICKET_TTL_S)
    return TicketOut(ok=True, url=url, expires_at=claims["exp"],
                     ttl_seconds=TICKET_TTL_S)


# ---------------------------------------------------------------------------
# 标注页专用接口（凭据 = 上下文票据，不是平台 JWT）
# ---------------------------------------------------------------------------
class TelemetryIn(BaseModel):
    """单题耗时上报。字段全部可空容错：打点失败绝不能影响标注。"""
    ls_task_id: int | None = None
    event: str                     # enter / leave / submit
    duration_ms: int | None = None
    queue_position: int | None = None
    queue_total: int | None = None
    # 页面当前所在的 LS 项目（标注员可能在同会话里翻了自己的其他批次）。
    # 服务端只认签名名单里的 id，见 _pick_project。
    ls_project_id: int | None = None
    at: str | None = None


# 便宜的写入节流：同一 (账号, 题, 事件) 3 秒内只落一条。
# 为什么需要：增强层是 800ms 轮询 + MutationObserver 双触发的，一旦某次
# 状态判断出错就可能连环上报，把流水表灌成噪声。这只是限流，不是唯一性
# 保证 —— 落库的每一条仍然是真实发生过的事件。
_RECENT: dict = {}
_RECENT_TTL_S = 3.0
_RECENT_MAX = 2048


def _throttled(key: tuple) -> bool:
    now = time.monotonic()
    if len(_RECENT) > _RECENT_MAX:      # 防止长期运行把内存撑大
        _RECENT.clear()
    last = _RECENT.get(key)
    _RECENT[key] = now
    return last is not None and (now - last) < _RECENT_TTL_S


@router.post("/telemetry")
async def ls_telemetry(body: TelemetryIn, request: Request) -> Dict[str, Any]:
    """接收单题耗时打点（标注页 → 编排层）。

    为什么服务端还要再截断一次时长：客户端已经截到 4 小时，但那个脚本跑在
    用户的浏览器里，改个变量就能上报任意值。均值是要进计价模型的，
    这里必须自己再钳一次，并且只接受三态事件名。
    """
    claims = _ctx_claims(request)
    if body.event not in ("enter", "leave", "submit"):
        raise HTTPException(400, "事件类型非法")
    p = _principal(claims)
    ls_project_id = _pick_project(claims, body.ls_project_id)

    dur = body.duration_ms
    if dur is not None:
        dur = max(0, min(int(dur), CTX_MAX_DURATION_MS))
    if _throttled((p.sub, body.ls_task_id, body.event)):
        return {"ok": True, "skipped": "throttled"}

    client_at = None
    if body.at:
        try:
            from datetime import datetime
            client_at = datetime.fromisoformat(body.at.replace("Z", "+00:00"))
        except ValueError:
            client_at = None

    async with db.tenant_conn(p) as conn:
        # ls_project_id → 平台批次。RLS 下标注员只查得到自己的批次，
        # 查不到就留 null（这不是错误：可能是管理员刚把批次从他名下撤了）。
        cur = await conn.execute(
            "select id from forge.tasks where ls_project_id = %s order by id limit 1",
            (ls_project_id,))
        row = await cur.fetchone()
        task_id = row["id"] if row else None
        await conn.execute(
            """
            insert into forge.task_time_logs
                   (enterprise_id, task_id, user_id, ls_project_id, ls_task_id,
                    event, duration_ms, queue_position, queue_total, client_at)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (p.tenant_id, task_id, p.sub, ls_project_id, body.ls_task_id,
             body.event, dur, body.queue_position, body.queue_total, client_at),
        )
    return {"ok": True, "task_id": task_id}


@router.get("/guideline")
async def ls_guideline(request: Request,
                       project: int | None = Query(default=None)) -> Dict[str, Any]:
    """标注页右侧侧栏的规范内容。

    没有配置时返回 configured=false + 空内容，前端据此显示空态说明；
    绝不返回一段编出来的"示例规范"——那会让标注员按错的规则干活。
    """
    claims = _ctx_claims(request)
    p = _principal(claims)
    ls_project_id = _pick_project(claims, project)

    async with db.tenant_conn(p) as conn:
        data = await repo.guideline_for_ls_project(conn, ls_project_id)
    if data is None:
        return {"configured": False, "markdown": "", "images": [],
                "reason": "该标注项目不是从平台创建的，无法关联到项目规范"}
    configured = bool((data.get("markdown") or "").strip() or (data.get("images") or []))
    return {
        "configured": configured,
        "markdown": data.get("markdown") or "",
        "images": data.get("images") or [],
        "project_name": data.get("project_name"),
        "project_code": data.get("project_code"),
        "updated_at": data.get("updated_at"),
    }
