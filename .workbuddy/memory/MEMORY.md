# 项目长期约定（2026-11-10 精简版；细节看当日日志）

## 一、用户口述过的硬决策（优先级最高，别再翻案）
1. **标注表单 = 甲方进 LS 自己设计**（11-07）。平台只签一次性票据 `GET /api/v1/ls/design-ticket`，
   边界在 `labelstudio/brand/forge_sso.py::_guard_designer()`（只放行 body 严格等于 `{"label_config":...}`
   的 `PATCH /api/projects/<id>/`）。自建设计器已归档 `tools/legacy/flow-designer/`，`014_label_flow.sql`
   已退出迁移链，**不要恢复**。已知缺口：LS 原生保存不拦已有标注。
2. **标注员数据行级一致**（11-07）：口径见第三节。
3. **交付 = 一个包、双击就能装**（11-08「不能就发一个包吗」）。安装全程在服务器本机，不需要远程/开发机在线。
4. **★ 不要注册任何服务/计划任务/开机自启**（11-09「服务器压根不会关」）。PG 用 `pg_ctl` 直起，三个进程
   `spawn_detached`，保活+备份交给 `--watch` 守护进程。
5. **交付面只有 Windows**（11-09「不用打LINUX，直接打WINDOW，更好了」）。不要再跑 `make_package.py`。

## 二、本机环境与日常操作
- 端口：LS 8080 / 编排层 8300 / 前端 dev 9120 / 原生部署 caddy 80 + LS 8100。
  编排层必须在 `platform/services/orchestrator` 下跑 `python -m app.serve`。
  native 模式必须带 `FORGE_AUTH_MODE=native` + `FORGE_JWT_SECRET`（≥32 字符），否则启动即校验失败。
- 本机 5432 是用户自己的 PostgreSQL 17（服务名在跑），`forge` 库在；口令 `postgres/postgres`；
  里面有老演示账号 `+8613800138000 张伟` / `+8613800138001`（`create-admin` 可重置口令）。
- 本机联调账号：管理员 `13800138001 / Manager@2026`（`create-admin` 后需清 `must_change_pwd` 才能用）。
- LS access token 用 `Bearer`（`Token ` 一律 401）；`FORGE_LS_REFRESH_TOKEN` 是 refresh token，
  换 access 走 `POST /api/token/refresh {"refresh":...}`。
- **启 LS 必须带 `USE_DEFAULT_BANNED_SUBNETS=false`**，否则 SSRF 防护静默丢弃内网 webhook，
  出现「数字对了但推送断了」。判真用 `check_webhook_push.py`（标注 1–2s +1 / 删除 1–2s 回退 / 日志无 SSRF）。
- 本机 `subprocess` 里写 `bash` 会解析到 `System32\bash.exe`（wsl）被拦 → 显式 `/c/Program Files/Git/bin/bash.exe`。
- **沙箱里 `HTTP_PROXY/HTTPS_PROXY` 指向 127.0.0.1:55774**：Python/curl 打 localhost 会被代理接走并返回
  502「upstream connect failed」。测试脚本一律 `build_opener(ProxyHandler({}))` 或 `no_proxy=127.0.0.1`。
- 沙箱后台进程：`&` 起的服务在本轮 bash 结束就被杀 → **启动+测试必须写在同一次调用里**；
  要长活必须 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` 直接起 `postgres.exe`（经 `pg_ctl` 起的仍会被回收）。
- 沙箱另外禁 `sc.exe` / `schtasks.exe`（程序黑名单）；删 >50 文件的大目录被 safe-delete 钩子拦。
- 打包链：`check_package.py --source`（核源码树，**别写死日期选旧包**）。
- `web/dist` 现在是**同源相对路径**构建（见第五节），`npm run build` 走 `.env.production`。

## 三、标注员数据一致性（口径 + 边界）
- 唯一正本 = LS `/api/tasks?project=N`。平台**不存/不复制/不缓存任何一行任务数据**；
  `GET /api/v1/me/assignment/items` 每次实时代理，逐行 `id/data/meta/data_sha256/is_labeled/drafts/updated_at/open_path`，
  整份算 `fingerprint`；不接受项目参数（集合来自 RLS）；LS 不可达该项目一行不回；上限 2000 行，超出 `truncated`。
- 合同量 `item_count`（500/400，¥1.20）是结算口径，绝不能抹成 LS 实际条数。
- 前端「我的任务」= 「我的数据」：逐条显示 LS task id 与真实内容，KPI 全部由**这一份**响应派生；
  可见时 60s 轮询 + 切回标签页重取；每批默认渲染 20 行（2000 行铺 DOM 会卡死）。
- 逐条打开 `/api/v1/ls/ticket?task_id=&ls_task_id=`：先 `ls.get_task()` 核 project 在 RLS 内，**再按 `is_labeled` 分流**：
  未标注 `?labeling=1&task=`，已标注 `?task=`。给已标注行带 `labeling=1` 会被 LS 静默跳到下一条
  （「点第 N 行打开第 N+1 行」）。`assignment._normalize_item` 与 `ls_bridge.next_path` 必须同时分流。
- 实测：500 行 3.0s / 558 KiB；2000 行 10s / 2.2 MiB；2200 行 11–19s 且 `truncated=1`。这是架构硬边界，别宣称可无限扩容。
- `in_sync=False` 有两种来源：①本次异常 ②业务数据债。断言只能要求「本次项目无告警」。
- 承诺「读时同源同次、不用旧数、漂移可见」，**不是**强一致。本功能不新增表。

## 四、交付形态（Windows 完整离线包）
- **唯一交付**：`platform/deploy/dist/智途-Windows完整离线-<日期>.zip`。
- 内含 `智途/`{安装说明.txt + 两个 exe + `payload/`(73) + `runtime/`{pgsql 1558 / py-ls 22.5k / py-orch 3.9k / caddy 1}}。
- 构建链：改前端 → `npm run build` → `make_payload.py` → `build_native.py --clean`
  → `verify_payload.py`（应 60/60）→ `make_offline_release.py`（约 6.5 分钟，882 MB→366 MB）→ 拷进 `dist/`。
  exe 必须用带 tkinter 的 `Python311`。
- 静态自检 `verify_payload.py` 不需要管理员/Docker/联网：逐字节一致、13 步且每步有 `step_<key>`、
  `orchestrator` 索引 < `database`、exe 不旧于载荷、安装程序体积 ≥ 监控台。
- **验不了（只能真机）**：80 端口接管、LS 首次 Django 迁移、Caddy validate、守护进程单实例与拉起。

## 五、安装程序与前端交付要点
- 第 02 步 **先 `stage_sources()`（payload/app → 安装目录）再 `stage_runtime()`**。少了这步第 04/05/07 步
  必报「缺编排层源码」。`stage_sources` 用 `_merge_tree` 只补缺不 rmtree；`stage_runtime` **先判目标
  `_runtime_ready()` 再判源**（同盘 move，装过一次源就空，先判源会误报「离线包不完整」）。
- 步骤表只有一份 `STEP_SPEC` + `step_list(inst)`，GUI 与 `--auto` 同源；13 步，第 10 步是**启动平台进程**
  （不是注册计划任务），第 12 步 LS 长期凭证必做（不做则标注完平台数字不动）。
- `run_steps()` 失败不中断；`NEEDS` 里前置 **fail 或 skip** 都让下游 SKIP（只判 fail 会多报假错误）。
- PG 取包双源：EDB 官方 zip + Maven `io.zonky.test:embedded-postgres-binaries-windows-amd64`（无 psql）。
  → 建库/迁移必须能无 psql 跑：`sql_split()` + 子进程 psycopg `_PG_RUNNER`；`000_roles.sql` 的 `\gexec`
  改成幂等 `DO $do$`；备份退化为冷备。
- 四个坑：①`pg_ctl start` 用 `run_daemon()`（DEVNULL），postgres 继承管道会让 `run()` 假死；
  ②`initdb` 60–90 秒，别用 40 秒超时判失败；③数 `information_schema` 必须连 `forge` 库；
  ④判库先看端口归属（`pg_owns_port`），别把别人的 5432 当自己。
- 口令自愈：第 03 步生成后立刻 `save_config({"pg_super"})`（否则重跑丢口令→`password authentication failed`）；
  起服务后 `pg_verify_super`，失败走 `pg_reset_super_password`（临时 HBA_TRUST，`finally` 必须原样写回）。
- **PG 起不来必须自己说清原因**（11-10 真机踩到）：`pg_start_local` 起之前清死进程残留的 `postmaster.pid`
  （PID 活着不动），失败后 `pg_log_tail` 读日志末尾 + `pg_classify` 分五类（残留锁/端口占用/版本不符/权限/无日志），
  残留锁自动重试一次；日志为空时补跑 `pg_ctl status` 拿关键措辞；`pg_ctl` 退出码非 0 就 3 秒内放弃。
  **顺序铁律：先删遗留服务（并等端口真关闭）再判端口**，反了必然撞 lock file。
- CLIXML：PowerShell 错误流是 `<Objs>` 且带 `_x000D__x000A_`，`ps()` 里脚本统一包 try/catch + `unwrap_clixml()`。
- **前端生产构建必须留空 `VITE_API_BASE`**（`.env.production`）。.env.local 的 `127.0.0.1:8300` 会被烧进 JS，
  换台电脑打开就请求访问者自己的本机 → 全空态。`http.ts` 的 `isBackendConfigured()` 恒 true、走同源 `/api`
  （Caddy 反代 8300）；展示用 `API_BASE_LABEL`，别再拼 `API_BASE`。

## 六、注册与演示数据（11-10 已闭环）
- 链路：管理员发邀请码 → 成员在登录页「凭邀请码注册」→ `status=pending` → 管理员在「人员管理」点激活 → 登录。
- 后端 `auth.py` 一直是完整的（`/register`、`/admin/invites`、`PATCH /admin/users/{id}/status`）；
  前端曾缺发码入口（假按钮）和激活按钮，11-10 补齐（`People.tsx` + `store.tsx` 的 `createInvite`/`setStaffStatus`）。
- 演示数据：`003_bootstrap.sql` 只建 `ent_forge`/智途，`verification/` 夹具不进包，前端 seed/dataMode 已删。
  重装到老库时 `on conflict do nothing` 不会改名/删号 → 003 里加了「旧品牌名自愈」update，
  `step_finish` 会点名遗留演示账号（不自动删）。

## 七、Webhook / 备份 / 品牌
- `ensure_webhook()` 复用时要双向核对 actions 与 headers（补 `X-Forge-Secret`；撤了 secret 要删旧头），
  否则回调被 404 丢掉函数还报 `reused ok`。
- 备份带 `.env.bak` 就必须能恢复：`restore.sh` 第 0 步比对 9 个密钥键，默认不动 `.env`，`--env=backup` 才回滚。
- 字标字体是**子集** `web/public/fonts/zhitu-serif.woff2`（只含 `· 智 途`），换名要重建子集。
- LS 合成邮箱域名 `staff.zhitu.local`，**改它 = 改身份**，三处必须配套（`ls_bridge.LS_EMAIL_DOMAIN`、
  `forge_sso.EMAIL_RE`、`_canonical_email`/`_adopt_legacy_email`）。

## 八、测试基建债（别假装已覆盖）
- `e2e_finance.py` / `smoke_http.py` 用 demo 假 token，native 下必 401；`e2e_labelstudio.py --token project_manager`
  同理。这些「多角色 RLS / 财务口径 / 状态机」用例在 native 下**没有重新验证**。
- `verify_ux.py` 在共享 project 13 上真提交且不清理，互相干扰。
- 全量回归：`web/tools/.tmp/run_full_regression.py`（SKIP 不算 PASS）。

## 九、尚未闭环
真机：80 端口接管、LS 首次迁移、Caddy validate、守护进程单实例。
其它：`013_ux_enhance.sql` 未在生产 `forge` 库跑；测试库 4 个批次未接入 LS；
多客户共用实例时 `client_viewer` 仍能读全部项目（RLS 待收窄）；`backup.sh`/`restore.sh` 未真实恢复过。

## 十、代码仓库与换机（11-10 建）
- 仓库：`https://github.com/Xiucheng0623/zhitu-platform`（**私有**，账号 Xiucheng0623）。
  2026-11-10 才第一次建独立仓库，**之前没有任何版本历史**（旧的 `git rev-parse --show-toplevel`
  指到 `C:/Users/G`，本项目 0 文件被跟踪）。
- 交付物（366MB 离线包、两个 exe、node_modules、web/dist、payload）**不入库**，见 `.gitignore`；
  `.gitattributes` 写 `* -text` 关闭行尾转换（逐字节自检依赖原始字节）。
- `.workbuddy/memory/` 随仓库走：clone 下来新 WorkBuddy 自动读到同一套记忆。
  前提是**工作目录必须是 clone 出来的仓库根**（记忆路径 `<workspace>/.workbuddy/memory/`）。
- 新机器开工看：`MIGRATION.md`（环境/出包）、`DEPLOY-PROMPT.md`（给新会话的自包含部署提示词）、
  `HANDOFF.md`（当前状态交接）。
- 网络事实：这台机器 **GitHub API 直连通、git 的 https 端点常被阻断**（502 / 443 超时）。
  push 失败时用 `gh api --method PUT /repos/.../contents/<path> --input <json>` 写文件；
  本地与远端会因此历史分叉，网络恢复后 `git fetch && git reset --hard origin/main` 对齐。
