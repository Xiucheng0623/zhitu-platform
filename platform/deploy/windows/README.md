# Windows 服务器部署

面向 **Windows Server 2019/2022 + 单机 PostgreSQL** 的落地步骤。
容器方案（`deploy/docker-compose.yml`）在 Windows 上跑不了 —— 它依赖 Linux 基础镜像，
Windows 容器运行时无法执行；所以 Windows 走裸机 + 计划任务 + 反代这条路。

---

## 0. 为什么不用 Windows 服务（sc.exe）

`sc.exe create` 配一个 `python.exe` 是**不可行**的：Windows 服务进程必须在 30 秒内调用
`StartServiceCtrlDispatcher`，否则 SCM 直接以 `error 1053` 杀掉它。要绕过就得引入
服务壳（pywin32 / NSSM / winsw），而内网环境里"下载那个 exe"经常是唯一失败的一步。

因此 `install.ps1` 用**计划任务**：

| 需求 | 计划任务的做法 |
|---|---|
| 开机自启、无需登录 | 触发器 `AtStartup` + 主体 `SYSTEM` + `RunLevel Highest` |
| 崩溃后自动拉起 | 给启动触发器挂 1 分钟重复，`MultipleInstances=IgnoreNew` 防叠进程 |
| 不因超时被杀 | `ExecutionTimeLimit = 0`（不限时） |

代价：崩溃恢复粒度是 1 分钟，不如 NSSM 的进程退出钩子细。这是明说的取舍，不是遗漏。

---

## 1. 前置

| 组件 | 版本 | 说明 |
|---|---|---|
| Windows Server | 2019 / 2022 | 需 Administrator 权限 |
| Python | 3.11 – 3.13 | 装到系统 PATH；勾选 "Add python.exe to PATH" |
| PostgreSQL | 15+（本项目在 17.10 上验证） | 本机 5432，需 `psql` 在 PATH |
| Label Studio | 1.23.2 | 独立 venv，见 §5 |

目录约定（脚本默认值，可改）：

```
D:\Forge\app      源码（platform\ 的内容）
D:\Forge\app\venv orchestrator 的虚拟环境
D:\Forge\data     配置 / 日志 / LS 数据（与代码分离，升级不覆盖）
```

---

## 2. 数据库初始化

```powershell
# 用 postgres 超级用户建库与角色（一次即可）
$env:PGPASSWORD = "<postgres 密码>"
psql -U postgres -c "CREATE DATABASE forge;"

# 应用角色：无 superuser、无 bypassrls —— RLS 是最后一道边界，不能给它开后门
psql -U postgres -d forge -f D:\Forge\app\deploy\migrations\000_roles.sql
```

然后按编号顺序执行迁移（`010_identity.sql` 建 `forge.users` / `forge.invites`）：

```powershell
Get-ChildItem D:\Forge\app\deploy\migrations\*.sql | Sort-Object Name | ForEach-Object {
  Write-Host "== $($_.Name)"
  psql -U postgres -d forge -f $_.FullName
}
```

> **注意**：`000_roles.sql` 里 `forge_app` 的密码是占位值。生产必须先改：
> `ALTER ROLE forge_app PASSWORD '<强随机>';`，并把同一个值填进安装脚本的 DSN。

---

## 3. 安装平台

```powershell
cd D:\Forge\app\deploy\windows
.\install.ps1 -AppRoot D:\Forge\app -DataRoot D:\Forge\data `
              -ApiPort 8300 -LSPort 8080 -Domain forge.example.cn
```

脚本会：
1. 建 venv、装依赖（走阿里云镜像，pypi.org 在内网常被拦）
2. 生成 `FORGE_JWT_SECRET`（48 字节 hex，**不写默认值**：没有密钥就不许 native 模式启动）
3. 写 `D:\Forge\data\config\forge.env`，并 `icacls` 收紧到 Administrators + SYSTEM
4. 生成 `run-orchestrator.cmd`（环境变量以字面 `set` 写死，避免 cmd 解析 `.env` 时被 `=` `&` 截断）
5. 注册并启动计划任务 `ForgeOrchestrator`
6. 若存在 `D:\Forge\app\lsvenv`，一并注册 `ForgeLabelStudio`

自检：

```powershell
curl.exe http://127.0.0.1:8300/healthz
# 期望：{"ok":true,...,"auth_mode":"native","db":{"ok":true,...,"rls_forced":22,...}}
```

---

## 4. 建第一个管理员

第一个账号没人能邀请它（鸡生蛋），所以只能从命令行开通 —— 有服务器权限才配这个资格。

```powershell
cd D:\Forge\app\services\orchestrator
D:\Forge\app\venv\Scripts\python.exe -m tools.bootstrap_admin `
    --phone 13800138000 --name "张伟" --roles platform_admin,ops_director
```

输出里的**初始密码只显示一次**（库里只有 scrypt 哈希）。首次登录会被强制改密：
服务端对其它接口一律返回 `403 password_change_required`，前端把路由钉在改密页。

之后员工开通走邀请码：`系统设置 → 邀请码` 生成，员工在登录页「凭邀请码注册」，
注册后状态是 `pending`，管理员在 `系统设置 → 人员` 激活即可登录。

---

## 5. Label Studio

独立 venv，不要和 orchestrator 共用（依赖冲突）：

```powershell
python -m venv D:\Forge\app\lsvenv
D:\Forge\app\lsvenv\Scripts\pip install label-studio==1.23.2 `
    -i https://mirrors.aliyun.com/pypi/simple/
```

### 5.1 换成 PostgreSQL（强烈建议）

SQLite 是**单写锁**：多人同时提交标注会 `database is locked`，症状是随机的
"保存失败"，且日志里看不出是谁的问题。10 人以上必须换 PG。

```powershell
$env:PGPASSWORD = "<postgres 密码>"
psql -U postgres -c "CREATE ROLE ls LOGIN PASSWORD '<强随机>';"
psql -U postgres -c "CREATE DATABASE labelstudio OWNER ls;"
```

把下面几项追加进 `D:\Forge\data\config\forge.env`，再重跑 `install.ps1`（它会把它们
写进 `run-labelstudio.cmd`）：

```
LS_SECRET_KEY=<64 位 hex>
LS_POSTGRE_NAME=labelstudio
LS_POSTGRE_USER=ls
LS_POSTGRE_PASSWORD=<与上面一致>
```

首次切库需要建表：

```powershell
$env:DJANGO_DB="default"; $env:POSTGRE_NAME="labelstudio"
$env:POSTGRE_USER="ls"; $env:POSTGRE_PASSWORD="<强随机>"
$env:POSTGRE_HOST="127.0.0.1"; $env:POSTGRE_PORT="5432"
D:\Forge\app\lsvenv\Scripts\label-studio.exe migrate --data-dir D:\Forge\data\labelstudio
```

> **切库不会自动搬数据。** 原来的 SQLite 里已有 5 个项目 / 8 个任务 / 3 条标注。
> 换库后 LS 是空的，需要用官方 REST API 重新导入（`/api/projects` 建项目、
> `/api/projects/{id}/import` 灌任务）。这一步属于迁移作业，别指望 `migrate` 代劳。
> 如果暂时不想搬，就先把 SQLite 留在原处、只增加 `--data-dir` 的备份频率。

### 5.2 平台与 LS 的账号关系

LS 的 `USERNAME_FIELD` 是 `email`，CE 版没有 OIDC/SAML，改不动（动它等于 fork Django 迁移）。
所以每个员工在 LS 里另有一个账号，邮箱用合成地址 `13800138000@staff.forge.local`，
凭证由平台代持（`forge.users.ls_email` / `ls_user_id`）。员工全程只看见手机号。

---

## 6. HTTPS（不配这一项就别上线）

没有反代时浏览器直连 `http://<IP>:8300`，**手机号与密码在办公网里是明文**。
这不是"以后再说"的优化项。

### 6.1 Caddy（推荐，自动签证书）

```powershell
# 有公网时用官方源；内网拿不到就走 GitHub 代理镜像
#   https://ghproxy.net/https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_windows_amd64.zip
Expand-Archive caddy_2.8.4_windows_amd64.zip -DestinationPath C:\Caddy
copy D:\Forge\app\deploy\caddy\Caddyfile C:\Caddy\Caddyfile
```

`Caddyfile` 里 `{$DOMAIN}` `{$WEB_ROOT}` `{$LOG_DIR}` 由环境变量注入。注册为服务：

```powershell
$env:DOMAIN="forge.example.cn"; $env:ACME_EMAIL="ops@example.cn"
$env:WEB_ROOT="D:\Forge\app\web\dist"; $env:LOG_DIR="D:\Forge\data\logs"
C:\Caddy\caddy.exe validate --config C:\Caddy\Caddyfile --adapter caddyfile
C:\Caddy\caddy.exe run --config C:\Caddy\Caddyfile --adapter caddyfile
```

用 `install.ps1` 同样的思路注册一个开机任务即可（把上面三行环境变量写进 `run-caddy.cmd`）。

### 6.2 内网无域名

把站点地址改成 `http://<服务器IP>`，并启用 `Caddyfile` 末尾的网段限制注释块，
只允许办公网段访问 `/api/v1/auth/*` 与 `/api/v1/admin/*`。明文传输的风险仍在，
但至少认证接口不出内网。**要真正安全必须有证书**，内网可自建 CA 后分发根证书。

### 6.3 前端构建

```powershell
cd D:\Forge\app\web
# .env.production：VITE_API_BASE 留空（同源走 /api 反代），VITE_LS_BASE=https://ls.<域名>
npm ci
npm run build
```

`VITE_API_BASE` 置空是刻意的：同源后由 Caddy 把 `/api/*` 转给 8300，
既省掉 CORS 白名单，也避免把内网端口写死进前端产物。

---

## 7. 备份

必须备份的三样，缺一不可：

```powershell
# 1) 业务库（含全部审计日志，append-only）
& "C:\Program Files\PostgreSQL\17\bin\pg_dump.exe" -U postgres -Fc forge > D:\backup\forge.dump

# 2) Label Studio 数据目录（SQLite / 上传的原始文件都在里面）
robocopy D:\Forge\data\labelstudio D:\backup\labelstudio /MIR

# 3) 运行配置（含 JWT 密钥：丢了所有会话失效，不丢数据）
robocopy D:\Forge\data\config D:\backup\config /MIR
```

---

## 8. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `error 1053` 服务起不来 | 用了 `sc.exe` 而不是计划任务 | 见 §0，用 `install.ps1` |
| `Psycopg cannot use the 'ProactorEventLoop'` | Windows 下 uvicorn 强制 Proactor | 必须用 `python -m app.serve`，不要 `uvicorn app.main:app` |
| `/healthz` 里 `db.ok=false` | DSN 密码错 / PG 未启动 | 查 `logs\orchestrator.err.log` |
| 登录一直"手机号或密码错误" | 哈希格式不对（手工 insert 过的库） | 用 `tools.bootstrap_admin --reset` 重设 |
| 所有接口 401 | `FORGE_JWT_SECRET` 变了 | token 全部失效属正常，重新登录 |
| 标注页白板 | LS 未起 / `X-Frame-Options` 挡了 iframe | 查 `logs\labelstudio.err.log`；核对 Caddyfile 的 `frame-ancestors` |
| 计划任务没起 | 主体不是 SYSTEM 或无 Highest | `Get-ScheduledTaskInfo -TaskName ForgeOrchestrator` 看 `LastTaskResult` |
| 页面顶栏挂「演示数据」 | 前端没连上后端（VITE_API_BASE 空 / 后端挂 / token 过期） | 打开浏览器控制台看 `/api/v1/auth/me` 的返回；`isBackendConfigured()` 为 false 时前端直接走降级 |
| 顶栏显示「部分模块未加载（N）」 | 该角色缺某个模块的读权限（如财务没有 `member:read`） | 这是设计行为，不是故障；鼠标悬停看是哪个模块。要放开就改 `security.py` 的 ROLE_MATRIX |
| 项目页「承接供应商」全是「待分包」 | `project_suppliers` 没有该项目↔供应商的行 | 建项目时勾选供应商，或 `insert into forge.project_suppliers` 补关系。任务分配不会自动补这一行（两件事：谁能接这个项目 / 这一批派给谁） |
| 交付日显示「未排期」 | `due_at` 为空 | 正常。旧版本这里会显示 `—（NaNd）`，根因是 `new Date("")` 得到 NaN，已修 |

---

## 9. 当前还没做完的事（不要当成已完成）

1. ~~前端只有登录/改密接了真库~~ —— **2026-11 已完成**：工作台、项目、任务、数据、
   审核、供应商、人员、绩效、结算九页全部读真实 PostgreSQL，写操作（建项目、建供应商、
   供应商定级、任务分配/提交/审核、结算流转、资产入湖）全部走后端接口。
   验收方式：`cd web && python tools/verify_wired_ui.py --phone ... --password ...`
   （26 项，用接口真实条数比对界面渲染结果）。
   仍然只有本地实现的只剩「新增/修改供应商成员」—— 页面会明确报错，不会假装成功。
2. **对象存储未接**：`/healthz?deep=1` 的 `storage.ok` 为 false，预签名上传返回 503。
   需要装 MinIO 并配 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` / `FORGE_S3_ENDPOINT`。
3. **手机号 → Label Studio 免登录嵌入未做**：目前标注页用自己的 LS 会话，
   需要为每个用户建 LS 账号并缓存 refresh token。
4. **postgres 超级用户密码仍是默认值**：上线前必须改。
5. **企业统一登录在等对方的凭据**：代码、迁移、前端入口与回调页都已完成并通过
   自建假 IdP 的 23 项后端断言 + 8 项浏览器断言，但**还没有对真实 IdP 跑过一次**。
   缺的是对方提供 `issuer` / `client_id` / `client_secret`，并在他们后台把
   `https://<我们的域名>/#/sso/callback` 登记为允许的回调地址。
6. **成员管理缺写接口**：`POST /api/v1/supplier-members` 与改角色接口还没有，
   目前新增标注员要在数据库或 LS 里维护。

## 10. 与集团另一个平台共用账号池（SSO）

### 10.1 这套设计的三条硬约束

1. **认证归对方，授权归我方。** id_token 只用来确认"你是谁"（取 `sub`）。
   角色永远从 `forge.users.roles` 读。对方 IdP 的管理员**没有**能力在本平台
   授予 `platform_admin` —— 那是能审批付款、能读全部审计的角色。
   （`tests/e2e_sso.py` 里专门有一条断言：id_token 塞 `roles=["platform_admin"]`
   之后登录，落到库里仍是 `annotator`。）
2. **对方的 token 不作为本服务 API 的凭证。** 它只在回调那一刻被消费一次，
   随后签发自有的 JWT（带 `token_version`）。这样改密、停用、降权都能立刻让
   旧会话失效，不必等对方 token 自然过期。
3. **不按手机号自动合并账号。** 对方若能把 `phone_number` 声明改成任意值，
   就能把自己并到管理员账号上完成提权。所以首次撞到已有本地账号时只登记
   `forge.identity_link_requests`，要求本人再输一次本地密码，或由管理员批准。

### 10.2 配置步骤

**第 1 步：找对方要四个值**

| 项 | 说明 |
|---|---|
| `issuer` | OIDC 的签发者标识，必须是 discovery 文档里 `issuer` 的**逐字**值（不能多个尾斜杠） |
| `client_id` | 我们在他那边的应用标识 |
| `client_secret` | 回调换 token 用；只能存在服务端 `.env`，绝不下发前端 |
| 回调地址登记 | `https://<我们的域名>/#/sso/callback`，一个字符都不能差（含 `#`） |

**第 2 步：验证 discovery 可达**（在服务器上执行，别在开发机上验完就当成了）

```powershell
curl.exe -s "https://<issuer>/.well-known/openid-configuration"
# 期望：JSON 里 issuer / authorization_endpoint / token_endpoint / jwks_uri 齐全
# 平台启动时会逐字比对 issuer，不一致直接拒绝（防 IdP 混用/钓鱼）
```

**第 3 步：填 `.env` 并重启**

```ini
FORGE_SSO_ISSUER=https://idp.example.cn/realms/group
FORGE_SSO_CLIENT_ID=forge-platform
FORGE_SSO_CLIENT_SECRET=<对方给的>
FORGE_SSO_REDIRECT_URI=https://forge.example.cn/#/sso/callback
FORGE_SSO_PROVIDER=oidc
FORGE_SSO_ALLOW_JIT=true
FORGE_SSO_JIT_ROLE=annotator
```

四项凭据**要么全填、要么全空**。只填一部分时 `config.validate()` 会在启动期
直接报错 —— 半配置的 SSO 比没配更危险（按钮亮着，点了登不进来）。

**第 4 步：先跑一次浏览器验证再叫同事用**

```bash
cd D:\Forge\app\web
# 先构建一份"API 指向本机后端"的产物给验证脚本用（不动生产用的 dist）
set VITE_API_BASE=http://127.0.0.1:8300
npx vite build --outDir dist-sso
python tools/verify_sso_ui.py --api-port 8300 --web-port 9121
```

脚本会自己起假 IdP 和后端，把"点按钮 → IdP → 回调页 → 进工作台"整条链路走一遍。
对上真实 IdP 时至少手动验三种结局：新员工（自动建号且只有 annotator）、
老员工（要求输本地密码关联）、停用账号（拒绝且提示联系管理员）。

### 10.3 常见故障对照

| 现象 | 真实原因 |
|---|---|
| 登录要 1 秒以上，其它页面都正常 | 先看后端日志有没有「登录耗时偏高」。单次 scrypt 正常 100~200 ms；如果日志显示「验密」占了大头，多半是 CPU 被限频或电源方案把进程压到能效核（笔记本上常见），服务器上不会这样 |
| 一个人登录时别人全卡住 | 已经修过了（scrypt 挪进线程池）。若复现，跑 `tests/diagnose_stability.py`，看「并发登录是真的并行」那条是否红了 |
| 监控老是报服务挂了 | 监控配的是 `/healthz?deep=1`。改配 `/healthz`（10 ms），deep 留给人看 |
| 点登录转一圈回登录页 | 回调地址没逐字一致（最常见是少/多 `#`），或 `state` 过期（默认 600s） |
| `签发者不一致（issuer mismatch）` | 对方 discovery 的 `issuer` 与配置不同（有的 IdP 带尾斜杠） |
| `InvalidSignatureError` | 对方轮换了签名密钥但 `jwks_uri` 缓存没刷新，重启服务即可 |
| 登进去是 annotator，明明是管理员 | 正常。角色由本平台管理员授予，不来自对方 claim。去「人员管理」加角色 |
| `该手机号在本平台已有账号，但需要管理员确认` | 命中了一个没设本地密码的本地账号；去「人员管理 → 待确认绑定」批准 |
