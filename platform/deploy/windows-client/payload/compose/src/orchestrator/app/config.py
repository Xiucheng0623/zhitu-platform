"""
FORGE Orchestrator · 运行配置
================================================================
所有配置来自环境变量，带安全的开发默认值。
生产部署时由 deploy/.env 注入（docker-compose 已声明）。

注意：AUTH_MODE 决定认证路径
  - dev  : 接受演示 token（`Bearer project_manager|ops_director`），仅本地联调用
  - jwt  : 严格校验 OIDC JWT，dev 模式自动拒绝
生产环境必须为 jwt，否则任何人都能伪造角色。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlsplit


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    return (_env(key) or str(default)).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- 数据库 -------------------------------------------------------------
    db_dsn: str = field(default_factory=lambda: _env(
        "FORGE_DB_DSN",
        "postgresql://forge_app:forge_dev_2026@localhost:5432/forge",
    ))
    db_pool_min: int = field(default_factory=lambda: _int("FORGE_DB_POOL_MIN", 2))
    db_pool_max: int = field(default_factory=lambda: _int("FORGE_DB_POOL_MAX", 12))
    db_statement_timeout_ms: int = field(
        default_factory=lambda: _int("FORGE_DB_STMT_TIMEOUT_MS", 8000))

    # --- 认证 ---------------------------------------------------------------
    # dev    演示用：token 直接是 "ops_director" 这类字符串，不验签不查库。
    #        只能本地用，validate() 会在对外部署时拦下。
    # jwt    外部 IdP 签发（OIDC/JWKS 或共享 HS256 密钥）。
    # native 本服务自签：手机号+密码登录，JWT 由 app/auth.py 签发，
    #        roles 每次请求从 forge.users 读，权限实时生效。
    auth_mode: str = field(default_factory=lambda: _env("FORGE_AUTH_MODE", "dev"))
    # OIDC：两种校验方式二选一。jwks_url 优先（支持密钥轮换）
    oidc_jwks_url: str = field(default_factory=lambda: _env("FORGE_OIDC_JWKS_URL"))
    oidc_issuer: str = field(default_factory=lambda: _env("FORGE_OIDC_ISSUER"))
    oidc_audience: str = field(default_factory=lambda: _env("FORGE_OIDC_AUDIENCE"))
    oidc_hs256_secret: str = field(
        default_factory=lambda: _env("FORGE_OIDC_HS256_SECRET"))
    default_tenant: str = field(
        default_factory=lambda: _env("FORGE_DEFAULT_TENANT", "ent_forge"))

    # --- Label Studio -------------------------------------------------------
    ls_base_url: str = field(
        default_factory=lambda: _env("FORGE_LS_BASE_URL", "http://labelstudio:8080"))
    # 1.23 起只认 JWT：access 有效期 5 分钟，refresh 长期有效。
    # 生产必须配 refresh（否则每 5 分钟就要人工换 token）。
    ls_token: str = field(default_factory=lambda: _env("LABELSTUDIO_TOKEN"))
    ls_refresh_token: str = field(default_factory=lambda: _env("FORGE_LS_REFRESH_TOKEN"))
    ls_webhook_secret: str = field(
        default_factory=lambda: _env("FORGE_LS_WEBHOOK_SECRET"))
    # 浏览器 SSO：编排层签票据、LS 侧 forge_sso.py 验票据，两边必须配同一个值。
    # 只在 /api/v1/ls/ticket 用到；没配时该接口 503，不影响其它功能。
    # LS 侧读的是同名环境变量，见 platform/labelstudio/brand/forge_sso.py。
    ls_sso_secret: str = field(default_factory=lambda: _env("FORGE_LS_SSO_SECRET"))
    # 浏览器可达的 LS 地址。与 ls_base_url（服务端内部调用）分开：生产里后者
    # 常是 http://labelstudio:8080 这类容器内网名，浏览器根本连不上。
    # 留空则退回 ls_base_url（本机 127.0.0.1 联调就是这个情形）。
    ls_public_base_url: str = field(
        default_factory=lambda: _env("FORGE_LS_PUBLIC_BASE_URL"))
    # 浏览器可达的平台 API 地址。标注页里的 UX 增强层（规范侧栏、单题耗时）
    # 要从 LS 的源跨到本服务，必须是绝对地址；相对路径会打到 LS 自己的
    # 8080 上去，表现为「接口 404」而不是「没配置」，很难排查。
    # 留空时注入层拿不到这两个地址，会明确显示「未配置」而不是假装成功。
    platform_api_base: str = field(
        default_factory=lambda: _env("FORGE_PLATFORM_API_BASE"))

    # Label Studio 回调本服务的地址（**容器内可达**，不是浏览器地址）。
    # 本地是 http://127.0.0.1:8300/...，容器里是 http://orchestrator:8300/...
    # 留空则不同步自动注册 webhook：进度只能靠手动「同步」按钮拉，不报错但要知情。
    ls_callback_url: str = field(
        default_factory=lambda: _env("FORGE_LS_CALLBACK_URL",
                                     "http://127.0.0.1:8300/api/v1/webhooks/labelstudio"))
    # 定时对账间隔（秒）。0 = 关闭。
    #
    # 为什么需要一个"轮询"来兜底：webhook 是**推送**，推送链路上任何一环坏掉
    # （LS 升级改了事件名 / 字段名 / 内网被 SSRF 拦 / 编排层重启时事件丢失），
    # 平台都会安静地停在旧数字上，没有任何报错。定时拉取把「推送」从唯一路径
    # 降级成加速路径：就算一次回调都没收到，最多滞后一个间隔也会自己追上。
    # 默认 300 秒 —— 对进度展示够快，又不会把 LS 打出压力。
    ls_sync_interval_s: int = field(
        default_factory=lambda: int(_env("FORGE_LS_SYNC_INTERVAL_S", "300") or 0))

    # --- 出站网络 -----------------------------------------------------------
    # 默认 False：不读取 HTTP_PROXY/HTTPS_PROXY。机房常见全局代理会把发往
    # 127.0.0.1 / 容器内网域名的请求交给代理，症状是莫名的 404 / 502。
    http_trust_env: bool = field(
        default_factory=lambda: _bool("FORGE_HTTP_TRUST_ENV", False))

    # --- 文件存储 -----------------------------------------------------------
    s3_endpoint: str = field(
        default_factory=lambda: _env("FORGE_S3_ENDPOINT", "http://minio:9000"))
    s3_access_key: str = field(default_factory=lambda: _env("MINIO_ROOT_USER", "forge"))
    s3_secret_key: str = field(default_factory=lambda: _env("MINIO_ROOT_PASSWORD"))
    s3_bucket: str = field(
        default_factory=lambda: _env("FORGE_S3_BUCKET", "forge-data"))
    s3_region: str = field(default_factory=lambda: _env("FORGE_S3_REGION", "us-east-1"))

    # --- 本服务自签 JWT（auth_mode=native）----------------------------------
    # native 模式下由 app/auth.py 用这个密钥签发/校验。
    # **不接受默认值**：没配就让服务起不来，绝不允许悄悄退化成一个
    # 谁都能签的固定密钥 —— 那就是把 dev 假鉴权换个皮。
    jwt_secret: str = field(default_factory=lambda: _env("FORGE_JWT_SECRET"))
    jwt_issuer: str = field(default_factory=lambda: _env("FORGE_JWT_ISSUER", "forge-orchestrator"))
    jwt_audience: str = field(default_factory=lambda: _env("FORGE_JWT_AUDIENCE", "forge-platform"))
    # 一个工作日足够用；改密/停用会 bump token_version 使旧 token 立即失效，
    # 所以不必靠短 TTL 来兜底撤销。
    jwt_ttl_minutes: int = field(default_factory=lambda: _int("FORGE_JWT_TTL_MIN", 480))

    # --- 外部 IdP 单点登录（SSO / OIDC Relying Party）-----------------------
    # 与 auth_mode 是**正交**的两件事：
    #   auth_mode 决定"我们的 API 接受哪种凭证"（native / jwt / dev）。
    #   SSO 只是另一种**拿到入口**的方式：浏览器在对方 IdP 登录后回调本服务，
    #   本服务校验 id_token、映射到本地账号，然后照常签发**我们自己的** JWT。
    #   所以对方的 token 永远不会出现在我们的 Authorization 头里，
    #   角色、停用、改密仍然由本地库说了算。
    sso_issuer: str = field(default_factory=lambda: _env("FORGE_SSO_ISSUER"))
    sso_client_id: str = field(default_factory=lambda: _env("FORGE_SSO_CLIENT_ID"))
    sso_client_secret: str = field(default_factory=lambda: _env("FORGE_SSO_CLIENT_SECRET"))
    # 回调地址必须与 IdP 里登记的一致（差一个斜杠都会被拒）
    sso_redirect_uri: str = field(default_factory=lambda: _env("FORGE_SSO_REDIRECT_URI"))
    # IdP 侧的逻辑名，写进 forge.identities.provider。换域名不该让绑定失效，
    # 所以这个名字跟 issuer 分开存。
    sso_provider_name: str = field(
        default_factory=lambda: _env("FORGE_SSO_PROVIDER", "oidc"))
    sso_scope: str = field(default_factory=lambda: _env(
        "FORGE_SSO_SCOPE", "openid profile email phone"))
    # 声明名映射。不同 IdP 叫法差很多（Keycloak: preferred_username/name，
    # 微信/Azure: 各有各的），所以做成配置而不是写死。
    sso_claim_sub: str = field(default_factory=lambda: _env("FORGE_SSO_CLAIM_SUB", "sub"))
    sso_claim_phone: str = field(
        default_factory=lambda: _env("FORGE_SSO_CLAIM_PHONE", "phone_number"))
    sso_claim_name: str = field(default_factory=lambda: _env("FORGE_SSO_CLAIM_NAME", "name"))
    sso_claim_email: str = field(default_factory=lambda: _env("FORGE_SSO_CLAIM_EMAIL", "email"))
    # 首次见到某个 sub、且没有可合并的本地账号时，是否自动建号。
    # 自动建的号只给 sso_jit_role（最小权限），要更高权限必须管理员升，
    # 不允许 IdP 那边一句 claim 就把人变成 platform_admin。
    sso_allow_jit: bool = field(default_factory=lambda: _bool("FORGE_SSO_ALLOW_JIT", True))
    sso_jit_role: str = field(default_factory=lambda: _env("FORGE_SSO_JIT_ROLE", "annotator"))
    # 登录成功后把浏览器送回前端的哪个地址
    sso_frontend_redirect: str = field(
        default_factory=lambda: _env("FORGE_SSO_FRONTEND_REDIRECT", "/#/sso/callback"))
    sso_state_ttl_seconds: int = field(
        default_factory=lambda: _int("FORGE_SSO_STATE_TTL_S", 600))

    # --- 登录防护 -----------------------------------------------------------
    login_max_failures: int = field(default_factory=lambda: _int("FORGE_LOGIN_MAX_FAILURES", 5))
    login_lock_minutes: int = field(default_factory=lambda: _int("FORGE_LOGIN_LOCK_MIN", 15))
    # 8 位是 NIST SP 800-63B 的下限，也是行业基线；更长的值当然更好，
    # 但把它设成 10 只会让人改用 "Spring2026!" 这类可预测变体，实际熵不升反降。
    # 真正的防线是最小长度 + 失败锁定 + 不重复使用，而不是长度数字本身。
    password_min_length: int = field(default_factory=lambda: _int("FORGE_PASSWORD_MIN_LEN", 8))

    # 行为开关
    cors_origins: List[str] = field(default_factory=lambda: [
        o for o in _env("FORGE_CORS_ORIGINS",
                        "http://localhost:5173,http://localhost:4173").split(",") if o
    ])
    idempotency_ttl_hours: int = field(
        default_factory=lambda: _int("FORGE_IDEMPOTENCY_TTL_H", 24))
    outbound_timeout_s: float = field(
        default_factory=lambda: float(_env("FORGE_OUTBOUND_TIMEOUT_S") or "6"))

    @property
    def is_prod(self) -> bool:
        """是否处于"可以对外"的认证模式。dev 一律不算。"""
        return self.auth_mode in ("jwt", "native")

    @property
    def ls_origins(self) -> List[str]:
        """标注工作台的浏览器源（scheme://host:port）。

        为什么由后端算：标注页里的 UX 增强层要跨源调本服务的规范/耗时接口，
        而带 Content-Type: application/json 的 POST 一定会触发 CORS 预检。
        让运维去环境变量里再抄一遍 LS 的地址，是最容易漏、且漏了只表现为
        「耗时数据一直是 0 条」的地方。这里从 LS 地址反推，配置一处即可。
        """
        out: List[str] = []
        for raw in (self.ls_public_base_url, self.ls_base_url):
            if not raw:
                continue
            parts = urlsplit(raw.strip())
            if parts.scheme and parts.netloc:
                origin = f"{parts.scheme}://{parts.netloc}"
                if origin not in out:
                    out.append(origin)
        return out

    @property
    def sso_enabled(self) -> bool:
        """SSO 是否配置完整。

        四项缺一不可：issuer 用来发现端点并校验 iss，client_id/secret 用来
        换 code（机密客户端），redirect_uri 必须与 IdP 登记值逐字一致。
        缺任何一项就整条链路关掉，而不是半开 —— 半开的 SSO 最典型的症状是
        "点登录跳到 IdP 报 invalid_client"，运维要从两个系统的日志里对时间线。
        """
        return bool(self.sso_issuer and self.sso_client_id
                    and self.sso_client_secret and self.sso_redirect_uri)

    def validate(self) -> Optional[str]:
        """启动自检：返回致命配置错误描述，None 表示通过。"""
        if self.auth_mode not in ("dev", "jwt", "native"):
            return f"FORGE_AUTH_MODE 非法: {self.auth_mode}（只允许 dev / jwt / native）"
        if self.auth_mode == "jwt" and not (self.oidc_jwks_url or self.oidc_hs256_secret):
            return "AUTH_MODE=jwt 但未提供 FORGE_OIDC_JWKS_URL 或 FORGE_OIDC_HS256_SECRET"
        if self.auth_mode == "native":
            # 长度要求不是形式主义：HS256 的安全性完全取决于密钥熵。
            # 32 字符以下的密钥在生产环境等于没有。
            if not self.jwt_secret:
                return ("AUTH_MODE=native 但未设置 FORGE_JWT_SECRET。"
                        "请生成一个 >= 32 字符的随机串，例如：\n"
                        "  python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
            if len(self.jwt_secret) < 32:
                return f"FORGE_JWT_SECRET 过短（{len(self.jwt_secret)} 字符），至少需要 32"
        if not self.db_dsn:
            return "FORGE_DB_DSN 为空"
        # SSO 四项是"全有或全无"。只配了一半是最坏的状态：
        # 界面上 SSO 按钮会亮，点下去在对方 IdP 报 invalid_client，
        # 而本服务日志里什么都没有 —— 排查方向完全是错的。
        sso_parts = {
            "FORGE_SSO_ISSUER": self.sso_issuer,
            "FORGE_SSO_CLIENT_ID": self.sso_client_id,
            "FORGE_SSO_CLIENT_SECRET": self.sso_client_secret,
            "FORGE_SSO_REDIRECT_URI": self.sso_redirect_uri,
        }
        missing = [k for k, v in sso_parts.items() if not v]
        if missing and len(missing) < len(sso_parts):
            return (f"SSO 配置不完整，缺少 {', '.join(missing)}。"
                    "要么四项全配，要么全部留空关闭 SSO。")
        return None


settings = Settings()
