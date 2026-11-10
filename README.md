# 智途 · 数据标注协作平台

面向数据标注外包协作的一体化平台：项目管理、人员与供应商、任务分配、标注作业、
审核、结算，标注作业面直接复用 Label Studio 原生界面（不自研标注器）。

交付形态是**一个 Windows 完整离线包**：拷到服务器，解压，双击 exe，全程本机完成，
不需要联网、不需要 Docker、不需要预先安装 Python / PostgreSQL。

## 目录

```
platform/
  services/orchestrator/    编排层（FastAPI + psycopg3）：认证、RLS、任务、结算、LS 桥接
  web/                      前端（React 18 + Vite + TypeScript）
  labelstudio/brand/        Label Studio 品牌补丁 + SSO 中间件
  deploy/
    migrations/             数据库迁移（12 条，幂等；verification/ 是验收夹具，不进交付）
    windows-native/         ★ 交付主线：13 步安装器 + 打包链
    windows-client/         早期单文件 exe 形态（已被 windows-native 取代）
    windows-docker/         容器路线（需 WSL2 + 嵌套虚拟化，云主机常不可用，备选）
  tools/                    源码树自检（check_package.py）
  docs/                     架构、API 契约、财务口径、上线就绪报告
```

## 本地跑起来

```bash
# 1. 数据库（本机 PostgreSQL 17，库名 forge，先跑迁移）
psql -d forge -f platform/deploy/migrations/001_schema.sql   # 其余迁移按序执行

# 2. 编排层（必须在 services/orchestrator 目录下跑）
cd platform/services/orchestrator
FORGE_DB_DSN="postgresql://<user>:<pwd>@127.0.0.1:5432/forge" \
FORGE_AUTH_MODE=native \
FORGE_JWT_SECRET="<32 字符以上随机串>" \
python -m app.serve --port 8300

# 3. 前端
cd platform/web && npm install && npm run dev    # http://127.0.0.1:9120
```

管理员账号不在 SQL 里建（密码哈希走应用层 scrypt）：

```bash
python -m app.cli create-admin --phone 138xxxxxxxx --password '........'
```

## 出交付包（Windows）

```bash
cd platform/web && npm run build                    # 前端产物 → web/dist
cd ../deploy/windows-native
python make_payload.py                              # 收拢源码/迁移/品牌 → payload/
python verify_payload.py                            # 静态自检，期望 60/60
python build_native.py --clean                      # 出两个 exe
python make_offline_release.py                      # 完整离线包（约 6 分钟）
```

`make_offline_release.py` 的暂存目录写死在 `D:/zhitu-offline-build`（pgsql / py-ls /
py-orch / caddy，约 882 MB），换机器先改脚本里的 `OFFLINE` 常量。

## 几条不能改的约束

- **不注册任何 Windows 服务 / 计划任务 / 开机自启**：PostgreSQL 用 `pg_ctl` 直起，
  三个进程 `spawn_detached` 常驻，保活与每日备份交给 `--watch` 守护进程。
- **前端生产构建必须留空 `VITE_API_BASE`**（见 `web/.env.production`）：走同源相对路径
  `/api`，由 Caddy 反代到 8300。写死 `127.0.0.1:8300` 会让别人打开页面时请求自己的本机。
- **平台不存任何一行标注任务数据**：`/api/v1/me/assignment/items` 每次实时代理
  Label Studio 的 `/api/tasks`，单次上限 2000 行。
- **迁移里不允许出现演示数据**：`003_bootstrap.sql` 只建企业主体，验收夹具放
  `migrations/verification/`，不随交付包发布。
- **行尾不做转换**（见 `.gitattributes`）：多处自检是逐字节比较，CRLF 会让新克隆的仓库假失败。

## 换一台机器继续开发

见 [`MIGRATION.md`](MIGRATION.md)：clone 代码、环境要求、出包流程、离线仓库包怎么用。
项目记忆随仓库走（`.workbuddy/memory/`），在新机器上新建任务会自动带上历史约定。
