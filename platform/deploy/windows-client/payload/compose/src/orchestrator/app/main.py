"""
FORGE Orchestrator · 自研业务中间层
================================================================
职责边界（不做越界的事）：
  - 状态机与权限的唯一权威实现（前端只是提前拦截以改善手感）
  - 通过官方 REST API 驱动 ERPNext（采购/结算单据）与 Label Studio（标注）
  - 审计写入与业务写入同事务，append-only
  - 多租户隔离由 PostgreSQL RLS 强制，应用层不做安全兜底

隔离模型（四道边界，全部落在 PG 策略里）：
  1. enterprise_id  租户
  2. supplier_id    供应商只能看自己承接的项目与人员
  3. assignee_id    标注员只能看分配给自己的任务
  4. 越权一律 404   不泄露资源存在性

启动：uvicorn app.main:app --host 0.0.0.0 --port 8300
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import (assignment, auth, autosync, collection, db, finance, ls_bridge,
               ops, repo, selfcheck, storage)
from .clients import IntegrationError, integrations_health, ls
from .config import settings
from .fsm import StateConflict
from .netutil import client_ip
from .security import (ALL_ACTIONS, ROLE_MATRIX, Principal, authenticate,
                       current_principal, require)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
log = logging.getLogger("forge.api")

VERSION = "0.2.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    err = settings.validate()
    if err:
        raise RuntimeError(f"配置校验失败：{err}")
    if settings.auth_mode == "dev":
        log.warning("AUTH_MODE=dev —— 仅限本地联调，生产必须切到 native")
    elif settings.auth_mode == "native":
        log.info("AUTH_MODE=native —— 手机号登录，JWT 本服务自签（TTL %s 分钟）",
                 settings.jwt_ttl_minutes)
    await db.open_pool()
    if settings.s3_secret_key:
        log.info("对象存储自检: %s", await storage.ensure_bucket())
    else:
        log.warning("未配置 MINIO_ROOT_PASSWORD，对象存储禁用（预签名接口会返回 503）")
    # 权限名自检：写错权限名不会有任何报错（越权一律 404），只能靠启动时扫出来。
    selfcheck.run_startup_checks()
    # 定时对账兜底：webhook 之外的第二条路，回调全丢也能自己追上。
    sync_task = autosync.start()
    # 回调端点只有在配了 secret 时才验签；为空时任何能访问本端口的人都能
    # POST 触发一次同步。生产必须配 LS_WEBHOOK_SECRET（install.sh 会自动生成），
    # 但要让人一眼看出当前是「裸的」，别等真出事才发现。
    if not settings.ls_webhook_secret:
        log.warning("未配置 LS_WEBHOOK_SECRET —— 回调端点无鉴权，"
                    "任何能访问本机 8300 的请求都能触发同步（仅限内网联调）")
    log.info("orchestrator %s 启动完成 (auth=%s)", VERSION, settings.auth_mode)
    try:
        yield
    finally:
        await autosync.stop(sync_task)
        await db.close_pool()


app = FastAPI(
    title="FORGE Orchestrator",
    version=VERSION,
    description="AI 数据生产与供应链管理平台 · 业务中间层",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # 平台前端 + 标注工作台的源。后者由 LS 地址反推（settings.ls_origins）：
    # 标注页里的 UX 增强层要跨源取「标注规范」并回传单题耗时，
    # 漏配这一个源的症状是「规范侧栏一直空着、耗时一直 0 条」，
    # 而 API 本身用 curl 测完全正常 —— 只有浏览器才看得见的失败。
    allow_origins=list(dict.fromkeys([*settings.cors_origins, *settings.ls_origins])),
    allow_credentials=True,
    # PUT 是必须的：标注流程保存用的是「整份替换」语义。
    # 漏过它一次的真实后果：浏览器预检被拒（OPTIONS 400），请求根本没发出去，
    # 前端表现为「点了保存没反应」，而后端日志里一条 PUT 都看不到 ——
    # 只看后端会误判成"前端没调"，只看前端会误判成"后端没存"。
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Tenant",
                   # 标注页的短期上下文凭证（编排层签发、LS 注入层携带）
                   "X-Forge-Ctx"],
    expose_headers=["X-Idempotent-Replay", "X-Request-Id"],
)


# 领域路由：财务（发票/付款/三单匹配）、采集、审核/绩效/资产
app.include_router(finance.router)
app.include_router(collection.router)
app.include_router(ops.router)

# 身份：手机号注册/登录/改密 + 管理员开通（邀请码、启停、重置密码）
app.include_router(auth.router)
app.include_router(auth.admin)

# 标注工作台单点登录票据：平台 JWT → LS session，员工不再二次登录
app.include_router(ls_bridge.router)

# 标注员任务清单：平台分配关系 + LS 实时可标任务数（不是合同条目数）
app.include_router(assignment.router)


# ---------------------------------------------------------------------------
# 统一错误体
# ---------------------------------------------------------------------------
@app.exception_handler(StateConflict)
async def _state_conflict(_: Request, exc: StateConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": {"code": "state_conflict", "entity": exc.entity,
                           "current": exc.current, "target": exc.target,
                           "message": str(exc)}},
    )


@app.exception_handler(IntegrationError)
async def _integration_error(_: Request, exc: IntegrationError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"error": {"code": "integration_failed", "system": exc.system,
                           "message": exc.detail}},
    )


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content={"error": detail})
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": "http_" + str(exc.status_code),
                                           "message": str(detail)}})


# ---------------------------------------------------------------------------
# 幂等中间件：POST/PATCH 带 Idempotency-Key 时，重放直接返回首次结果
# ---------------------------------------------------------------------------
IDEMPOTENT_METHODS = {"POST", "PATCH"}
REPLAY_HEADER = "X-Idempotent-Replay"


@app.middleware("http")
async def idempotency_mw(request: Request, call_next):
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or request.method not in IDEMPOTENT_METHODS:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    try:
        p = await authenticate(auth_header)
    except HTTPException:
        # 认证交给路由依赖处理，避免这里二次报错掩盖真实原因
        return await call_next(request)

    try:
        async with db.tenant_conn(p) as conn:
            cached = await db.idem_lookup(conn, p, key)
    except Exception as exc:  # noqa: BLE001
        log.error("幂等查询失败，降级为直接执行: %s", exc)
        cached = None

    if cached:
        return JSONResponse(
            status_code=cached["status_code"],
            content=cached["response"],
            headers={REPLAY_HEADER: "true"},
        )

    response = await call_next(request)

    # 只缓存成功响应；错误（4xx/5xx）不落库，允许客户端修正后重试同一 key
    if response.status_code < 400:
        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", "replace")[:2000]}
        try:
            async with db.tenant_conn(p) as conn:
                await db.idem_store(conn, p, key, request.method,
                                    request.url.path, response.status_code, payload)
        except Exception as exc:  # noqa: BLE001
            log.error("幂等写入失败（不影响本次结果）: %s", exc)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code,
                        headers=headers, media_type=response.media_type)

    return response


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class AssignBody(BaseModel):
    supplier_id: str = Field(min_length=1)
    assignee_id: str = Field(min_length=1)
    note: str = ""


class BindUserBody(BaseModel):
    """花名册成员绑定登录账号。按手机号绑定（管理端输入手机号最顺手，
    每次去翻 usr_ id 不现实）。"""
    phone: str = Field(min_length=5, max_length=32)


class SubmitBody(BaseModel):
    item_count: int = Field(ge=0, default=0)
    ls_annotation_ref: Optional[str] = None


class ReviewBody(BaseModel):
    stage: str = Field(default="l1", pattern="^(l1|l2|qc)$")
    result: str = Field(pattern="^(pass|rework|reject)$")
    note: str = ""
    sampled: int = Field(ge=0, default=0)
    defects: int = Field(ge=0, default=0)


class SupplierStatusBody(BaseModel):
    status: str = Field(pattern="^(qualifying|active|probation|suspended)$")
    note: str = ""


class CreateProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    client: str = Field(default="—", max_length=120)
    modality: str = Field(default="text",
                          pattern="^(image|video|audio|text|multimodal)$")
    status: str = Field(default="draft",
                        pattern="^(draft|planning|running|paused|accepting|closed)$")
    total_items: int = Field(ge=0, default=0)
    unit_price: float = Field(ge=0, default=0)
    budget: Optional[float] = None
    due_at: Optional[str] = None
    # 不传就让服务端按模态生成编号：前端算编号一定会撞号，
    # 唯一索引一冲突就 500，用户看到的是"新建失败"却说不出为什么。
    code: Optional[str] = Field(default=None, max_length=40)
    labeling_config_id: Optional[str] = None
    labeling_config_name: Optional[str] = None
    qc_sampling_rate: Optional[int] = Field(default=None, ge=0, le=100)
    max_rework: Optional[int] = Field(default=None, ge=0, le=20)
    supplier_ids: List[str] = []


class GuidelineBody(BaseModel):
    """项目标注规范。存 Markdown 原文，渲染在前端做 —— 存 HTML 等于把
    XSS 面存进库里，而这段内容会被注入到标注页（另一个源）里显示。"""
    markdown: str = Field(default="", max_length=20000)
    images: List[str] = []


class CreateSupplierBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    short_name: Optional[str] = Field(default=None, max_length=40)
    code: Optional[str] = Field(default=None, max_length=40)
    tier: Optional[str] = Field(default=None, pattern="^[SABC]$")
    status: Optional[str] = Field(default=None,
                                  pattern="^(active|probation|qualifying|suspended)$")
    modalities: List[str] = []
    city: Optional[str] = Field(default=None, max_length=40)
    contact_name: Optional[str] = Field(default=None, max_length=40)
    contact_phone: Optional[str] = Field(default=None, max_length=32)
    capacity_hours: Optional[int] = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz(deep: int = 0):
    h = {"ok": True, "version": VERSION, "auth_mode": settings.auth_mode,
         "db": await db.db_health(),
         # 自检与兜底状态放在健康检查里：这两项坏了不会让服务挂掉，
         # 只会让数据静默不同步 —— 放在这里，监控才看得见。
         "selfcheck": selfcheck.checks_summary(),
         "autosync": autosync.snapshot(),
         # 标注员一致性：坏了不会白屏，只是平台显示的数字/数据和 LS 不一样，
         # 所以必须放进健康检查让监控能看见（issues 里写明哪个批次没接入）。
         # 分两层：集合/数量，以及真实数据行的条数与指纹。
         "assignment": assignment.snapshot(),
         "assignment_items": assignment.items_snapshot()}
    h["ok"] = bool(h["db"].get("ok"))
    if deep:
        h["integrations"] = await integrations_health()
    return h


@app.get("/api/v1/me")
async def me(p: Principal = Depends(current_principal)):
    return {"sub": p.sub, "name": p.name, "email": p.email,
            "tenant_id": p.tenant_id, "roles": p.roles,
            "supplier_id": p.supplier_id, "is_internal": p.is_internal,
            "permissions": sorted(p.permissions)}


@app.get("/api/v1/rbac")
async def rbac():
    """给前端契约测试用：与 web/src/lib/rbac.ts 比对，防止两侧规则漂移。"""
    return {"roles": {k: sorted(v) for k, v in ROLE_MATRIX.items()},
            "actions": ALL_ACTIONS}


# ---------------------------------------------------------------------------
# 项目 / 供应商
# ---------------------------------------------------------------------------
@app.get("/api/v1/projects")
async def projects(p: Principal = Depends(require("project:read"))):
    async with db.tenant_conn(p) as conn:
        rows = await repo.list_projects(conn)
    return {"rows": rows, "total": len(rows)}


@app.post("/api/v1/projects")
async def project_create(body: CreateProjectBody, request: Request,
                         p: Principal = Depends(require("project:write"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.create_project(conn, p, body.model_dump(), client_ip(request))
    if not res["ok"]:
        if res["reason"] == "code_exists":
            raise HTTPException(409, "项目编号已存在，请换一个")
        raise HTTPException(400, res["reason"])
    return res


@app.get("/api/v1/projects/{project_id}/guideline")
async def project_guideline(project_id: str,
                            p: Principal = Depends(require("project:read"))):
    """项目标注规范的编辑视图（平台侧）。

    没配置过返回空结构而不是 404：对前端而言「这个项目还没有规范」
    和「这个项目不存在」是两件事，后者由 RLS 过滤后自然查不到。
    """
    async with db.tenant_conn(p) as conn:
        return await repo.get_guideline(conn, project_id)


@app.post("/api/v1/projects/{project_id}/guideline")
async def project_guideline_save(project_id: str, body: GuidelineBody,
                                 request: Request,
                                 p: Principal = Depends(require("labeling:write"))):
    """保存项目标注规范。写进 forge.project_guidelines，标注页右侧侧栏实时生效。

    图片只收 http/https 绝对地址：这段内容会被塞进 <img src>，
    放 javascript: / data: 进来就是一个可执行的注入点。
    """
    bad = [u for u in body.images
           if not (isinstance(u, str) and u.startswith(("http://", "https://")))]
    if bad:
        raise HTTPException(400, "示例图只支持 http/https 绝对地址")
    if len(body.images) > 12:
        raise HTTPException(400, "示例图最多 12 张")
    async with db.tenant_conn(p) as conn:
        res = await repo.save_guideline(conn, p, project_id, body.markdown,
                                       body.images, client_ip(request))
    if not res["ok"]:
        # 项目不存在或不在可见范围内，统一 404（与全局越权语义一致）
        raise HTTPException(404, "not found")
    return res


class LsBindingBody(BaseModel):
    """把平台项目绑定到一个**已经存在**的 Label Studio 项目。"""
    ls_project_id: int = Field(gt=0)
    config_name: Optional[str] = Field(default=None, max_length=120)


@app.get("/api/v1/projects/{project_id}/ls-binding")
async def project_ls_binding(project_id: str,
                             p: Principal = Depends(require("project:read"))):
    """只读：这个项目绑没绑标注引擎、绑的是哪个 LS 项目。

    项目详情里的「在标注引擎里设计」按钮靠它判断能不能进。这不是设计器
    接口 —— 平台不存表单结构，`label_config` 的正本始终在 LS 里。

    `labeling_config_id` 身兼两职：新建项目时写占位串 `lsc_default`
    （意思是「用标准模板」），真正绑定 LS 之后才是数字项目号。只认数字，
    否则界面会显示「已绑定 #lsc_default」这种看起来像真的假话。
    """
    async with db.tenant_conn(p) as conn:
        proj = await repo.project_brief(conn, project_id)
    if not proj:
        raise HTTPException(404, "not found")
    raw = str(proj.get("labeling_config_id") or "").strip()
    ls_id = int(raw) if raw.isdigit() else None
    return {
        "bound": ls_id is not None,
        "ls_project_id": ls_id,
        "ls_config_name": proj.get("labeling_config_name") if ls_id else None,
    }


@app.post("/api/v1/projects/{project_id}/ls-binding")
async def project_ls_binding_save(project_id: str, body: LsBindingBody,
                                  request: Request,
                                  p: Principal = Depends(require("project:write"))):
    """管理员把平台项目绑到已有 LS 项目 —— 甲方进 LS 设计的前置条件。

    为什么不让平台顺手「新建」LS 项目：一个可用的 LS 项目还要挂数据源、
    云存储、成员与导入任务，这些都在 LS 里做。平台建个空壳只会让两边对不上，
    甲方点进去一条数据都没有。这里只做绑定，且先向 LS 核实项目真的存在，
    否则「在标注引擎里设计」会把人送进一个 404。

    权限刻意给 `project:write` 而不是 `labeling:write`：甲方能改表单，
    但不能改「这个项目连的是哪个 LS 项目」—— 那等于把所有数据换了个地方。
    """
    async with db.tenant_conn(p) as conn:
        proj = await repo.project_brief(conn, project_id)
    if not proj:
        raise HTTPException(404, "not found")
    try:
        remote = await ls.get_project(body.ls_project_id)
    except IntegrationError as exc:
        raise HTTPException(502, f"标注引擎里没有这个项目（#{body.ls_project_id}）：{exc}")
    name = (body.config_name or "").strip() or str(remote.get("title") or "标注界面")
    async with db.tenant_conn(p) as conn:
        await repo.set_project_ls(conn, p, project_id, body.ls_project_id,
                                  name, client_ip(request))
    return {"ok": True, "bound": True, "ls_project_id": body.ls_project_id,
            "ls_config_name": name}


@app.get("/api/v1/tasks/{task_id}/efficiency")
async def task_efficiency(task_id: str,
                          p: Principal = Depends(require("performance:read"))):
    """批次单题耗时汇总 —— 直接喂效率与计价模型。

    数据来自标注页上报的 task_time_logs。没有任何上报时返回全 0 +
    items=0，前端据此显示「暂无耗时数据」，而不是画一条假的曲线。
    """
    async with db.tenant_conn(p) as conn:
        return await repo.task_efficiency(conn, task_id)


@app.get("/api/v1/suppliers")
async def suppliers(status: Optional[str] = None,
                    p: Principal = Depends(require("supplier:read"))):
    async with db.tenant_conn(p) as conn:
        rows = await repo.list_suppliers(conn, status)
    return {"rows": rows, "total": len(rows)}


@app.post("/api/v1/suppliers")
async def supplier_create(body: CreateSupplierBody, request: Request,
                          p: Principal = Depends(require("supplier:write"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.create_supplier(conn, p, body.model_dump(), client_ip(request))
    if not res["ok"]:
        if res["reason"] == "code_exists":
            raise HTTPException(409, "供应商编号已存在，请换一个")
        raise HTTPException(400, res["reason"])
    return res


@app.get("/api/v1/supplier-members")
async def supplier_members(supplier_id: Optional[str] = None,
                           p: Principal = Depends(require("member:read"))):
    async with db.tenant_conn(p) as conn:
        rows = await repo.list_members(conn, supplier_id)
    return {"rows": rows, "total": len(rows)}


@app.post("/api/v1/supplier-members/{member_id}/bind-user")
async def supplier_member_bind(member_id: str, body: BindUserBody, request: Request,
                               p: Principal = Depends(require("member:write"))):
    """把花名册成员绑定到平台登录账号。绑上后该账号登录才看得到分配给
    自己的批次（RLS 按 user_id 实时映射）；没绑的成员名下批次没人能作业。"""
    try:
        phone = auth.normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, f"手机号格式不正确：{exc}")
    async with db.tenant_conn(p) as conn:
        res = await repo.bind_member_user(conn, p, member_id, phone,
                                          client_ip(request))
    if not res["ok"]:
        reason = res["reason"]
        if reason in ("member_not_found", "user_not_found"):
            raise HTTPException(404, "not found")
        if reason == "user_inactive":
            raise HTTPException(422, "该账号已停用，无法绑定")
        if reason == "member_already_bound":
            raise HTTPException(409, "该成员已绑定其它登录账号，请先解绑")
        if reason == "user_already_bound":
            raise HTTPException(409, "该手机号已绑定其它花名册成员")
        raise HTTPException(400, reason)
    return res


@app.delete("/api/v1/supplier-members/{member_id}/bind-user")
async def supplier_member_unbind(member_id: str, request: Request,
                                 p: Principal = Depends(require("member:write"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.unbind_member_user(conn, p, member_id, client_ip(request))
    if not res["ok"]:
        raise HTTPException(404, "not found")
    return res


@app.post("/api/v1/suppliers/{supplier_id}/status")
async def supplier_status(supplier_id: str, body: SupplierStatusBody, request: Request,
                          p: Principal = Depends(require("supplier:qualify"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.set_supplier_status(conn, p, supplier_id, body.status, client_ip(request))
    if not res["ok"]:
        raise HTTPException(404, "not found")
    return res


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def _scrub_tasks(p: Principal, res: Dict[str, Any]) -> Dict[str, Any]:
    """对无结算权限的角色剥掉单价字段。

    为什么在服务端裁：unit_price 是结算口径（内部成本），标注员只需要知道
    「标什么、标多少、什么时候交」。前端藏列不算数 —— 接口吐了就是泄露。
    有 settlement:read 的角色（财务/运营总监/管理员）保持原样。
    """
    if p.can("settlement:read"):
        return res
    for row in res.get("rows", []):
        row.pop("unit_price", None)
    return res


@app.get("/api/v1/tasks")
async def tasks(project_id: Optional[str] = None, status: Optional[str] = None,
                supplier_id: Optional[str] = None, limit: int = 200, offset: int = 0,
                p: Principal = Depends(require("task:read"))):
    limit = max(1, min(limit, 500))
    async with db.tenant_conn(p) as conn:
        res = await repo.list_tasks(conn, project_id, status, supplier_id, limit, offset)
    return _scrub_tasks(p, res)


@app.get("/api/v1/tasks/{task_id}")
async def task_detail(task_id: str, p: Principal = Depends(require("task:read"))):
    async with db.tenant_conn(p) as conn:
        t = await repo.get_task(conn, task_id)
    if t is None:
        raise HTTPException(404, "not found")
    return _scrub_tasks(p, {"rows": [t]})["rows"][0]


@app.post("/api/v1/tasks/{task_id}/assign")
async def task_assign(task_id: str, body: AssignBody, request: Request,
                      p: Principal = Depends(require("task:assign"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.assign_task(conn, p, task_id, body.supplier_id,
                                     body.assignee_id, body.note, client_ip(request))
    if not res["ok"]:
        reason = res["reason"]
        if reason in ("not_found", "member_not_found"):
            raise HTTPException(404, "not found")
        if reason == "member_supplier_mismatch":
            raise HTTPException(422, "该成员不属于所选供应商")
        if reason == "member_inactive":
            raise HTTPException(422, "该成员已停用，无法分配")
        raise HTTPException(400, reason)
    return res


@app.post("/api/v1/tasks/{task_id}/submit")
async def task_submit(task_id: str, body: SubmitBody, request: Request,
                      p: Principal = Depends(require("task:submit"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.submit_task(conn, p, task_id, body.item_count,
                                     body.ls_annotation_ref, client_ip(request))
    if not res["ok"]:
        raise HTTPException(404, "not found")
    return res


@app.post("/api/v1/tasks/{task_id}/review")
async def task_review(task_id: str, body: ReviewBody, request: Request,
                      p: Principal = Depends(require("review:decide"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.review_task(conn, p, task_id, body.stage, body.result,
                                     body.note, body.sampled, body.defects, client_ip(request))
    if not res["ok"]:
        raise HTTPException(404, "not found")
    return res


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
@app.get("/api/v1/settlements")
async def settlements(status: Optional[str] = None, supplier_id: Optional[str] = None,
                      p: Principal = Depends(require("settlement:read"))):
    async with db.tenant_conn(p) as conn:
        rows = await repo.list_settlements(conn, status, supplier_id)
    return {"rows": rows, "total": len(rows)}


@app.post("/api/v1/settlements/{sid}/advance")
async def settlement_advance(sid: str, request: Request,
                             p: Principal = Depends(require("settlement:read"))):
    async with db.tenant_conn(p) as conn:
        res = await repo.advance_settlement(conn, p, sid, client_ip(request))
    if not res["ok"]:
        # 权限不足与不存在同样返回 404，避免探测结算单状态
        raise HTTPException(404, "not found")
    return res


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
@app.get("/api/v1/audit")
async def audit_log(action: Optional[str] = None, limit: int = 100, offset: int = 0,
                    p: Principal = Depends(require("audit:read"))):
    limit = max(1, min(limit, 500))
    async with db.tenant_conn(p) as conn:
        rows = await db.list_audit(conn, limit, offset, action)
    return {"rows": rows, "total": len(rows), "page": offset // limit + 1, "page_size": limit}


# ---------------------------------------------------------------------------
# Label Studio 回调：LS 里的提交实时回流到平台状态机
# ---------------------------------------------------------------------------
@app.post("/api/v1/webhooks/labelstudio")
async def ls_webhook(request: Request,
                     x_forge_secret: str = Header(default=""),
                     x_ls_webhook: str = Header(default="labelstudio")):
    if settings.ls_webhook_secret and x_forge_secret != settings.ls_webhook_secret:
        raise HTTPException(404, "not found")

    payload: Dict[str, Any] = await request.json()
    project_id = payload.get("project", {}).get("id")
    action = payload.get("action", "")
    task_id_ls = payload.get("task", {}).get("id")

    if not project_id:
        return {"ok": False, "reason": "no_project"}

    # 用服务主体执行：webhook 来自 LS，没有终端用户身份；只按 ls_project_id 定位
    from .security import Principal as _P
    svc = _P(sub="svc_labelstudio", tenant_id=settings.default_tenant,
             roles=["platform_admin"], name="Label Studio", is_service=True)

    # 顺序很关键：先确认这个 project 真是平台绑定的项目，再决定要不要去打 LS。
    # 原来反了 —— 不看 project 是谁就先 project_summary() 翻全量页（最多 100 页
    # ×100 行，2000 行约 10 秒），之后才查库。于是 secret 为空时（内网联调就是），
    # 随便编个 project id 连发几十条，就能让平台替调用方去压 LS，顺手把审计表刷满。
    async with db.tenant_conn(svc) as conn:
        cur = await conn.execute(
            "select id, status from forge.tasks where ls_project_id = %s limit 1",
            (project_id,))
        row = await cur.fetchone()

    if row is None:
        # 未绑定的项目直接忽略：不打 LS、不写审计。返回 200 是刻意的 ——
        # 让 LS 认为投递成功，避免它反复重试；但对平台不产生任何副作用。
        return {"ok": True, "ignored": "unknown_project"}

    # 拿 LS 的真实统计再落账。
    # 为什么不直接用 payload 里的 task.id 数数：一次标注事件只知道"这一题被改了"，
    # 不知道"这一批一共做完几条"。让 LS 自己报总数，才是同一个口径 ——
    # 否则并发提交时前端算出来的 done_count 会来回跳。
    #
    # 这里按**前缀**匹配而不是枚举全名：LS 的事件名有单复数两套
    # （ANNOTATION_CREATED / ANNOTATIONS_CREATED / ANNOTATIONS_DELETED），
    # 按全名枚举一定会漏，漏掉的那个恰好是"删除"，症状是进度只涨不跌。
    is_annotation_event = action.startswith("ANNOTATION")
    ls_stats: Dict[str, Any] = {}
    if is_annotation_event:
        try:
            ls_stats = await ls.project_summary(int(project_id))
        except Exception as exc:  # noqa: BLE001 - LS 抖一下不能让回调 500
            log.warning("webhook 取 LS 统计失败 project=%s %s", project_id, exc)

    async with db.tenant_conn(svc) as conn:
        if ls_stats:
            task = await repo.get_task(conn, row["id"])
            if task is not None:
                applied = await repo.apply_ls_progress(conn, svc, task, ls_stats,
                                                       "webhook")
                # 打一行可核对的日志：同步类问题十有八九是"回调收到了但数字没动"，
                # 没有这行就只能靠翻审计表猜。
                log.info("LS 回调落账 project=%s task=%s %s→%s/%s status %s→%s",
                         project_id, row["id"], task.get("done_count"),
                         applied.get("done_count"), applied.get("item_count"),
                         applied.get("previous_status"), applied.get("status"))
        elif is_annotation_event and "DELETED" not in action:
            # 取不到统计时退化成原来的行为：至少把状态推到已提交
            # （删除事件不推状态：删标注不等于交付）
            if row["status"] in ("assigned", "in_progress"):
                await conn.execute(
                    "update forge.tasks set status = 'submitted', updated_at = now() where id = %s",
                    (row["id"],))
        await db.write_audit(conn, svc, "ls.annotation", row["id"],
                             f"project={project_id} ls_task={task_id_ls} {action}")
    return {"ok": True, "synced": bool(ls_stats)}


@app.get("/api/v1/ls/reconcile")
async def ls_reconcile(p: Principal = Depends(require("task:read"))):
    """平台批次 vs 标注引擎项目：逐条列出差异（只读，不做任何写入）。

    为什么要有这个接口：两边一旦对不上，一线看到的进度就是假的。
    与其让人肉去 LS 里数，不如把差异摆出来，让人决定要不要同步。

    路径放在 /ls 下而不是 /tasks/ls-reconcile：/api/v1/tasks/{task_id} 先注册，
    任何 /tasks/xxx 都会被它吃掉当成任务 id，表现为莫名其妙的 404。
    """
    async with db.tenant_conn(p) as conn:
        tasks = await repo.tasks_with_ls(conn, p)
    rows, errors = [], []
    # 本轮已经取到的 LS 实时数据交给 assignment 自检复用：管理侧对账面板
    # 要同时看「进度差异」和「标注员任务集合是否一致」，不该把每个项目问两遍。
    remote: Dict[int, Dict[str, Any]] = {}
    for t in tasks:
        pid = int(t["ls_project_id"])
        try:
            summary = await ls.project_summary(pid, with_rows=True)
        except Exception as exc:  # noqa: BLE001
            errors.append({"task_id": t["id"], "ls_project_id": t["ls_project_id"],
                           "error": str(exc)[:160]})
            remote[pid] = {"summary": None, "error": str(exc)[:160]}
            continue
        rows_data = summary.get("rows") or []
        remote[pid] = {
            "summary": {k: v for k, v in summary.items() if k != "rows"},
            "rows": rows_data, "title": summary.get("title") or "", "error": "",
        }
        rows.append(repo.diff_ls(t, summary))
    try:
        async with db.tenant_conn(p) as conn:
            parity = (await assignment.build_assignment(conn, remote))["summary"]
            item_parity = (await assignment.build_items(
                conn, remote, compact=True))["summary"]
    except Exception as exc:  # noqa: BLE001 - 自检失败不影响原有对账结果
        parity = {"in_sync": False, "checked_at": 0, "source": "error",
                  "batches": 0, "assigned": 0, "connected": 0,
                  "not_connected": 0, "unreachable": 0, "contract_items": 0,
                  "actual_items": 0, "actual_done": 0, "ls_projects": 0,
                  "ticket_projects": [],
                  "issues": [f"一致性自检失败：{str(exc)[:160]}"]}
        item_parity = {"in_sync": False, "checked_at": 0,
                       "source": "error", "items": 0, "labeled": 0,
                       "drafts": 0, "ls_projects": 0, "ticket_projects": [],
                       "unreachable": 0, "truncated": 0, "contract_items": 0,
                       "fingerprint": "", "issues": ["数据行自检失败"]}
    return {"rows": rows, "errors": errors,
            "in_sync": sum(1 for r in rows if r["in_sync"]), "total": len(rows),
            # 标注员任务一致性：平台分配集合 vs LS 实际可标任务。
            # 和上面的「进度/名称差异」是两件事，分开摆，别混成一个数字。
            "assignment": parity,
            # 真实数据行级：条数、指纹、不可达/截断。平台不存任务行，
            # 所以这里展示的是同一批 LS 行的汇总，不是另一份数据。
            "items": item_parity,
            # 把兜底任务的状态一起给出去：运营看到"自动对账 5 分钟一次、上次 22:52"
            # 才敢相信「就算回调丢了也会自己好」，否则只能盲信。
            "auto": autosync.snapshot()}


@app.post("/api/v1/tasks/{task_id}/ls-sync")
async def ls_sync(task_id: str, request: Request, align_items: bool = False,
                  p: Principal = Depends(require("task:assign"))):
    """把 LS 的实际进度回写到平台（幂等），并顺手把 LS 项目标题对齐。

    命名以平台为准；进度以 LS 为准 —— 各管一头，避免两边互相覆盖。

    `align_items=1` 时连**条目数**也以 LS 为准。默认关掉，是因为两者语义不同：
      · item_count 是**合同/交付口径**（客户买了 500 条）；
      · LS 的条数是**已导入实际值**（可能只导了 2 条）。
    两边不等通常意味着「导入没做完」，删数字只会把这个真问题掩盖掉，
    所以必须由人显式选择：要么补导入，要么承认合同数改了。

    权限用 `task:assign` 而不是发明的 `task:write`（踩过的坑）：
    权限矩阵里从来没有 task:write 这个动作，`require("task:write")` 恒为假，
    而本项目越权语义是 **404 不是 403**，于是表现为「点同步就提示任务不存在」，
    排查时完全不指向权限 —— 白花了半小时。
    `task:assign`（派/收批次）才是「谁负责这批活谁来对账」对应的现有动作，
    持有者为平台管理员、运营总监、项目经理与供应商管理员/组长；
    供应商侧仍受 RLS 限制，只能同步自己名下批次。
    """
    async with db.tenant_conn(p) as conn:
        task = await repo.get_task(conn, task_id)
    if task is None or task.get("ls_project_id") is None:
        raise HTTPException(404, "not found")

    try:
        summary = await ls.project_summary(int(task["ls_project_id"]))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"标注引擎不可达: {str(exc)[:120]}")

    renamed = False
    want_title = repo.desired_ls_title(task)
    if want_title and summary.get("title") != want_title:
        try:
            await ls.update_project(int(task["ls_project_id"]), title=want_title)
            summary["title"] = want_title
            renamed = True
        except Exception as exc:  # noqa: BLE001 - 改名失败不影响进度回写
            log.warning("LS 项目改名失败 %s: %s", task["ls_project_id"], exc)

    # 顺手把回调挂上（幂等）。不挂的话，标注员在 LS 里提交后平台毫无反应，
    # 只能靠人点同步 —— 这正是之前"两边对不上"的根因。
    webhook: Dict[str, Any] = {"ok": False, "reason": "未配置 FORGE_LS_CALLBACK_URL"}
    if settings.ls_callback_url:
        try:
            webhook = await ls.ensure_webhook(int(task["ls_project_id"]),
                                              settings.ls_callback_url)
        except Exception as exc:  # noqa: BLE001
            webhook = {"ok": False, "reason": str(exc)[:160]}

    async with db.tenant_conn(p) as conn:
        out = await repo.apply_ls_progress(conn, p, task, summary, "manual",
                                           client_ip(request),
                                           align_items=align_items)
    return {**out, "renamed": renamed, "webhook": webhook, "ls": summary,
            "diff": repo.diff_ls(task, summary)}



@app.get("/api/v1/integrations/health")
async def integrations(p: Principal = Depends(require("audit:read"))):
    return {"rows": await integrations_health(),
            "storage": await storage.health(),
            "labelstudio_base": settings.ls_base_url}


# ---------------------------------------------------------------------------
# 文件存储：预签名直传（文件不经过中间层）
# ---------------------------------------------------------------------------
class PresignBody(BaseModel):
    project_id: str = Field(min_length=1)
    batch_no: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_type: str = "application/octet-stream"


@app.post("/api/v1/assets/presign")
async def assets_presign(body: PresignBody, p: Principal = Depends(require("asset:import"))):
    if not settings.s3_secret_key:
        raise HTTPException(503, "对象存储未配置")
    # 项目必须可见，否则等于绕过了项目隔离去往别人项目里塞文件
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute("select 1 from forge.projects where id = %s", (body.project_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, "not found")
    try:
        out = await storage.presign_put(p, body.project_id, body.batch_no,
                                        body.filename, body.content_type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"对象存储不可用: {type(exc).__name__}")
    await out.update({"project_id": body.project_id, "batch_no": body.batch_no})
    return out


@app.get("/api/v1/assets/sign")
async def assets_sign(key: str, p: Principal = Depends(require("asset:read"))):
    if not settings.s3_secret_key:
        raise HTTPException(503, "对象存储未配置")
    try:
        return await storage.presign_get(p, key)
    except PermissionError:
        raise HTTPException(404, "not found")
