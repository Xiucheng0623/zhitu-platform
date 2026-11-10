# -*- coding: utf-8 -*-
"""
智途平台 → Label Studio 单点登录桥（Label Studio 侧）
============================================================================
部署位置：由 apply_brand.py 复制到
    <venv>/Lib/site-packages/label_studio/core/forge_sso.py
并注册到 core/settings/base.py 的 MIDDLEWARE 末尾。

解决的问题
----------------------------------------------------------------------------
平台账号在 PostgreSQL / forge.users（手机号 + 自签 JWT），LS 账号在
SQLite / htx_user（Django session）。两套凭证互相不认识，员工在平台登录后
点开 iframe 里的标注画布，仍会被 LS 重定向到 /user/login —— 一次登录要输
两次密码，还能误入 LS 自带的注册页（Heidi / 原版营销表单）。

本中间件把「平台身份」换成「LS 会话」：

    平台前端  ──GET /api/v1/ls/ticket（带平台 JWT）──▶ 编排层
        ◀── 90 秒一次性 HMAC 票据 + LS 公网地址 ──────────
    iframe ──GET {LS}/forge/sso/?ticket=... ─────────────▶ 本中间件
        校验签名 / 时效 / 重放
        按票据里的合成邮箱 get_or_create LS 用户（无可用密码、非管理员）
        加入项目所属组织、设为 active_organization
        django.contrib.auth.login() 写下 sessionid
        302 → /projects/{id}/data
    之后画布里所有 /api/* 请求都带这个 sessionid，LS 认为该员工已登录。

设计取舍（都是刻意的，别轻易改）
----------------------------------------------------------------------------
1. **用 HMAC 而不是 LS 的 JWT**：LS 侧只依赖标准库。票据是「平台→LS」的
   单向凭证，不是登录 token；用 HS256 共享密钥 + 90 秒 TTL + jti 一次性，
   比引入 PyJWT/simplejwt 的用户模型耦合更少。
2. **不复制密码、不暴露 LS 凭证**：LS 用户一律 set_unusable_password()，
   密码登录对员工账号永远失败。平台停用账号后票据不再签发，LS 侧会话
   到期即失效。
3. **绝不提权**：新建用户 is_staff=False / is_superuser=False。票据里只有
   一个 `can_import` 布尔值，不是 LS 角色；伪造票据也拿不到 LS 管理员。
4. **上传权限在 LS 服务端落实**：票据里的 can_import=False 时把
   `forge_ls_restricted` 写进 session，随后所有「建项目 / 导入数据 /
   批量建任务 / 连接云存储」的写接口与导入页一律 403。前端隐藏按钮只是
   减少误操作，不是安全边界。供应商与内部管理角色不受限。
5. **组织归属按项目算**：从 next 里的 project id 反查 project.organization，
   多组织部署也不会把人塞错地方；查不到再退回第一个组织。
6. **票据只走一次**：jti 写 Django cache（120 秒）。cache.add 返回 False
   即为重放，直接 403。若部署用了 dummy cache，仍有 90 秒签名时效兜底。
7. **fail closed**：FORGE_LS_SSO_SECRET 没配时，中间件不拦普通请求，但
   /forge/sso/ 一律 503。绝不因为「忘了配」就退化成免鉴权。
8. **标注员在 LS 里只见自己的批次**：批次↔LS 项目 1:1，标注数据本身
   按项目天然隔离，越权面在**项目列表页 / 组织成员页 / 名册 API /
   导出接口 / DM 的 view→task 通道**。票据携带 `ls_projects`（签发时
   RLS 下该账号全部可见项目），受限会话以此为白名单：
   - 项目列表页 302 回自己最近的批次项目；
   - 非白名单项目的页面与 API 一律 404（「看不见即为不存在」，与平台
     编排层同一语义）；
   - 名册面（/api/users、organizations/memberships、project/annotators、
     /people、/organization）与导出面与**凭证签发面**（/api/token 等）
     一律 403 —— 签发面必须封死，否则受限账号换一张 API token 就绕开
     本中间件的全部会话级限制；
   - /api/users 名册：自己保留真实信息，其他人只留 id、姓名邮箱换成占位
     （不能返回空列表 —— DM 任务行按 id 解析 annotators 引用，名册一空，
     有标注的任务会整页崩 "Failed to resolve reference ... to type User"）；
   - 标注流必需的 next_task 动作照常放行，其余 DM 动作（删任务、
     指派等）403。
   前端 sweep 藏入口只是体验，真正的拦截全在本中间件。

生产注意
----------------------------------------------------------------------------
* 平台与 LS 必须**同主域**（例如 platform.example.cn / ls.example.cn），
  否则 Django 默认 SameSite=Lax 的 sessionid 不会随跨站 iframe 发送。
  若确实要跨站，需另设 SESSION_COOKIE_SAMESITE='None' + SESSION_COOKIE_SECURE。
* 票据出现在 URL 里，会进 LS 访问日志和浏览器历史；90 秒 + 一次性使其
  不可复用。不要把这个 TTL 调到几分钟以上。
* 本文件是「补丁资产」，直接改 site-packages 里的副本会在升级 LS 后丢失。
  改这里，再跑 apply_brand.py。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time

from django.core.cache import cache
from django.http import HttpResponse, HttpResponseRedirect

logger = logging.getLogger("forge.sso")

SSO_PATH = "/forge/sso/"
SECRET_ENV = "FORGE_LS_SSO_SECRET"
PURPOSE = "ls-sso"
TICKET_TTL_S = 90
REPLAY_TTL_S = 120
# 合成邮箱域名白名单。同时认新旧两个域名：
#   staff.zhitu.local   —— 当前品牌（平台侧 ls_bridge.LS_EMAIL_DOMAIN）
#   staff.shiyan.local  —— 改名前的老域名，只为存量账号与在途票据保留
# 少认一个的直接后果是：那批老账号打不开工作台（EMAIL_RE 不过 → _ensure_user
# 返回 None），而不是"被迁走"。所以老域名要留到确认没有存量为止。
EMAIL_DOMAIN = "staff.zhitu.local"
LEGACY_EMAIL_DOMAINS = ("staff.shiyan.local",)
EMAIL_RE = re.compile(
    r"^1[3-9]\d{9}@(?:"
    + "|".join(re.escape(d) for d in (EMAIL_DOMAIN,) + LEGACY_EMAIL_DOMAINS)
    + r")$"
)
PROJECT_RE = re.compile(r"^/projects/(\d+)/")

# ---------------------------------------------------------------------------
# LS 内「建项目 / 上传数据」权限
# ---------------------------------------------------------------------------
# 平台票据里的 can_import=False（普通标注员、只读访客）会写进 session。
# 只有带这个标记的用户被拦；LS 原生管理员、平台服务账号的会话没有该键，
# 完全不受影响 —— 不能因为多加了权限就反而锁死运维入口。
RESTRICTED_SESSION_KEY = "forge_ls_restricted"
# 会话用途：空/其他 = 标注员正常进标注流；"design" = 甲方进原生「标注界面」
# 编辑器。设计会话同样是受限会话，但拦法完全不同（见 _guard_designer）：
# 标注员要能提交标注、不能进设置页；甲方反过来 —— 能进设置页改配置，
# 但不能提交标注、不能碰任何别的写接口。用两套规则而不是在一个 _guard
# 里叠 if，是为了避免"给甲方开个例外"时把标注员的拦截也松掉。
MODE_SESSION_KEY = "forge_ls_mode"
MODE_DESIGN = "design"
# LS 1.23.2 实测：设置页是 SPA 通配路由，所有 /projects/<id>/settings/* 都返回
# 同一份 HTML；只有 /settings/labeling 这一个前端路由会渲染「浏览模板 /
# 代码 / 可视化」编辑器（/settings/ 是通用设置，/settings/labeling-interface
# 只是个过渡页）。所以放行必须锚定到 labeling 这一段。
SETTINGS_LABELING_RE = re.compile(r"^/projects/\d+/settings/labeling/?$")
API_PROJECT_DETAIL_RE = re.compile(r"^/api/projects/(\d+)/?$")
# 原生编辑器保存配置走 PATCH /api/projects/<id>/，请求体里只允许这一个字段。
# 项目对象还有 title / task_data_password / sampling / is_published 等字段，
# 一旦放行"整个 PATCH"，甲方就能顺手改项目名乃至取到数据口令。
DESIGN_ONLY_FIELD = "label_config"
# 可见 LS 项目白名单（list[int]）与最近进入的「家」项目（int）。
# 内容来自票据的 ls_projects / ls_project_id —— 签发时 RLS 已把范围收窄到
# 该账号实际分配到的批次，LS 侧不再自己推断分配关系（两库不通，推断必错）。
PROJECTS_SESSION_KEY = "forge_ls_projects"
HOME_PROJECT_SESSION_KEY = "forge_ls_home_project"

# 标注页上下文票据（编排层签发、purpose=ls-ctx）。UX 增强层用它跨源取
# 规范、回传单题耗时。存 session 而不是放 cookie：只在服务端渲染页面时
# 注入一次，浏览器里只有这一张短期票据，不额外多一个可复用的 cookie。
CTX_SESSION_KEY = "forge_ls_ctx"
API_BASE_SESSION_KEY = "forge_ls_api_base"
# 标注页（Data Manager 带 labeling=1 也走这个路径）
ANNOTATION_PAGE_RE = re.compile(r"^/projects/(\d+)/data/?$")
# 注入的脚本用固定 id 标记，便于排查「到底注入没注入」
CTX_SCRIPT_ID = "forge-ux-ctx"

# 真实路由取自 LS 1.23.2：
#   data_import/urls.py   api/projects/<pk>/import, reimport, tasks/bulk
#   projects/urls.py      api/projects/<pk>/...
#   io_storages/urls.py   api/storages/*
#   data_manager/urls.py  projects/<pk>/data/import （导入页面）
# 注意 **不要** 拦 /api/tasks/<id>/annotations —— 那是标注员提交标注的正路。
WRITE_RULES = [
    (re.compile(r"^/api/projects/?$"), {"POST", "PUT", "PATCH"}),
    (re.compile(r"^/api/projects/\d+/?$"), {"PUT", "PATCH", "DELETE"}),
    (re.compile(r"^/api/projects/\d+/(import|reimport|import/predictions|tasks/bulk)/?$"),
     {"POST", "PUT", "PATCH", "DELETE"}),
    (re.compile(r"^/api/projects/\d+/(imports|reimports|file-uploads)/.*$"),
     {"POST", "PUT", "PATCH", "DELETE"}),
    (re.compile(r"^/api/import/.*$"), {"POST", "PUT", "PATCH", "DELETE"}),
    (re.compile(r"^/api/storages/.*$"), {"POST", "PUT", "PATCH", "DELETE"}),
    (re.compile(r"^/api/tasks/?$"), {"POST"}),
    # 任务数据本身：标注员只产出标注（/api/tasks/<id>/annotations 与 drafts
    # 是标注正路，必须放行），不能改任务内容、不能删任务。正则锚定到任务
    # 详情这一段，故意不吞 /annotations、/drafts 子路径。
    (re.compile(r"^/api/tasks/\d+/?$"), {"POST", "PUT", "PATCH", "DELETE"}),
    # 项目任务列表的批量破坏面（LS 源码注释：get and destroy）
    (re.compile(r"^/api/projects/\d+/tasks/?$"), {"POST", "PUT", "PATCH", "DELETE"}),
    # DM 视图批量清空（前端 deleteTabs: DELETE /api/dm/views/reset）
    (re.compile(r"^/api/dm/views/reset/?$"), {"POST", "PUT", "PATCH", "DELETE"}),
]
# 导入/导出页面（GET）也拦：导出是数据出境面，标注员不需要；正则顺带覆盖
# /projects/new、/projects/create 这两个建项目入口。
IMPORT_PAGE_RE = re.compile(
    r"^/projects/\d+/data/(?:import|export)/?$|^/projects/(new|create)/?$"
)

# ---------------------------------------------------------------------------
# 受限账号的可见性收口（可见性 = 权限，UI 裁剪不是边界）
# ---------------------------------------------------------------------------
# 项目列表面（LS 的 / 就是 main 视图，登录后 302 到 /projects/）
ROOT_PAGE_RE = re.compile(r"^/$|^/projects/?$")
# 组织/名册页面：LS CE 的成员可见面在 /people 与 /organization
ORG_PAGE_RE = re.compile(r"^/(?:people|organization|models)(?:/|$)")
# 项目设置页（标注界面/机器学习/Webhook 等配置面）
SETTINGS_PAGE_RE = re.compile(r"^/projects/\d+/settings(?:/|$)")
# 页面级项目 id 捕获
PROJECT_PAGE_RE = re.compile(r"^/projects/(\d+)(?:/|$)")
API_PROJECT_RE = re.compile(r"^/api/projects/(\d+)(?:/|$)")
API_TASK_RE = re.compile(r"^/api/tasks/(\d+)(?:/|$)")
API_DM_VIEW_RE = re.compile(r"^/api/dm/views/(\d+)(?:/|$)")
# 名册面 / 凭证签发面 / 导出面 / Webhook 配置面
API_ANNOTATORS_RE = re.compile(r"^/api/projects/\d+/annotators(?:/|$)")
API_COUNTS_RE = re.compile(r"^/api/projects/counts(?:/|$)")
API_USERS_RE = re.compile(r"^/api/users(?:/|$)")
API_USER_DETAIL_RE = re.compile(r"^/api/users/(\d+)(?:/|$)")
API_ORG_MEMBER_RE = re.compile(
    r"^/api/organizations/\d+/memberships(?:/|$)|^/api/invite(?:/|$)")
API_TOKEN_RE = re.compile(
    r"^/api/(?:token|jwt)(?:/|$)|^/api/current-user/(?:token|reset-token)(?:/|$)")
API_EXPORT_RE = re.compile(r"^/api/(?:projects/\d+/export\w*|auth/export)(?:/|$)")
# 账号设置页会读 /api/jwt/settings。它跟签发面共用 /api/jwt/ 前缀，
# 被 API_TOKEN_RE 一起误杀 → 页面拿不到设置，整页白屏（点「自定义快捷键」就是这个）。
# 该接口只返回两个布尔值（api_tokens_enabled / legacy_api_tokens_enabled），
# 读不泄露任何密钥；但 POST 是组织级写入，必须继续拦 —— 所以只放行 GET。
API_SETTINGS_READ_RE = re.compile(r"^/api/jwt/settings(?:/|$)")
API_WEBHOOK_RE = re.compile(r"^/api/webhooks(?:/|$)")
# DM 动作入口：标注流的 next_task 是正路，其余动作（删任务/指派/清空）封死
DM_ACTIONS_RE = re.compile(r"^/api/dm/actions/?$")


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _verify(ticket: str, secret: str):
    """验签 + 解 payload。任何异常都返回 None，绝不把解析细节抛给浏览器。"""
    body, dot, sig = ticket.partition(".")
    if not dot or not body or not sig or len(ticket) > 4096:
        return None
    expected = _b64u(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                              hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        claims = json.loads(_b64u_decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    return claims


def _safe_next(value) -> str:
    """只允许站内绝对路径。票据是签过名的，这里仍然再挡一层开放重定向。"""
    if not isinstance(value, str) or not value.startswith("/"):
        return "/"
    if value.startswith("//") or "\\" in value or "\n" in value or "\r" in value:
        return "/"
    if value.startswith(SSO_PATH):
        return "/"
    return value


def _org_for(next_path: str):
    from organizations.models import Organization

    match = PROJECT_RE.match(next_path)
    if match:
        try:
            from projects.models import Project

            project = Project.objects.filter(pk=int(match.group(1))).first()
            if project is not None and project.organization_id:
                return project.organization
        except Exception:  # noqa: BLE001 - 组织解析失败不能让 SSO 直接 500
            logger.exception("按项目解析组织失败，退回默认组织")
    return Organization.objects.order_by("pk").first()


def _canonical_email(email: str) -> str:
    """把老域名的合成邮箱统一改写成当前域名。

    为什么必须做：改域名之后，同一部手机号会以两种写法出现 ——
      * 平台新签的票据：staff.zhitu.local
      * 在途票据 / 老版本编排层：staff.shiyan.local
    不归一化就直接按 email 查人，两种写法各自查不到对方，于是同一个手机号
    会**轮流建出两个账号**（实测：先新域名登录成功，再用老域名票据进来，
    库里多出第二个"张三"，标注留在第一个上）。所以查人之前先归一化，
    「老域名」只在 LEGACY_EMAIL_DOMAINS 里作为**查找旧行**的线索用一次。
    """
    local, at, domain = email.partition("@")
    if at and domain in LEGACY_EMAIL_DOMAINS:
        return f"{local}@{EMAIL_DOMAIN}"
    return email


def _adopt_legacy_email(email: str):
    """把老域名的同号账号就地改名成新域名，返回该 user（没有就 None）。

    为什么需要：品牌改名时合成邮箱域名跟着从 staff.shiyan.local 换成
    staff.zhitu.local。LS 是按 email 认人的 —— 直接按新邮箱走
    create_user 会得到**一个全新的空账号**，而这个人的历史标注、已完成数、
    审核记录全挂在旧账号上。标注员一登录看见"我的活没了"，平台数字却还在，
    两边立刻对不上（并且与「标注员手里的数据 == 平台看到的 == LS 里的」
    这条硬要求直接冲突）。

    就地改 email 是唯一不丢历史的做法：user 主键不变 → Task/Annotation
    的外键全部继续指向同一个账号。同理不改 first_name / password / 组织。

    幂等与边界：
      * 新邮箱已被占用（重复建过号）→ 不动，交给调用方按新邮箱新建，
        避免两个账号抢同一个 email 触发唯一约束异常；
      * 多个老域名按 LEGACY_EMAIL_DOMAINS 顺序找，命中第一个就走。
    """
    if not email.endswith("@" + EMAIL_DOMAIN):
        return None
    prefix = email[: -len(EMAIL_DOMAIN)]      # 保留末尾的 "@"
    from users.models import User

    for domain in LEGACY_EMAIL_DOMAINS:
        legacy = prefix + domain
        if User.objects.filter(email=email).exists():
            return None
        old = User.objects.filter(email=legacy).first()
        if old is None:
            continue
        old.email = email
        old.save(update_fields=["email"])
        logger.info("SSO 迁移老域名账号 %s → %s（主键不变，历史标注保留）",
                    legacy, email)
        return old
    return None


def _ensure_user(claims, next_path: str):
    """按合成邮箱找到/建立 LS 用户，并保证组织归属。返回 user 或 None。"""
    from organizations.models import OrganizationMember
    from users.models import User

    email = _canonical_email((claims.get("email") or "").strip().lower())
    if not EMAIL_RE.match(email):
        logger.warning("SSO 票据邮箱格式非法: %r", claims.get("email"))
        return None

    name = (claims.get("name") or "").strip()[:200]
    user = User.objects.filter(email=email).first()
    if user is None:
        user = _adopt_legacy_email(email)
    if user is None:
        user = User.objects.create_user(
            email=email, password=None,
            first_name=name, username=name,
            is_staff=False, is_superuser=False, is_active=True,
        )
        # password=None 时 Django 会设一个不可用密码；显式再调一次，
        # 免得将来上游改了 create_user 的行为，员工账号突然能用密码登录。
        user.set_unusable_password()
        user.save(update_fields=["password"])
        logger.info("SSO 开通 LS 用户 email=%s platform_sub=%s", email, claims.get("sub"))
    if not user.is_active:
        return None

    # 显示名以平台为准。少这一步的后果：平台改了人名（或整个品牌改名），
    # LS 里这个账号仍挂着建号那天的旧名字 —— 标注明细、导出表、审核记录
    # 都显示旧名，看起来像"另一个人干的活"。只在真的不同时才写。
    if name and user.first_name != name:
        user.first_name = name
        user.save(update_fields=["first_name"])

    org = _org_for(next_path)
    if org is None:
        logger.error("LS 里没有任何组织，无法完成 SSO")
        return None
    if not OrganizationMember.objects.filter(
        user=user, organization=org, deleted_at__isnull=True
    ).exists():
        org.add_user(user)
    if user.active_organization_id != org.pk:
        user.active_organization = org
        user.save(update_fields=["active_organization"])
    return user


def _html(message: str, status: int) -> HttpResponse:
    """错误页保持品牌一致，且绝对不回显票据内容。"""
    return HttpResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>智途 · 标注工作台</title>"
        "<style>body{margin:0;background:#14161A;color:#E8E6E3;font:14px/1.8 "
        "-apple-system,'Microsoft YaHei',sans-serif;display:grid;place-items:center;"
        "height:100vh}h1{font-size:17px;font-weight:600}p{color:#9BA1AA;font-size:13px}"
        "</style></head><body><div style='text-align:center;max-width:420px'>"
        "<h1>智途 · 标注工作台</h1><p>" + message + "</p>"
        "<p>请返回平台，从「标注工作台」重新进入。</p></div></body></html>",
        status=status, content_type="text/html; charset=utf-8",
    )


class ForgeSsoMiddleware:
    """/forge/sso/ 负责换票登录；其余请求只对受限会话拦「上传类写操作」。

    普通标注员在平台侧签到的票据 can_import=False，登录后 session 带
    `forge_ls_restricted`。此后就算他手动拼 URL、直接调 API，甚至绕开被隐藏
    的前端按钮，也过不了这一层。供应商 / 管理员 / LS 原生账号不带该标记。
    受限会话另外收口可见性：只能看到票据白名单里的批次项目，成员名册、
    导出、凭证签发一律不可见（见文件头设计取舍 8）。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == SSO_PATH:
            return self._handle_sso(request)
        if self._restricted(request):
            # 设计会话与标注员会话共用「项目白名单 + 响应 scrub」，但拦截面
            # 分开：原生 label_config 编辑器需要的设置页与 PATCH，对标注员
            # 是禁区，对甲方是唯一允许的动作。
            denial = (self._guard_designer(request) if self._designer(request)
                      else self._guard(request))
            if denial is not None:
                return denial
            return self._inject_ctx(request, self._scrub(request, self.get_response(request)))
        return self._inject_ctx(request, self.get_response(request))

    # -----------------------------------------------------------------------
    # 标注页上下文注入
    # -----------------------------------------------------------------------
    def _inject_ctx(self, request, response):
        """把 window.__FORGE_CTX__ 注入标注页 HTML。

        为什么在中间件里做而不是改模板：模板是 LS 的模板，改它就得在
        apply_brand.py 里维护一段 Django 模板片段；而这里已经能拿到完整的
        session（含票据）与响应体，纯字符串插入更稳，也更容易在失败时
        「原样放行」——注入失败最多是没有侧栏，绝不能白屏。
        """
        if (request.method or "GET").upper() not in ("GET", "HEAD"):
            return response
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return response
        if getattr(response, "streaming", False):
            return response
        content_type = (response.get("Content-Type") or "").lower()
        if "text/html" not in content_type:
            return response
        match = ANNOTATION_PAGE_RE.match(request.path or "")
        if not match:
            return response
        try:
            token = request.session.get(CTX_SESSION_KEY) or ""
            api_base = (request.session.get(API_BASE_SESSION_KEY) or "").rstrip("/")
        except Exception:  # noqa: BLE001 - session 坏了也不能影响标注页渲染
            return response
        if not token:
            # 老票据（编排层升级前签的）没有 ctx，页面照常，只是没有增强层功能
            return response
        try:
            project_id = int(match.group(1))
        except (TypeError, ValueError):
            return response

        payload = {
            "token": token,
            "ls_project_id": project_id,
            # 没配平台地址时留 null：增强层会显示「未配置」而不是打到 8080 上吃 404
            "telemetry_url": f"{api_base}/api/v1/ls/telemetry" if api_base else None,
            "guideline_url": (
                f"{api_base}/api/v1/ls/guideline?project={project_id}"
                if api_base else None),
        }
        body_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        script = (f'<script id="{CTX_SCRIPT_ID}">'
                  f"window.__FORGE_CTX__={body_json};</script>")
        try:
            html = response.content.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            return response
        if f'id="{CTX_SCRIPT_ID}"' in html:
            return response
        lower = html.lower()
        pos = lower.rfind("</head>")
        if pos < 0:
            pos = lower.rfind("</body>")
        if pos < 0:
            return response
        html = html[:pos] + script + html[pos:]
        encoded = html.encode("utf-8")
        response.content = encoded
        response["Content-Length"] = str(len(encoded))
        return response

    @staticmethod
    def _restricted(request) -> bool:
        try:
            return bool(request.session.get(RESTRICTED_SESSION_KEY))
        except Exception:  # noqa: BLE001 - session 不可用时按未受限处理，不误杀
            return False

    @staticmethod
    def _designer(request) -> bool:
        """只有票据里明确写了 mode=design 才是设计会话。

        缺失/异常一律按标注员处理（fail closed）：老版本编排层签的票据没有
        这个字段，宁可让甲方暂时进不去编辑器，也不能默认给他多放权限。
        """
        try:
            return request.session.get(MODE_SESSION_KEY) == MODE_DESIGN
        except Exception:  # noqa: BLE001
            return False

    def _guard_designer(self, request):
        """甲方设计会话的服务端拦截：能改标注界面，别的都不行。

        为什么写操作用「默认全拒 + 一条例外」而不是黑名单：
          Label Studio CE 没有 RBAC（官方对比页里 RBAC / 项目成员隔离 /
          自定义权限全是 Enterprise）。一个能 PATCH 项目对象的账号，如果
          只按路径黑名单拦，甲方换个字段就能改标题、改数据口令、改抽样，
          甚至 DELETE 掉项目。反过来把写操作全部默认拒掉、只留一个字段级
          白名单例外，边界才与「只让他设计表单」这句话严格对齐。

        为什么 GET 默认放行：设置页是 SPA，加载时要拉 /api/projects/<id>/、
        /api/templates/、当前用户、静态资源等十几个接口。逐个枚举白名单
        随时会因为 LS 升级多一个新接口就白屏。所以 GET 侧采用「显式拦
        危险读面 + 项目白名单 + 响应 scrub」三层，写侧才是硬白名单。
        """
        path = request.path or ""
        method = (request.method or "GET").upper()

        # 1) 项目范围：非票据白名单项目一律 404（越权语义与平台一致）。
        #    _resolve_project 同时覆盖 URL 里的 id、任务 id 反查和
        #    ?project=N 这类 query 参数，列表接口再由 _scrub 抹掉。
        project_id = self._resolve_project(request)
        if project_id is not None and not self._allowed(request, project_id):
            return self._deny(request, "not found", 404)

        # 2) 危险读面：导出（数据出境）、凭证签发（能换 token 就能绕开
        #    整个会话级限制）、成员名册、Webhook、云存储、组织成员。
        danger_read = (
            API_EXPORT_RE, API_TOKEN_RE, API_ANNOTATORS_RE, API_USERS_RE,
            API_USER_DETAIL_RE, API_ORG_MEMBER_RE, API_WEBHOOK_RE,
            API_COUNTS_RE,
        )
        if any(rx.match(path) for rx in danger_read):
            return self._deny(request, "not found", 404)
        if re.match(r"^/api/storages/", path):
            return self._deny(request, "not found", 404)
        if IMPORT_PAGE_RE.match(path):
            return self._deny(request, "当前账号不能导入、导出或新建项目。", 403)
        if ORG_PAGE_RE.match(path):
            return self._deny(request, "not found", 404)

        # 3) 设置页：只放行 labeling 这一页，其余设置页（通用/标注/模型/
        #    预测/云存储/Webhook/危险操作区）一律挡住。
        if method in ("GET", "HEAD") and SETTINGS_PAGE_RE.match(path):
            if not SETTINGS_LABELING_RE.match(path):
                return self._deny(request, "当前账号只能编辑标注界面。", 403)
            return None

        # 4) 写操作：默认全拒。唯一例外是原生编辑器保存 label_config。
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            if method == "PATCH" and API_PROJECT_DETAIL_RE.match(path):
                return self._design_patch_ok(request)
            return self._deny(request, "当前账号只能修改标注界面配置。", 403)
        return None

    def _design_patch_ok(self, request):
        """PATCH /api/projects/<id>/ 的字段级白名单。返回 None = 放行。

        这里读 request.body 做检查，不改它：Django 会把 body 缓存，
        后续 view 仍然能正常解析。解析失败一律拒绝，不能"看不懂就放过"。
        """
        m = API_PROJECT_DETAIL_RE.match(request.path or "")
        if not m or not self._allowed(request, m.group(1)):
            return self._deny(request, "not found", 404)
        try:
            body = json.loads((request.body or b"").decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._deny(request, "请求体不是合法 JSON。", 400)
        if not isinstance(body, dict) or set(body.keys()) != {DESIGN_ONLY_FIELD}:
            return self._deny(request, "当前账号只能修改标注界面配置。", 403)
        if not isinstance(body[DESIGN_ONLY_FIELD], str):
            return self._deny(request, "标注界面配置必须是文本。", 400)
        return None

    @staticmethod
    def _projects(request) -> set:
        """票据白名单里的 LS 项目 id 集合。读不出来按空集（fail closed）。"""
        try:
            raw = request.session.get(PROJECTS_SESSION_KEY) or []
            return {int(x) for x in raw if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}
        except Exception:  # noqa: BLE001
            return set()

    def _allowed(self, request, project_id) -> bool:
        try:
            return int(project_id) in self._projects(request)
        except (TypeError, ValueError):
            return False

    def _guard(self, request):
        """受限会话的统一拦截。返回 HttpResponse=拒绝，None=放行。

        顺序：先做项目范围收口（越权语义=404，先判存在性），再拦能力面
        （写操作/凭证/名册/导出），404 一律回 "not found"，不泄露资源是否存在。
        """
        path = request.path or ""
        method = (request.method or "GET").upper()

        # 1) 项目范围收口：非白名单项目 → 404（页面与 API 同一语义）
        project_id = self._resolve_project(request)
        if project_id is not None and not self._allowed(request, project_id):
            return self._deny(request, "not found", 404)

        # 2) 建项目 / 导数据 / 删任务等写操作（含 GET 的导入导出页面）
        if self._is_upload_write(request):
            return self._deny(request, "当前账号为标注员，无权创建项目或上传数据。")
        if method in ("GET", "HEAD") and IMPORT_PAGE_RE.match(path):
            return self._deny(request, "当前账号为标注员，无权导入或导出数据。")

        # 3) 凭证签发面：能换 token 就能绕开全部会话级限制，必须封死
        if API_TOKEN_RE.match(path):
            if not (API_SETTINGS_READ_RE.match(path) and method in ("GET", "HEAD")):
                return self._deny(request, "当前账号无权签发接口凭证。")

        # 4) 名册面：组员/供应商/分配关系不是标注员该看的
        if API_ANNOTATORS_RE.match(path):
            return self._deny(request, "当前账号无权查看项目成员。")
        if API_ORG_MEMBER_RE.match(path):
            return self._deny(request, "当前账号无权查看或管理成员。")
        if API_WEBHOOK_RE.match(path):
            return self._deny(request, "当前账号无权管理 Webhook。")
        if API_COUNTS_RE.match(path):
            return self._deny(request, "not found", 404)
        if API_EXPORT_RE.match(path):
            return self._deny(request, "当前账号无权导出数据。")
        if API_USERS_RE.match(path):
            denial = self._guard_users(request)
            if denial is not None:
                return denial

        # 5) DM 动作面：标注流的 next_task 是正路，其余（删任务/指派/清空）封死
        if DM_ACTIONS_RE.match(path) and method == "POST":
            if (request.GET.get("id") or "") != "next_task":
                return self._deny(request, "当前账号无权执行该批量操作。")

        # 6) 页面面：项目列表 302 回家；组织/设置页 403（设置页属已知项目，403 才对）
        if not path.startswith("/api/") and method in ("GET", "HEAD"):
            if ROOT_PAGE_RE.match(path):
                return self._redirect_home(request)
            if ORG_PAGE_RE.match(path):
                return self._deny(request, "当前账号无权访问成员或组织页面。")
            if SETTINGS_PAGE_RE.match(path):
                return self._deny(request, "当前账号无权访问项目设置。")
        return None

    def _guard_users(self, request):
        """/api/users 名册：列表只回自己；他人详情 404；写操作只许改自己。"""
        path = request.path or ""
        method = (request.method or "GET").upper()
        if re.match(r"^/api/users/?$", path):
            # 列表 → 放行到 _scrub，把名册抹成空列表；写操作（建用户）一律拒
            return None if method in ("GET", "HEAD") else self._deny(
                request, "当前账号无权管理用户。")
        m = API_USER_DETAIL_RE.match(path)
        if m:
            try:
                target = int(m.group(1))
            except (TypeError, ValueError):
                return self._deny(request, "not found", 404)
            own = getattr(request.user, "pk", None)
            if own is not None and target == int(own):
                return None  # 自己的资料/头像随便改
            return self._deny(request, "not found", 404)
        return self._deny(request, "not found", 404)

    def _resolve_project(self, request):
        """解析『这个请求在操作哪个 LS 项目』。解析不到返回 None（不拦）。

        来源优先级：URL 里的项目 id → 任务 id 反查 → DM 视图 id 反查 →
        query 里的 project / view / tabID 参数。DM 前端把 id 放 query
        （实测 invokeAction 传 ?id=next_task&project=N），所以参数层必须查。
        """
        path = request.path or ""
        m = API_PROJECT_RE.match(path) or PROJECT_PAGE_RE.match(path)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                return None
        m = API_TASK_RE.match(path)
        if m:
            from tasks.models import Task

            return (Task.objects.filter(pk=int(m.group(1)))
                    .values_list("project_id", flat=True).first())
        m = API_DM_VIEW_RE.match(path)
        if m:
            from data_manager.models import View

            return (View.objects.filter(pk=int(m.group(1)))
                    .values_list("project", flat=True).first())
        for key in ("project", "view", "tabID", "tabId"):
            raw = request.GET.get(key)
            if raw is None or not str(raw).isdigit():
                continue
            if key == "project":
                return int(raw)
            from data_manager.models import View

            return (View.objects.filter(pk=int(raw))
                    .values_list("project", flat=True).first())
        return None

    def _redirect_home(self, request):
        """项目列表页对受限账号没有意义：302 回自己最近的批次项目。"""
        try:
            home = request.session.get(HOME_PROJECT_SESSION_KEY)
        except Exception:  # noqa: BLE001
            home = None
        allowed = self._projects(request)
        if home is not None and int(home) in allowed:
            return HttpResponseRedirect(f"/projects/{int(home)}/data?labeling=1")
        if allowed:
            return HttpResponseRedirect(f"/projects/{sorted(allowed)[-1]}/data?labeling=1")
        return self._deny(request, "当前账号没有可见的标注项目，请返回平台从任务列表进入。")

    def _scrub(self, request, response):
        """响应级收口：列表接口把白名单外的行整个抹掉。

        为什么还要 scrub：/api/projects 列表与 /api/dm/views 列表天然跨项目，
        404 只能拦『指名道姓』的请求，拦不住『全量列表』。这里按白名单过滤
        响应体 —— 标注员看到的列表里，别人项目根本不存在。
        """
        if (request.method or "GET").upper() not in ("GET", "HEAD"):
            return response
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            return response
        if (response.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return response
        path = request.path or ""
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except Exception:  # noqa: BLE001 - 非 JSON 体（如文件流）原样放行
            return response

        allowed = self._projects(request)
        changed = False

        # 项目列表：只留白名单项目，count 同步改掉
        if re.match(r"^/api/projects/?$", path) and isinstance(payload, dict) \
                and isinstance(payload.get("results"), list):
            kept = [p for p in payload["results"]
                    if isinstance(p, dict) and self._allowed(request, p.get("id"))]
            if len(kept) != len(payload["results"]):
                payload["results"] = kept
                payload["count"] = len(kept)
                changed = True

        # DM 视图（tabs）列表：跨项目视图全量可见，按项目字段过滤
        if re.match(r"^/api/dm/views/?$", path) and isinstance(payload, list):
            kept = [v for v in payload
                    if isinstance(v, dict) and self._allowed(request, v.get("project"))]
            if len(kept) != len(payload):
                payload = kept
                changed = True

        # 用户名册：自己保留真实信息，其他人换成脱敏占位。
        #
        # 为什么不能像以前那样直接返回空列表（踩过的坑）：
        # DM 的任务行里带 annotators[].user 引用，mobx-state-tree 会拿 id 去
        # 名册里解析；名册一空，任何有标注的任务都会整页崩：
        #   Failed to resolve reference '7' to type 'User'
        # 而且 id=7 就是标注员自己 —— 连"自己"都被抹掉了，纯属误伤。
        #
        # 为什么也不返回真实全量：标注员不该看到组员/供应商的身份。
        # 折中：自己真实，其余按 id 保留、内容改成占位，引用能解析、身份不泄漏。
        if re.match(r"^/api/users/?$", path):
            rows = payload.get("results") if isinstance(payload, dict) else payload
            if isinstance(rows, list):
                own = getattr(request.user, "pk", None)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        is_self = own is not None and int(row.get("id")) == int(own)
                    except (TypeError, ValueError):
                        is_self = False
                    if is_self:
                        continue
                    row["email"] = ""
                    row["first_name"] = "标注员"
                    row["last_name"] = ""
                    row["username"] = ""
                    row["phone"] = ""
                    row["initials"] = "标"
                    row["avatar"] = None
                    row["custom_hotkeys"] = {}
                changed = True

        if changed:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            response.content = body
            response["Content-Length"] = str(len(body))
        return response

    @staticmethod
    def _is_upload_write(request) -> bool:
        path = request.path or ""
        method = (request.method or "").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return False
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            return False
        for pattern, methods in WRITE_RULES:
            if method in methods and pattern.match(path):
                return True
        return False

    @staticmethod
    def _deny(request, message: str, status: int = 403):
        if (request.path or "").startswith("/api/"):
            return HttpResponse(
                json.dumps({"detail": message}, ensure_ascii=False),
                status=status, content_type="application/json; charset=utf-8",
            )
        return _html(message, status)

    def _handle_sso(self, request):
        secret = os.environ.get(SECRET_ENV, "").strip()
        if len(secret) < 32:
            logger.error("%s 未配置或过短，SSO 拒绝服务", SECRET_ENV)
            return _html("单点登录未配置，请联系系统管理员。", 503)

        ticket = request.GET.get("ticket", "")
        claims = _verify(ticket, secret) if ticket else None
        if not claims:
            return _html("登录凭证无效或已过期。", 403)
        if claims.get("purpose") != PURPOSE:
            return _html("登录凭证用途不符。", 403)
        try:
            if int(claims.get("exp", 0)) <= int(time.time()):
                return _html("登录凭证已过期。", 403)
            if int(claims.get("iat", 0)) > int(time.time()) + 30:
                return _html("登录凭证时间异常。", 403)
        except (TypeError, ValueError):
            return _html("登录凭证时间异常。", 403)

        jti = str(claims.get("jti") or "")
        if not jti or not cache.add("forge:sso:" + jti, 1, REPLAY_TTL_S):
            return _html("该登录凭证已被使用。", 403)

        next_path = _safe_next(claims.get("next"))
        try:
            user = _ensure_user(claims, next_path)
        except Exception:  # noqa: BLE001
            logger.exception("SSO 建号/挂载组织失败")
            return _html("账号开通失败，请联系系统管理员。", 500)
        if user is None:
            return _html("账号不可用，请联系系统管理员。", 403)

        # **必须走 LS 自己的 login 包装，不能直接 django.contrib.auth.login。**
        # users/functions/common.py 的 login() 会先写 request.session['last_login']，
        # 而 core/middleware.py 的 InactivitySessionTimeoutMiddleWare 在下一个请求
        # 读不到这个键时把它当 0（1970 年），判定"会话已存在 56 年"直接 logout()。
        # 症状极具迷惑性：302 + sessionid 都正常，但紧接着的每个请求都被当匿名，
        # 于是 LS 又把你送回登录页。踩过一次，别改回去。
        # 显式指定 backend：LS 的 AUTHENTICATION_BACKENDS 首个是 rules 的
        # ObjectPermissionBackend，不指定时某些版本取不到 backend。
        from users.functions.common import login as ls_login

        # ★ 同一浏览器里换账号必须先登出。
        # 实测：先在管理员会话里点开一个项目，再用另一个人的票据进来，
        # django.login() 会 flush 旧 session 并 cycle_key，但 LS 的登录包装
        # 已经把 last_login 写进了**旧** session 对象，新 cookie 落到浏览器后
        # 下一个请求被判为匿名 —— 症状是 SSO 明明 302 成功，却立刻跳
        # /user/login/?next=...。先显式 logout 清干净再登录，顺序就对了。
        from django.contrib.auth import logout as django_logout

        if request.user.is_authenticated and request.user.pk != user.pk:
            django_logout(request)
            logger.info("SSO 切换账号：已登出 %s 再登录 %s",
                        request.user.pk, user.pk)

        ls_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        # login() 可能 flush 旧 session，所以标记必须在它之后写。
        # can_import 缺失时按 False 处理（fail closed）：老版本编排层签的票据
        # 不带这个字段，宁可让标注员暂时不能上传，也不能默认放行。
        request.session[RESTRICTED_SESSION_KEY] = not bool(claims.get("can_import"))
        # 票据用途：只有平台明确签了 mode=design 才开设计会话。老票据没有
        # 这个字段时写空串，_designer() 判定为 False，按标注员的老规则走。
        request.session[MODE_SESSION_KEY] = str(claims.get("mode") or "")
        # 甲方设计票据刻意不给 can_import，所以上面 restricted=True；
        # 它靠 mode=design 分流到 _guard_designer，而不是被当成标注员。
        # 项目白名单：以票据为准。ls_projects 是签发时 RLS 下该账号全部可见
        # 项目（标注员=分配给自己的批次），LS 侧不做第二套推断。老编排层票据
        # 没有该字段时退化为「本票据项目」单元素白名单（fail closed）。
        projects = self._claim_projects(claims, next_path)
        request.session[PROJECTS_SESSION_KEY] = sorted(projects)
        if projects:
            home = claims.get("ls_project_id")
            if not (isinstance(home, int) and not isinstance(home, bool) and home in projects):
                home = max(projects)
            request.session[HOME_PROJECT_SESSION_KEY] = int(home)
        # 标注页上下文票据：编排层签的 purpose=ls-ctx。老票据没有这两个字段，
        # 写入空串即可 —— 页面照常渲染，只是没有规范侧栏与耗时打点。
        request.session[CTX_SESSION_KEY] = str(claims.get("ctx") or "")
        request.session[API_BASE_SESSION_KEY] = str(claims.get("api_base") or "")
        logger.info("SSO 登录成功 platform_sub=%s ls_user=%s next=%s restricted=%s projects=%s",
                    claims.get("sub"), user.email, next_path,
                    request.session[RESTRICTED_SESSION_KEY],
                    request.session[PROJECTS_SESSION_KEY])
        return HttpResponseRedirect(next_path)

    @staticmethod
    def _claim_projects(claims, next_path: str) -> set:
        """从票据提取项目白名单。任何非数字内容都丢掉，绝不让票据灌进脏值。"""
        projects = set()
        raw_list = claims.get("ls_projects")
        if isinstance(raw_list, list):
            for x in raw_list:
                if isinstance(x, bool):
                    continue
                if isinstance(x, int) or (isinstance(x, str) and x.isdigit()):
                    projects.add(int(x))
        single = claims.get("ls_project_id")
        if isinstance(single, bool) or not (isinstance(single, int) or
                                            (isinstance(single, str) and str(single).isdigit())):
            # 兼容老票据：从 next 路径里反解项目 id
            m = PROJECT_RE.match(next_path or "")
            single = int(m.group(1)) if m else None
        if single is not None:
            projects.add(int(single))
        return projects
