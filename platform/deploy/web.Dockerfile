# FORGE 统一前端镜像（多阶段：Node 构建 → Nginx 静态托管）
FROM node:22-alpine AS build

WORKDIR /app

# 先只拷依赖清单，最大化利用层缓存
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

ARG VITE_API_BASE=/api
ARG VITE_LS_EMBED=0
ENV VITE_API_BASE=$VITE_API_BASE
ENV VITE_LS_EMBED=$VITE_LS_EMBED

RUN npm run build


FROM nginx:1.27-alpine

# 静态站点配置：SPA 回退 + 资源长缓存（配置随前端目录走，避免跨构建上下文 COPY）
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000
HEALTHCHECK --interval=20s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
