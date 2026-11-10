# ============================================================================
# FORGE · 平台前端镜像（多阶段：Node 构建 → Nginx 静态托管）
# ----------------------------------------------------------------------------
# 构建上下文是部署包根目录（不是 src/web），所以 COPY 路径都带 src/web/ 前缀。
# 为什么不用「上下文就是 src/web」的老做法：三个镜像共用一个上下文根，
# docker-compose.yml 里 build.context 统一写 .，路径关系一眼能看懂，
# 也避免每个 Dockerfile 里出现 ../../ 这种脆弱相对路径。
# ============================================================================
FROM node:22-alpine AS build

WORKDIR /app

# 国内服务器直连 registry.npmjs.org 很慢，默认走 npmmirror；
# 海外服务器在 .env 里把 NPM_REGISTRY 改回 https://registry.npmjs.org 即可。
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "$NPM_REGISTRY"

# 先只拷依赖清单，最大化利用层缓存（源码改动不重装依赖）
COPY src/web/package.json src/web/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY src/web/ ./

# 前端运行时只认两个注入量：
#   VITE_API_BASE      同源反代下的 API 前缀（/api）
#   VITE_PLATFORM_URL  LS 里「返回主工作台」按钮的目标地址
ARG VITE_API_BASE=/api
ARG VITE_PLATFORM_URL=
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_PLATFORM_URL=$VITE_PLATFORM_URL

RUN npm run build


FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY src/web/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000

HEALTHCHECK --interval=20s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
