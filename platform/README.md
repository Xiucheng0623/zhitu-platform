# FORGE · Enterprise AI Data Operations Platform

企业级 AI 数据生产与供应链管理平台。

**底座构成（2026-11 架构调整后）**：Label Studio CE 1.23 作标注引擎 + 自研业务
中间层（orchestrator，FastAPI + PostgreSQL + RLS）+ 统一前端（React 18）。
原方案的 ERPNext 已移除——供应商、采购、结算、开票、付款改由自有表承载，
决策与代价见 `docs/02 §9`（ADR-001）。

功能矩阵对标海天瑞声（Speechocean）这类专业数据服务平台，而非 ERP 系统。

## 仓库结构

```
platform/
├── docs/                      # 01 源码审计 · 02 架构(含 ADR-001) · 03 API 契约
│                              # 04 品牌 · 05 Label Studio · 06 财务/采集运行手册
├── brand/                     # 定稿标识 + 品牌规范（brand-sheet.html）
├── web/                       # 统一前端（React 18 + TS + Vite，零 UI 库依赖）
│   ├── nginx.conf             # 生产静态托管配置
│   ├── .env.local             # 本地联调：指向真实 orchestrator 与 LS
│   └── src/
│       ├── design/            # Design System：tokens / components / charts / icons
│       ├── shell/             # 应用骨架：侧边导航 · 顶栏 · 命令面板
│       ├── pages/             # 14 个页面（含 Login / ChangePassword）
│       ├── lib/               # auth（会话+守卫）· http（真实后端）· store（真库状态+写操作）
│       │                      # adapters（后端行 → 前端类型的映射层）· dataMode（数据来源标记）
│       │                      # hooks（视角收敛）· rbac · format · analytics · sso
│       └── data/              # 确定性种子数据（只在后端不可达时作为降级显示）
├── labelstudio/               # 标注引擎侧资产
│   ├── configs/*.xml          # 5 个 labeling config 模板（LS 官方校验通过）
│   ├── provision.py           # 幂等创建项目 + 注册 webhook（真实可跑）
│   └── brand/                 # UI 品牌化：可重放补丁 + 自检（见 docs/05 §8）
│       ├── apply_brand.py     # 幂等补丁器：--check / --revert，升级 LS 后重跑
│       ├── verify_brand.py    # 带登录态的无头浏览器断言 + 截图（标准库 CDP）
│       ├── scan_zh_coverage.py# 遍历 8 个页面，列出仍是英文的 UI 文案
│       ├── build_zh_dict.py   # 合并两路词库 → 生成 zh-cn.js
│       ├── zh-cn.template.js  # 中文层执行器（安全边界在这里）
│       ├── zh-cn-upstream.json# 社区补丁词库 897 条（出处见 _meta）
│       ├── zh-cn-overrides.json# 本项目词条 177 条，覆盖优先级更高
│       ├── forge-wordmark.svg · forge-mark.svg · login-ornament.svg
│       ├── login.css.fragment · palette.css.fragment · runtime.css.fragment
│       └── ls-runtime-brand.js
├── services/orchestrator/     # 自研中间层（FastAPI + PostgreSQL + RLS）
│   ├── app/                   # config · security · fsm · db · repo · clients · storage
│   │                          # auth（手机号注册/登录/改密 + 管理员开通 + SSO）
│   │                          # sso（OIDC Relying Party：PKCE / 验签 / JIT / 绑定申请）
│   │                          # finance（发票/付款/三单匹配）· collection（采集）
│   │                          # ops（审核/绩效/资产）· netutil · serve · looputil
│   ├── tools/                 # bootstrap_admin（首个管理员开通）
│   ├── tests/smoke_http.py    # 39 项 HTTP 契约/隔离测试
│   ├── tests/e2e_labelstudio.py # LS 端到端：标注 → webhook → 状态推进（9 项）
│   ├── tests/e2e_finance.py   # 财务+采集端到端：58 项断言
│   ├── tests/e2e_sso.py       # 企业统一登录端到端（自带假 IdP，23 项断言）
│   ├── tests/check_rbac_parity.py # 前后端规则表一致性（10 角色 × 29 动作）
│   └── Dockerfile
└── deploy/                    # compose(9 服务) · Caddy · 镜像 · .env.example
    ├── windows/               # Windows 服务器部署（install.ps1 + README）
    └── migrations/            # 000 角色 · 001 结构 · 002 RLS · 003 种子
                               # 004 RLS 验收 · 005 财务 · 006 采集 · 007 RLS 扩展
                               # 008 财务验收 · 009 历史凭证回填
                               # 010 身份（users / invites + 列级授权 + 定义者函数）
                               # 011 SSO（identities / identity_link_requests）
```

## 本地运行

前端（无 Docker 也可）：

```bash
cd web
npm install --registry=https://registry.npmmirror.com
npm run dev            # http://127.0.0.1:9120
npm run typecheck      # tsc --noEmit，零错误
npm run build          # dist/，任意静态服务器可用
```

中间层（需要本机 PostgreSQL 17）：

```bash
cd services/orchestrator
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 首次：建库建角色后跑迁移
psql -U postgres -c "create role forge_app login password 'forge_dev_2026' nosuperuser nobypassrls"
psql -U postgres -c "create database forge owner postgres"
for f in 000_roles 001_schema 002_rls 003_seed_verify \
         005_finance 006_collection 007_rls_ext 009_backfill_finance; do
  psql -U postgres -d forge -v ON_ERROR_STOP=1 -v app_password=forge_dev_2026 \
       -f deploy/migrations/$f.sql
done

# 启动（Windows 必须走 app.serve，原因见下方「已知平台差异」）
# native 模式：手机号 + 密码登录，JWT 由本服务自签
export FORGE_AUTH_MODE=native
export FORGE_JWT_SECRET=$(python -c "import secrets;print(secrets.token_hex(48))")
python -m app.serve --port 8300

# 首次：开一个平台管理员（密码只打印一次）
python -m tools.bootstrap_admin --phone 13800138000 --name "张伟" \
       --roles platform_admin,ops_director
```

跑契约测试（服务已启动的前提下）：

```bash
python tests/smoke_http.py        # 39 项：隔离 / 状态机 / 幂等 / 审计（需 dev 模式后端）
python tests/e2e_finance.py       # 58 项：发票核验 → 付款到账 → 冲销，采集全链路
python tests/e2e_sso.py --phone 13900139000 --password '<该账号密码>'
                                  # 23 项：自带假 IdP，含"claim 里的 roles 不提权"
python tests/check_rbac_parity.py # 前后端 RBAC 规则表逐条比对（不需要起服务）
python tests/diagnose_stability.py --admin-password '<管理员密码>'
                                  # 35 项：注册→激活→改密→锁定→解锁→并发→作业接口
psql -U forge_app -d forge -f deploy/migrations/008_finance_verify.sql   # 库层 13 场景
```

> `smoke_http.py` 用 `"ops_director"` 这类裸字符串当 token，只在
> `FORGE_AUTH_MODE=dev` 下成立。native 模式下它会全红，那不是回归。
> 跑法：另起一个 `FORGE_AUTH_MODE=dev FORGE_PORT=8310` 的后端，
> 再 `FORGE_BASE=http://127.0.0.1:8310 python tests/smoke_http.py`。

前端浏览器验证（真 Edge/Chrome，走 CDP，不依赖 Playwright）：

```bash
cd web
python tools/verify_auth_ui.py --phone 13800138000 --password '<密码>'   # 手机号登录全流程
python tools/verify_auth_ui.py --anon-only                              # 只验未登录拦截
python tools/verify_wired_ui.py --phone 13800138000 --password '<密码>'  # 26 项：界面数字 vs 接口真实条数
python tools/verify_wired_ui.py --read-only                            # 只比对，不做新建项目写操作
# SSO 全链路（自起假 IdP + 后端 + 静态站，需先按脚本头部说明构建 dist-sso）
python tools/verify_sso_ui.py
```

`verify_wired_ui.py` 是本项目最该先跑的一条：它先用 API 拿到每个资源的**真实条数**
作为基准，再去浏览器里读同一页面的渲染结果逐条比对。前端接真库最危险的失败不是
报错，而是「看起来没事」—— store 还在读 seed 时页面数字一样漂亮，字段名对不上时
金额显示 ¥0 也不报错。这类问题只有比对才能发现。

> 截图默认不开（`--shots` 打开）。无头 Chrome 连续运行数十秒后偶尔不再响应 CDP，
> 一旦卡住会把整轮验证拖到超时，而那时断言早就跑完了 —— 图片是留证，不该当断言。

容器化部署（目标服务器，本机未装 Docker 未实测）：

```bash
cd deploy && cp .env.example .env   # 填真实密码与域名
docker compose up -d                # migrate 自动建角色 → 迁移 → 灌验证种子
```

## 健康检查怎么用（别配错端点）

| 端点 | 耗时 | 用在哪 |
|---|---|---|
| `GET /healthz` | ~10 ms | **监控/LB 探活配这个**，只查自身与数据库 |
| `GET /healthz?deep=1` | 首次 ≤1.2 s，15 s 内命中缓存 | 人工排查外部系统（Label Studio / 对象存储） |

`deep=1` 会真的去连一次 Label Studio，所以给了 **1 秒硬预算 + 15 秒缓存**：
外部系统挂掉时，健康接口本身不能变成故障源（这一条是踩出来的 ——
LS 未启动时它曾经要 2.1 秒，监控每秒打一次就等于自己 DoS 自己）。

## 当前完成度（如实）

| 阶段 | 状态 |
|---|---|
| 1 源码审计 | ✅ docs/01（版本、许可证、GPL 合规决策、复用清单，联网核实） |
| 2 基础环境 | ✅ 数据库层实测（15 表 + 全表 force RLS + 9 场景隔离测试通过）<br>✅ 中间层服务化（真实 SQL / OIDC 校验 / 幂等 / 审计 / S3 预签名）<br>✅ 编排与镜像定义完成，**本机无 Docker，容器未实测** |
| 3 UI 重构 | ✅ Design System + 13 页全部可交互，逐页无头截图核验 |
| 4 业务开发 | ✅ 项目/供应商/任务/审核/结算/绩效/资产/审计 的读写接口全部落地，前端逐页接真库<br>◐ 交付记录、Frappe Custom App 待做 |
| 5 测试 | ✅ orchestrator 39 项契约 + 58 项财务 e2e + 23 项 SSO + 35 项稳定性<br>✅ 前端 26 项「界面数字 vs 接口条数」比对（`verify_wired_ui.py`） |
| 6 身份（手机号） | ✅ 010 迁移 + auth 模块 + 前端会话/守卫，22 项 curl 实测通过 |
| 6.5 企业统一登录（SSO） | ✅ 011 迁移 + OIDC RP + 前端入口/回调页<br>✅ 后端 23 项（自带假 IdP）+ 浏览器 8 项全通过<br>◐ 已按需求**搁置**，待对方提供 issuer / client_id / client_secret 并登记回调地址 |
| 6.6 平台 → LS 单点登录 | ✅ 一次性票据 SSO（平台手机号 JWT → LS Django session），员工不再二次登录；LS 自助注册与注册页原版内容已清除<br>✅ `verify_ls_sso_ui.py` 真实浏览器 7/7：iframe 直接落在 `/projects/{id}/data`，无登录页 |
| 7 投产 | ◐ Windows 部署套件（计划任务 + Caddy 模板）已交付<br>◐ 待办：MinIO（对象存储仍 503）、`POST /api/v1/tasks` 建批次、LS 换 PG、改 `postgres` 超级用户密码<br>◐ 本机 LS 仍是 Django dev server + SQLite，进程靠会话内手工启动 |

### 前端与后端的关系（重要，别误解）

**现在的状态：所有业务页面都读真实 PostgreSQL，写操作也真的落库。**
`web/src/data/seed.ts` 不再参与正常渲染，只在后端不可达时作为降级显示，
此时顶栏会挂出红色/橙色「演示数据」标识。

数据流一句话说清：

```
页面 → useScoped()（按视角收敛） → store.tsx
       ├─ 挂载时并发拉 10 个 GET，经 lib/adapters.ts 映射成前端类型
       ├─ 写操作 POST 到后端，成功后用服务端结果刷新（服务端为准）
       └─ 失败/无权限 → 该模块记一条 partialError，顶栏显示「部分模块未加载」
```

几条设计上的取舍，改代码前先看：

1. **不做 localStorage 业务缓存**。旧实现把整个 StoreState 序列化进浏览器，
   接真库后这就变成了「上次打开时的快照」——任务已改派、结算已付款，界面还停在
   几天前，而且毫无提示。现在只缓存 token。
2. **字段翻译集中在 `lib/adapters.ts`**，只此一处。后端返回 snake_case 是故意的
   （接口字段与表列一一可对，排查不用在脑子里做名字翻译），前端要 camelCase 因为
   要在 JSX 里当属性访问。两边都不该为对方让步。
3. **后端没返回的字段宁可为空，不许编**。例如 `v_project_progress` 不带
   `manager_name`、`forge.users` 不带部门/在制项目数 —— 页面就显示「—」或干脆
   不显示那一列，而不是填一个恒为 0 的假数字。
4. **权限差异不该整体降级**。财务角色没有 `member:read`，人员页拿不到内部账号，
   但这不影响项目、任务、结算照常显示。所以 10 个 GET 用 `Promise.allSettled`
   各自成败，只有全挂才回落到 seed。

### 后端接口一览（前端已用到）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/projects` | project:read | 视图 + 负责人 + 承接供应商数组 |
| POST | `/api/v1/projects` | project:write | 编号由服务端生成，撞号返回 409 |
| GET | `/api/v1/suppliers` | supplier:read | 含 modalities / 联系方式 / 资质 |
| POST | `/api/v1/suppliers` | supplier:write | 新建默认 qualifying |
| POST | `/api/v1/suppliers/{id}/status` | supplier:qualify | 状态机校验 |
| GET | `/api/v1/supplier-members` | member:read | 标注员名录（含所属供应商名） |
| GET | `/api/v1/tasks` | task:read | 已 join `project_name` / `assignee_name` / `supplier_name` |
| POST | `/api/v1/tasks/{id}/assign` | task:assign | 校验成员归属 + 状态机 |
| POST | `/api/v1/tasks/{id}/submit` | task:submit | |
| POST | `/api/v1/tasks/{id}/review` | review:decide | 写 reviews 并推进状态 |
| GET | `/api/v1/reviews` | review:read | 含 project_name / reviewer_name |
| GET | `/api/v1/performance` | performance:read | 快照表；`/performance/live` 是现算 |
| GET | `/api/v1/assets` · POST `/assets/import` | asset:read / import | 入湖登记幂等 |
| GET | `/api/v1/settlements` | settlement:read | 含 supplier_name / 单据号 |
| POST | `/api/v1/settlements/{id}/advance` | settlement:read | 状态机推进 |
| GET | `/api/v1/admin/users` | member:read | 内部账号（手机号已脱敏，返回 `{items:[]}` 而非 `{rows:[]}`） |

**仍然没有后端实现的**：新增/修改供应商成员（People 页会明确报错，不会假成功）、
交付记录、ERPNext 相关（已移除）。

## 已知平台差异（踩过的坑，别重复踩）

1. **Windows 上必须 `python -m app.serve` 启动**。uvicorn 0.54 的
   `asyncio_loop_factory` 在 Windows 上硬编码返回 `ProactorEventLoop`，无视事件循环
   策略；而 psycopg3 异步连接只支持 `SelectorEventLoop`。直接用
   `uvicorn app.main:app` 会导致连接池无限重连失败，日志报
   `Psycopg cannot use the 'ProactorEventLoop'`。`app/serve.py` 显式建 Selector 循环
   并跑 `server.serve()`。Linux/容器内行为不变。
2. **DB URL 与迁移必须同源**：`forge_app` 不是表 owner 且 `nobypassrls`，
   迁移必须由库 owner（compose 里的 `forge_admin`）执行；应用连接用 `forge_app`。
   让应用用 owner 连接会**静默让整套 RLS 失效**（owner 默认不受策略约束）。
3. **`force row level security` 是必须的**：只 `enable` 时表 owner 仍可绕过。
   002/007 里对全部 22 张业务表 `enable + force`，健康检查会核对这个数字。
4. **for-all 策略不要手写 `with check`**：PG 会自动复用 `using` 表达式做插入校验；
   手写一份只含部分条件的 `check` 会让写越权静默成功（004 测试曾因此假阳性）。
5. **boto3 默认超时会让探活变成故障**：默认 `connect_timeout=60` 且重试 3 次，
   MinIO 未起时一次 `head_bucket` 卡 20 秒，`/integrations/health` 直接超时。
   `storage.client()` 已收紧为 2s/5s + 重试 1 次，`health()` 再套 3s 硬上限。
6. **本机若设置了 `HTTP_PROXY`，测试与客户端必须 `trust_env=False`**。
   否则发往 `127.0.0.1` 的请求会被交给代理，症状是随机 `ReadTimeout`，
   而不是「连不上」——从报错完全看不出根因。

## 关键设计约束（不可妥协）

1. 不直连 Label Studio 数据库，只走官方 API（1.23 起为 JWT，access 5 分钟需自动续）；
2. 财务以本平台自有表为准：`invoices` / `payments` / `payment_allocations`，
   不再向外部 ERP 单向推送（ADR-001）；
3. 标注组件全部复用 LS 原生能力，前端只换外壳；
4. 会计域内控写进数据库而非 UI：价税自洽、防重票、防超付、防重复流水号，
   结算单 `paid` 由核销金额推导，不允许人工改状态；
5. 越权访问一律 404；四道隔离边界见 docs/02 §5，RLS 覆盖全部 22 张业务表。
