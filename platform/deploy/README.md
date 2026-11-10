# FORGE · 智途数据生产平台 —— 服务器一键部署包

解压即用的 Docker 部署包。一条命令起完整平台：业务前端 + 业务中间层 +
Label Studio 标注工作台（带智途品牌与单点登录）+ PostgreSQL×2 + MinIO + Caddy 入口。

**部署包里不含任何演示数据**：迁移只建结构、权限和企业主体「智途」，
管理员账号由安装脚本用你给的手机号创建。首次登录看到的全是空列表——这是正确状态。

> ## 📦 当前对外交付形态：只有一个 Windows 原生包
>
> `deploy/dist/智途-Windows原生-<日期>.zip`。解压 → 右键以管理员身份运行里面的
> `智途安装程序（原生）.exe`，全程在本机完成，不需要 Linux、不需要 Docker、
> 不需要开发机在线。
>
> **Linux 的 `forge-deploy-*.tar.gz` 已停止产出**，历史包归档在
> `deploy/dist/_archive-linux/`（仅备查，不要再发出去）。本文档下半部分的
> `install.sh` / `smoke.sh` 保留为 Linux 路线的源码参考。
>
> 内容断言核对命令（不再需要 Linux 包）：
> ```bash
> python platform/tools/check_package.py --source
> ```

---

> ## ⚠️ 服务器是 Windows？先读这一段
>
> 本文档下面的所有命令（`install.sh` / `smoke.sh` / `backup.sh`）都是 **Linux** 的。
> Windows 服务器**不能直接跑**，而且 **Docker Desktop 不支持 Windows Server**
> （Docker 官方明确说明，订阅协议也限定它只能装在个人电脑/开发工作站上）。
>
> Windows 上有四条路，包内都带了。**默认走第一条**：
>
> | 方案 | 目录 | 前提 | 说明 |
> |---|---|---|---|
> | **原生 EXE（推荐）** | `windows-native/` | Windows 10/11 或 Server 2016+，只要装得上 Python 3.11–3.13 | 13 步装完：PostgreSQL / 编排层 / Label Studio / Caddy **全是 Windows 原生**，不碰 Docker、不碰 WSL、不需要嵌套虚拟化 |
> | **容器版 EXE** | `windows-client/` | Windows 10/11 或 Server 2022+，**且宿主支持嵌套虚拟化** | 图形界面，把镜像 `docker load` 进 WSL 里的 Docker；第 01 步就可能 `BLOCKED` |
> | **WSL 2 + Docker Engine** | `windows-docker/` | 同上 | 与本文档的 compose 完全同一套；先跑 `check-host.ps1` 判定能不能走 |
> | 裸机脚本（半成品） | `windows/` | 任意 Windows Server | 只装编排层，品牌补丁 / SSO / 前端 / Caddy / LS 凭证都要手工做，**已被 `windows-native/` 取代** |
>
> **为什么默认原生**：容器镜像的内核是 Linux，在 Windows 上必须靠 WSL 2 + 嵌套
> 虚拟化；低价云 Windows 主机多数没有透传，装到第 07 步直接 `BLOCKED`，前面
> 全白做。原生路线每个组件都用 Windows 版，没有这个前提。
>
> `windows-client/` 是容器路线的图形化外壳：`docker load` 镜像、落盘 compose、
> 起栈、自检、开端口、建快捷方式。**它一样需要 WSL 2 + 虚拟化**，第 01 步会先判定。
>
> 判断顺序：先跑
> ```powershell
> .\windows-native\智途安装程序（原生）.exe --selftest
> ```
> 它只体检不改系统。想要容器那种「一个镜像打包所有依赖」的形态，再跑
> ```powershell
> .\windows-docker\check-host.ps1
> ```
> 结论 `BLOCKED` 说明这台机器没有嵌套虚拟化，**别硬装 Docker**，回到原生路线。
> 详细步骤见 `windows-native/README.md`、`windows-docker/README.md`、`windows/README.md`。

---

## 1. 端口约定

刻意避开 **8080** 和 **3001**（常见被占用端口）。用到的端口：

| 端口 | 服务 | 对外 | 说明 |
|------|------|------|------|
| 80 | caddy | ✅ | 平台入口（HTTP；域名模式会 308 跳 HTTPS） |
| 443 | caddy | ✅ | HTTPS（域名模式自动签发证书） |
| 8100 | labelstudio | ✅ | 标注工作台（浏览器直达 + 平台 SSO 回跳） |
| 8300 | orchestrator | ❌ | 业务中间层（仅容器内，经 caddy 的 /api 反代） |
| 3000 | web | ❌ | 前端静态站（仅容器内） |
| 5432 | orchdb / lsdb | ❌ | PostgreSQL（仅容器内） |
| 9000 | minio | ❌ | 对象存储（仅容器内） |

改端口：编辑 `.env` 里的 `HTTP_PORT` / `HTTPS_PORT` / `LS_PORT`，然后
`docker compose up -d`。

---

## 2. 快速开始

**服务器要求**：Linux x86_64，4 核 8 GB 起（LS 构建吃内存），磁盘 ≥ 40 GB，
Docker 24+ 与 Docker Compose V2（`docker compose` 子命令）。

```bash
tar xzf forge-deploy-<日期>.tar.gz
cd forge-deploy

# 建议先单独跑一次体检（只读，不改系统）：
bash preflight.sh

# 域名部署（DNS 里 data.example.cn 和 ls.data.example.cn 都要解析到本机）
./install.sh --domain data.example.cn --phone 138xxxxxxxx

# 或纯 IP / 内网部署（无 HTTPS，只建议在受控内网用）
./install.sh --phone 138xxxxxxxx
```

`preflight.sh` 会检查九项：系统与架构、内存与磁盘、Docker/Compose、python3、
SELinux、防火墙与云安全组、端口占用、出网、包完整性。它把「反正后面一定会炸」
的条件提前问出来——构建阶段失败（Node 被 OOM、caddy 报 permission denied、
端口被占、registry 超时）的报错都很难读。`install.sh` 会自动先跑它，
有阻断项就停；确认要忽略用 `SKIP_PREFLIGHT=1 ./install.sh`。

install.sh 会依次：环境体检 → 检查环境 → 生成 `.env`（全部密钥随机）→ 选 Caddy 入口模板 →
`docker compose up -d --build`（首次 5~15 分钟）→ 建管理员 → 自动登录 LS 换 API
token → 跑冒烟检查 → 打印访问地址和密码（**只显示一次**）。

装完后：

- 平台入口：`https://data.example.cn` 或 `http://<服务器IP>`
- 标注工作台：平台里点「进入工作台」自动单点登录；也可直连 `https://ls.data.example.cn`
- 管理员：你给的手机号 + 打印出的密码（也存在 `.env`，权限 600）

---

## 3. 部署模式

| | 域名模式 | IP 模式 |
|---|---|---|
| 触发 | `--domain` 有值 | 不给 `--domain` |
| 入口模板 | `caddy/Caddyfile.domain` | `caddy/Caddyfile.ip` |
| HTTPS | 自动签发（需 DNS 就绪） | 无（明文 HTTP） |
| LS 地址 | `https://ls.<域名>` | `http://<IP>:8100` |
| 适用 | 公网 / 有多域名 | 内网试点、验收 |

**域名模式注意**：`<域名>` 与 `ls.<域名>` 两条 A 记录都要指向服务器，
否则 LS 子域证书签不下来。`ACME_EMAIL` 填真实邮箱（证书到期提醒）。

---

## 4. 目录结构

```
forge-deploy/
├── install.sh / backup.sh / restore.sh / smoke.sh   运维四件套
├── docker-compose.yml        服务编排（启动顺序由 healthcheck 串联）
├── .env.example              环境变量模板（install.sh 会生成 .env）
├── docker/                   三个镜像的 Dockerfile
│   ├── web.Dockerfile            Node 构建前端 → Nginx 静态托管
│   ├── orchestrator.Dockerfile   Python 业务中间层
│   └── labelstudio.Dockerfile    官方 LS + 构建期品牌补丁 + SSO 中间件
├── caddy/                    Caddyfile.domain（域名）/ Caddyfile.ip（纯 IP）
├── migrations/               仅结构/权限/引导/业务域，无演示数据
├── tools/fetch_ls_refresh.py 自动获取 LS API token
└── src/                      三个组件的源码（构建镜像时用）
```

Label Studio 镜像在**构建期**打品牌补丁（登录页字标、标题 favicon、中文层、
去掉原版加载动画与第三方云存储标识）并安装单点登录中间件 `core/forge_sso.py`。
补丁脚本是幂等的：升级 LS 后重跑构建即恢复品牌；锚点对不上会让**构建失败**，
不会带病上线。

---

## 5. 手工分步部署（不用 install.sh 时）

```bash
cp .env.example .env
# 编辑 .env：至少改所有 change_me 值、DOMAIN/PLATFORM_PUBLIC_URL/LS_PUBLIC_URL、
# ADMIN_PHONE。强随机密钥推荐：python3 -c "import secrets;print(secrets.token_hex(24))"
cp caddy/Caddyfile.domain caddy/Caddyfile     # 或 Caddyfile.ip
docker compose up -d --build
docker compose exec orchestrator python -m app.cli create-admin \
    --phone 138xxxxxxxx --name 管理员 --password '你的密码'
python3 tools/fetch_ls_refresh.py --ls-url http://127.0.0.1:8100 \
    --email <LS_ADMIN_EMAIL> --password <LS_ADMIN_PASSWORD>
# 把输出的 token 填进 .env 的 LS_REFRESH_TOKEN，然后：
docker compose up -d orchestrator
./smoke.sh
```

### 手工获取 LS API token（自动脚本失败时的兜底）

1. 浏览器打开 `http://<服务器>:8100/user/login/`，用 `LS_ADMIN_EMAIL/LS_ADMIN_PASSWORD` 登录；
2. F12 → Console：`fetch('/api/token/', {method:'POST', headers:{'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)[1], 'Content-Type':'application/json'}, credentials:'include', body:'{}'}).then(r=>r.json()).then(d=>console.log(d.refresh))`；
3. 把输出的值填进 `.env` 的 `LS_REFRESH_TOKEN`，`docker compose up -d orchestrator`。

**这个 token 不能省**：LS 1.23 的 access token 只有 5 分钟，orchestrator 靠
refresh token 续期；不配的话建项目/导入数据每 5 分钟 401。

---

## 6. 备份与恢复

```bash
./backup.sh                              # → backups/forge-<时间戳>/
./restore.sh backups/forge-<时间戳>              # 覆盖恢复，保留当前 .env
./restore.sh backups/forge-<时间戳> --env=backup # 连 .env 一起回滚
```

备份内容：平台库 `forge.sql`、LS 库 `labelstudio.sql`、LS 上传文件卷、
MinIO 对象卷、当期 `.env.bak`（**含密钥，权限 600，外传前加密**）。
数据库用 `--clean --if-exists` 导出，恢复可直接覆盖。

**跨机恢复必须注意密钥配套。** LS 库里的 access token 由当期 `LS_SSO_SECRET`
签，平台连 LS 用它记下的 `LS_REFRESH_TOKEN`。只灌数据库、沿用目标机自己的
`.env`，会出现「服务都起来了，但平台读不到 LS 任务、甲方 SSO 进 LS 全 401」。
`restore.sh` 开头会逐项比对 9 个密钥类键：有差异时**默认不动 `.env`**，
要求输入 `keep` 才继续；要连配置一起回滚就加 `--env=backup`（当前 `.env`
另存为 `.env.pre-restore-<时间戳>`，权限 600）。

建议：`crontab -e` 加一行每日备份，例如
`0 3 * * * cd /opt/forge-deploy && ./backup.sh >> backups/backup.log 2>&1`

---

## 7. 升级

```bash
./backup.sh                          # 先备份
# 替换包内 docker/ src/ migrations/ 等文件（保留 .env 与 backups/）
docker compose up -d --build
./smoke.sh
```

迁移全部幂等（`on conflict do nothing` / `create ... if not exists`），
重复执行不叠加。品牌补丁同样幂等，重建镜像自动对齐新版模板。

---

## 8. 常见问题

**标注页顶部快捷条「提交并下一题」按了没反应 / 提示未选标签**
增强层曾读错 LSF 内部字段而误拦（包内已修）：`Region.labeling.selected` 表示
"界面上哪个标签高亮"，**不是**"这个区域挂了哪些标签"，正常拖框时它一直是 `null`。
现在同时看 `labeling.value` / `region.results` / `serializeAnnotation()` 三个信源。
若在别的 LS 版本上又出现，先看 `window.__FORGE_UX__.diag()` 里的 `canvasKind`、
`required`、`requiredMissing`、`submitBlocked`，再对照
`platform/labelstudio/brand/ls-ux-enhance.js` 的 `regionHasLabel()`。

**条件分支题（选了 A 才出现 B 这一步）会不会被拦住**
不会，而且这是刻意保证的。`label_config` 里写
`<View visibleWhen="choice-selected" whenTagName="x" whenChoiceValue="y">` 时，
LSF 会把条件块 `display:none` 收起，但 XML 里的 `required="true"` 还在。
只读 XML 判断必填就会把"选了另一条分支"的人拦住。现在的规则是：
**页面上存在被收起的控件时，必填判定交给 LSF 自己**（它弹框说缺哪个，我们
把名字抽出来显示成"必填项未完成：xxx"）。`diag().requiredMissing` 会列出按 XML
算出来的缺项，与 `required` 对着看即可区分"真没填"和"被条件收起"。

**提交后没报错，但平台/引擎里没有这条标注**
这是最坏的一类问题，已修：LSF 校验不通过时**不会**进 `isSubmitting`、也不会换题，
只弹一个 `Warning / Checkbox "x" is required` 的框。早期版本只看
`isSubmitting`/任务 id，于是"被拦住"被判成"已提交"。现在提交后会先认这个弹窗，
弹出即报「必填项未完成：…」并关掉弹窗。

**已标注过的题，快捷条提交按钮找不到**
底栏按钮的 `data-testid` 随状态变化：全新任务是 `bottombar-submit-button`（提交），
已有标注/草稿是 `bottombar-update-button`（更新）。只认前者会让"回来改一下"
这种最常见的情况直接失效。现在两个都认。

**缩放热键在音频题上提示"音频题不支持缩放"，视频题没反应**
这是按题型分流的结果，不是故障：图片题真的放大（转发 LSF 的 ctrl+plus）、
音频波形不支持缩放所以直接说清楚、视频/PDF 交给 LSF 自己的缩放。
`diag().canvasKind` 会告诉你这一题被判成了哪一类（`image/video/audio/canvas/''`）。
注意增强层绑的是**单键** `+`/`-`/`0`，`Ctrl + +` 是 LSF 原生的，两条路都能用。

**自定义标注过程（步骤/顺序/必填/条件分支）怎么配 · 甲方能自己改吗**

能，**甲方进 Label Studio 用原生编辑器改**：项目详情 →「标注流程」面板 →
「在标注引擎里设计」。浏览器另开标签页直达 `/projects/<id>/settings/labeling`，
那里是 LS 自带的**浏览模板 / 可视化 / 代码**编辑器，覆盖全部控件、
`required` / `perRegion` / `visibleWhen` 条件分支、配置数据与实时预览。
保存后标注员刷新标注页就是新表单。`label_config` 的正本始终在 LS，
平台不存、也不解析表单结构。

**为什么不直接在平台里做一个设计器**：曾经做过（8 种控件 + 一级条件 + 手写 XML
兜底），但它永远追不上上游 LS 的控件与渲染行为，长尾仍要退回手写 XML，
等于长期维护一份半成品。已整体下线，源码归档在
`tools/legacy/flow-designer/`（**不在部署包里**），对应的
`014_label_flow.sql` / `forge.project_flows` 表也已从迁移链移除。

**为什么不是「直接给甲方开一个 LS 账号」**：LS 社区版没有 RBAC、
项目成员隔离、自定义权限（都是 Enterprise 能力）。一个原生 LS 账号能看所有项目、
建项目、导数据、删任务、导出标注。所以平台签一张**一次性受限 SSO 票据**：

| 环节 | 实现 |
|---|---|
| 平台侧 | `GET /api/v1/ls/design-ticket?project_id=...`，校验 `labeling:write`；`ls_project_id` 由服务端从项目表反查，前端不能指定；90 秒一次性；claims 带 `mode=design`、`can_import=false`、`ls_projects=[单个项目]`、`next=/projects/<id>/settings/labeling` |
| LS 侧 | `labelstudio/brand/forge_sso.py` 的 `_guard_designer()`（构建期由 `apply_brand.py` 注入 LS 镜像）：写操作**默认全拒**，只放行 `PATCH /api/projects/<id>/` 且请求体顶层严格等于 `{"label_config": "..."}`；项目白名单外一律 404；导出 / 凭证 / 名册 / Webhook / 云存储 / 导入 / 提交标注 / 其他设置页全部拒绝；`/api/projects/` 列表只回白名单项目 |
| 前端 | `web/src/design/LsDesignEntry.tsx` 只显示绑定状态与一个按钮。前端藏不藏按钮都不是安全边界 |

**前置条件**：项目必须先绑定一个**已经存在**的 LS 项目。管理员走
`POST /api/v1/projects/{id}/ls-binding`（`project:write`），服务端会先向 LS
核实该项目存在再写库，避免把甲方送进 404；甲方没有这个权限，不能换掉项目连的
LS 项目。只读状态走 `GET .../ls-binding`。新建项目时也可以直接在建项请求里传
`labeling_config_id`。占位串 `lsc_default` 不算绑定，签票会返回 409。

**一个必须讲清的风险**：LS 原生编辑器保存 `label_config` 时**不会拦已有标注**。
改控件名 / 删控件可能让旧标注渲染不出来。旧自建设计器那道 409 保护随之下线了，
目前靠项目规范约束（项目里已有标注时先在 LS 里确认影响范围再保存），
界面上也写了提示。若客户要求硬拦截，需要另做 LS 侧 webhook / 保存前校验，
不要假装有保护。

权限：`labeling:write`（标注标准编辑）单独一个动作，给
`platform_admin / ops_director / project_manager / client_viewer`。
**甲方不拿 `project:write`**：改表单是合理诉求，改合同金额、单价、交付日期不是。
越权语义仍是 404。两侧矩阵由 `services/orchestrator/tests/check_rbac_parity.py` 比对。

依赖迁移：本功能**不新增表**，只需 `013_ux_enhance.sql`（标注规范）。
`FORGE_LS_SSO_SECRET`、`FORGE_LS_PUBLIC_BASE_URL`、`FORGE_PLATFORM_URL`
三处配置必须一致，否则票据签发或 LS 侧验签会失败。

**平台上的进度和 Label Studio 里的对不上**
进度同步有两条路，任一坏了界面都不会报错，只是数字停住不动：
①LS 推 webhook（秒级）；②平台定时拉取（`LS_SYNC_INTERVAL_S`，默认 300 秒）。
在「任务管理」页顶部有「标注引擎对账」面板：能直接看到差在哪、以及自动对账
上一轮是什么时候跑的。手工兜底两件事：
- 单项/全部同步：面板上的「同步」按钮（`POST /api/v1/tasks/{id}/ls-sync`）；
- 只看不改：`GET /api/v1/ls/reconcile`。

对账口径是刻意不对称的：**进度以 LS 为准**（只有 LS 知道标了几条）、
**项目名以平台为准**、**合同条目数谁都不自动改**（LS 条数偏少通常意味着
数据没导完，自动抹平会把真问题藏起来）。注意这里说的是「对账面板里的合同数」，
和下面「标注员看到的任务数」是两件事。

如果面板显示「自动对账未启用」，说明 `LS_SYNC_INTERVAL_S=0` 或没透传进容器。

**LS 日志里出现 `URL resolves to a reserved network address`**
这是 LS 1.23 的 SSRF 防护把内网 webhook 回调拦了，webhook 会**静默丢弃**：
平台数字不涨，但定时对账过一会儿可能把它补上，所以「数字最终对了」不等于
「推送链路是通的」。compose 里已配 `USE_DEFAULT_BANNED_SUBNETS=false`
（只额外禁 link-local），`smoke.sh` 会直接断言这个变量。自己用 venv 或裸机
启动 LS 时**必须带上同名环境变量**，否则每秒都会踩一次：
`export USE_DEFAULT_BANNED_SUBNETS=false`。判断推送是否真通，看
`check_webhook_push.py`：造一条标注应在 1~2 秒内 +1，删掉后 1~2 秒回退，
且 LS 日志本轮不得出现 SSRF 拦截 —— 三条全过才算通过。

**标注员手里的数据、平台看到的数据、LS 看到的数据必须一样（500 vs 2）**

先把三个口径分开，否则永远说不清：`tasks.item_count` 是**合同/交付量**
（客户买了 500 条，用于计价与预算）；`/me/assignment` 回答的是**分到哪些
项目、每个项目多少条**；而用户要的「一样」最终落到**标注员手里一条条真实
数据** —— 平台页面列出的每一行，必须就是他在 LS 里打开、标注的那一行。
以前把合同量直接显示给标注员，于是「平台 910 条 / LS 4 条」看起来永远对不上；
更糟的是有些批次压根没绑定 LS 项目，平台仍把它们算作「分给标注员的任务」。

**做法：LS 的任务行是唯一正本，平台不保存、不复制、不缓存任何一行**
（读时实时投影 / 读时代理，不是定时同步，更不是双写）：

| 环节 | 做法 |
|---|---|
| 服务端 | `GET /api/v1/me/assignment/items`（`app/assignment.py`）：在调用方 RLS 下取该账号可见的 LS 项目，**本次请求实时**调 LS `/api/tasks?project=N`，把每一行原样带回来（`id`、`data`、`meta`、`is_labeled`、标注数、草稿数、`updated_at`），逐行算 `data_sha256`，整份清单算 `fingerprint`。接口不接受任何项目参数，前端无法指定别人的项目 |
| 前端 | 「我的任务」页主视图改为「我的数据」，逐条显示 LS task id、项目内序号与真实数据内容；KPI 条数、已完成数、状态**全部由这一份响应派生**，不再另读一个统计接口（两个来源就可能出现两个不同的数字）。页面可见时每 60 秒、切回标签页时自动重取；每批次默认只渲染前 20 行，可手动展开 |
| 逐条打开 | 点这一行 → `GET /api/v1/ls/ticket?task_id=…&ls_task_id=…`。服务端先向 LS 核实这条任务属于该账号可见的项目，再按状态分流落地地址：**未标注行**进标注流 `/projects/N/data?labeling=1&task=<id>`；**已标注行**进数据管理视图 `/projects/N/data?task=<id>`。为什么必须分流：LS 标注流队列只排未标注任务，给已标注行带 `labeling=1` 会被**静默跳到下一条**（实测 project 13 的 #5 已标注时，`labeling=1&task=5` 渲染出的是 #6），那样「点平台第 N 行」打开的就成了第 N+1 行。界面按钮也相应显示「打开这一条 / 查看这一条」，不假装已标注任务还能进标注流。跨项目或不存在的 task id 一律 404 |
| 集合约束 | `GET /api/v1/me/assignment` 仍负责批次集合 / 合同口径 / 漂移自检：批次 ↔ LS 项目 **1:1**、批次单 assignee，所以项目白名单等价于该人的任务行集合。自检会检测**两个批次共用同一个 LS 项目**（一旦共用，两个标注员就会看到对方数据）并报警 |
| 票据白名单 | 自检把 `ls/ticket` 的 `ls_projects` 与数据行所属项目逐项对比，防止把人送进平台里不存在的批次 |
| 漂移可见 | 定时对账每轮复用本轮已取的 LS 行算指纹（不额外压 LS），集合/数量结果进 `/healthz.assignment`，数据行条数与指纹进 `/healthz.assignment_items`；管理侧对账面板分别列「标注员任务一致」与「数据行一致」 |

**验证口径（真实账号逐行比，不是只比条数）**：`verify_annotator_data_parity.py`
把三份事实摆在一起：张三平台 4 行 == LS 管理员视角 4 行 == 他本人 SSO
会话里看到的 4 行，逐行 `data` / `data_sha256` / 标注状态 / 草稿 / 时间戳完全一致；
李四 0 == 0。往 LS 导入一条临时任务，平台刷新立刻变 5 行；删掉后立刻回 4 行、
指纹跟着变（证明没有缓存、没有另存一份）。浏览器验证点第 1 行，LS 落在
`?task=5` 且渲染出的正是「B1-批次一 样本 A」。合同 500/400 始终原样保留，
结算金额不受影响。

**边界（不要误读成绝对强一致 / 无限容量）**：
* 平台和 LS 是两个库、跨网络，不可能做到「任意时刻零延迟」。这里做到的是
  **平台展示的每一行与 LS 当下的行同源同次读取**，所以只要 LS 可达就不存在
  「两份数据不同步」这回事；LS 不可达或项目不存在时该项目一行都不返回，界面
  显示「无法核对」，**绝不拿旧数据假装一致**。两次刷新之间 LS 若被改动，指纹会变、
  轮询/刷新即追上；做不到的是「一个已经打开的浏览器页面在 LS 被改的瞬间自动变」。
* **数据规模是有上限的**：单项目单次最多带回 2000 行，超过会明确报「无法完整核对」
  而不是静默截断。轮询会实时代理 LS 全量任务行，人多 / 项目大时会给 LS 压力；
  要承载大项目，必须改成「先只取指纹、指纹变了才按需取行」或 LS 侧增量接口，
  那是架构变更，不要以为当前实现可以无限横向扩。
合同量始终原样保留。本功能不新增表、不需要迁移。

**改了代码后某个接口对所有人都是 404**
先看 `docker compose logs orchestrator | grep 权限自检`。
本项目的越权语义是 404（不是 403），所以**权限名拼错**和**资源不存在**在客户端
完全一样。启动时会自动核对代码里用到的权限名是否都在权限矩阵里，拼错的会直接
打 ERROR 并指出所在文件；`/healthz` 的 `selfcheck.permissions` 也能看到。

**漏了这条会静默不同步：`LS_SYNC_INTERVAL_S`**
它控制平台多久拉一次 LS 的进度。设成 0 就只剩 webhook 一条路，
回调一断平台永久停在旧数字上，而且不会报任何错。冒烟会断言它大于 0。

**compose up 后 labelstudio 起不来，日志里连不上数据库**
环境变量必须是 `POSTGRE_*`（不是 `POSTGRESQL_*`）——包里已按 LS 1.23.2 的
实际读法配好。若自己改过 compose 又改回去，检查这一段。

**平台能登录，但点「进入工作台」被踢回 LS 登录页**
`.env` 里 `LS_SSO_SECRET` 与平台侧 `FORGE_LS_SSO_SECRET` 必须一致
（包里是同一个变量，不用两填）；且 `LS_PUBLIC_URL` 要与浏览器实际访问 LS 的
地址完全一致（含协议、端口）。

**业务接口 401 且日志提示 token 过期（每 5 分钟一次）**
`LS_REFRESH_TOKEN` 空了或失效。按第 5 节重新获取并写入，再
`docker compose up -d orchestrator`。

**冒烟里「品牌没打上」**
构建期补丁失败一般会直接导致镜像构建失败；如果镜像是旧的，
`docker compose build --no-cache labelstudio` 重建。

**忘记管理员密码**
```bash
docker compose exec orchestrator python -m app.cli create-admin \
    --phone <手机号> --name 管理员 --password <新密码> --force
```

**端口被占用**
改 `.env` 的 `HTTP_PORT/HTTPS_PORT/LS_PORT`（避开 8080/3001 之外也要避开
本机已占用的），`docker compose up -d`。

**磁盘涨得快**
`docker system prune` 清构建缓存；`backups/` 定期归档转移；
PostgreSQL WAL 在容器卷里，一般无需干预。

---

## 9. 安全清单（上线前逐条过）

- [ ] `.env` 权限 600，且**不进**任何 git / 网盘 / 聊天工具
- [ ] 管理员密码已改掉自动生成值并妥善保存（install.sh 只打印一次）
- [ ] 公网部署走域名模式（HTTPS），不在公网裸跑 IP 模式
- [ ] `FORGE_AUTH_MODE` 保持 `native`；`dev` 模式绝不上公网
- [ ] 防火墙只开 80/443/8100；数据库端口不对公网开放
- [ ] `LS_ADMIN_PASSWORD` 用强密码（这个号能建项目/导入数据）
- [ ] 升级已有环境时补跑 `013_ux_enhance.sql`，并跑一次 `./smoke.sh`（`014_label_flow.sql` 已废弃，不要跑）
- [ ] 甲方账号用 `client_viewer`，确认有 `labeling:write` 但**没有** `project:write`
- [ ] `FORGE_LS_SSO_SECRET` / `FORGE_LS_PUBLIC_BASE_URL` / `FORGE_PLATFORM_URL` 三处一致，否则设计票据签发或 LS 验签会失败
- [ ] LS 镜像构建执行了 `apply_brand.py`，即 `_guard_designer()` 真的进了运行中的 LS（冒烟会断言）
- [ ] 定期 `./backup.sh` 并把备份放到另一台机器
- [ ] 需要更严的入口管控时，启用 `caddy/Caddyfile.ip` 末尾的网段限制注释

---

## 10. 权限模型（速查）

| 角色（forge.users.roles） | 平台 | 进 LS 设计表单 / 编辑规范 | LS 建项目/导入 | LS 标注提交 |
|---|---|---|---|---|
| platform_admin / ops_director / project_manager | 全部 | ✅（`labeling:write`） | ✅ | ✅ |
| 审核 / 供应商 | 业务页 | ❌ | ✅ | ✅ |
| annotator（普通标注员工） | 业务页 | ❌ | ❌（服务端 403） | ✅ |
| client_viewer（甲方） | 项目与绩效只读 | ✅（`labeling:write`） | ❌ | ❌ |

权限的真正边界在 **LS 服务端**：标注员经 SSO 进 LS 后，
建项目/导入/云存储/批量建任务的接口一律 403（前端只是顺手藏了按钮）。
供应商不受限。标注提交接口刻意不拦。

甲方走的是**另一类会话**（票据 `mode=design`）：由 `_guard_designer()` 收口，
连项目名、抽样策略、成员名册都不给看，只允许保存 `label_config` 一个字段。
两类会话互不相通，不要把「标注员能提交标注」理解成「甲方能提交标注」。

---

## 11. 组件版本

| 组件 | 版本 | 说明 |
|---|---|---|
| Label Studio | 1.23.2（`heartexlabs/label-studio`） | 构建期打品牌补丁 + SSO 中间件 |
| PostgreSQL | 17（业务库）/ 16（LS 库） | 两库隔离，互不影响 |
| MinIO | RELEASE.2024-10-13 | S3 兼容对象存储 |
| Caddy | 2.8 | 自动 HTTPS |
| Node / Python | 22-alpine / 3.13-slim | 构建期与运行期 |

迁移链（按序、全部幂等）：`000_roles → 001_schema → 002_rls → 003_bootstrap →
005_finance → 006_collection → 007_rls_ext → 010_identity → 011_sso →
012_member_link → 013_ux_enhance`。
`013` 建标注规范表。**升级已有环境时必须补跑 `013`**，否则标注规范侧栏读接口 500。
曾经存在 `014_label_flow.sql`（自建表单设计器的 `forge.project_flows` 表），
已随自建设计器一起下线，**不在部署包、不在自动链路、不要再跑**；
旧环境里若已经建了这张表，留着即可（空表，无代码引用），也可以手工 drop。
**没有任何种子/演示数据脚本**——旧的验收夹具（灌假供应商/假项目）只存在于
开发仓库的 `migrations/verification/`，不进部署包、不进自动链路。
