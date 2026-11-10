# ============================================================================
# FORGE · Label Studio 镜像 = 官方镜像 + 构建期品牌补丁 + 平台单点登录中间件
# ----------------------------------------------------------------------------
# 为什么在构建期打补丁，而不是容器启动时打：
#   启动时打补丁每次 restart 都要多花时间，而且补丁失败容器照常起来、
#   界面静默退回原版，问题要到用户打开页面才暴露。构建期打补丁，
#   补丁锚点对不上 = 镜像构建失败，问题在部署阶段就拦住。
#
# ★ 镜像名是 heartexlabs/label-studio（官方 Docker Hub 仓库名）。
#   heartex/label-studio 是错误拼写，拉不到 1.23.2 的 tag。
# ============================================================================
ARG LS_IMAGE=heartexlabs/label-studio:1.23.2
FROM ${LS_IMAGE}

USER root

# ★ 必须保持 src/labelstudio/brand 与 src/web/public/brand 的相对目录结构：
#   apply_brand.py 里品牌 PNG 按 BRAND_DIR.parents[1]/web/public/brand/…
#   相对引用（为什么共用一份 PNG：品牌标记只维护一处，避免前端与 LS 两份
#   图标日后不一致）。目录结构被压平 = 构建期直接报「找不到 PNG」失败。
COPY src/labelstudio/brand /opt/forge/src/labelstudio/brand
COPY src/web/public/brand/sy-mark-dark.png /opt/forge/src/web/public/brand/sy-mark-dark.png

# apply_brand.py 一次做两件事（都是幂等、可重放的）：
#   1. 打品牌补丁（登录页字标/标题/favicon/加载态/中文层）
#   2. 装 SSO 中间件 core/forge_sso.py 并挂进 MIDDLEWARE
# 退出码 2 = 有补丁锚点没匹配上（LS 版本变了），必须让构建失败而不是带病上线。
RUN set -e; \
    PY="$(command -v python3 || command -v python)"; \
    LS_ROOT="$("$PY" -c 'import label_studio, pathlib; print(pathlib.Path(label_studio.__file__).resolve().parent)')"; \
    echo "[brand] label_studio package: $LS_ROOT"; \
    "$PY" /opt/forge/src/labelstudio/brand/apply_brand.py --ls-root "$LS_ROOT"; \
    rm -rf /opt/forge

# 不改 USER：官方镜像的 entrypoint 需要跑 migrate / collectstatic，
# 沿用镜像自带的运行身份，避免数据目录 /label-studio/data 权限不一致。
