# 智途 · Windows 原生安装（无 Docker / 无 WSL / 无 Linux）

面向**一台 Windows 服务器**：组件全部用 Windows 原生版。

## 0. 三种装法，先看这一节

| 装法 | 命令 | 需要什么 | 用时 | 什么时候用 |
|---|---|---|---|---|
| **① 纯源码装**（推荐） | `python install_from_source.py` | 一个 clone + Python 3.11–3.13 + 外网 | 25–45 分钟 | 服务器能上外网。**不要 exe、不要离线包、不要 Node** |
| ② 安装套件 | 解压 → 右键 exe | 同 ①，加一个 24 MB 的 zip | 25–45 分钟 | 想要图形界面的进度条和日志窗口 |
| ③ 完整离线包 | 解压 → 右键 exe | 一个 366 MB 的 zip，**全程不联网** | 3–8 分钟 | 服务器完全断网（内网隔离） |

三条路跑的是**同一套 13 步逻辑**（`src/zn_setup.py`）。exe 只是给这套逻辑套了个
tkinter 窗口 —— 所以「不想用安装程序」完全成立，`install_from_source.py` 就是
去掉窗口的入口，它还顺手把原来打包时才做的 `make_payload.py` 也一起跑了。

方式 ① 的最小流程：

```bash
git clone https://github.com/Xiucheng0623/zhitu-platform.git D:\zhitu-platform
cd D:\zhitu-platform\platform\deploy\windows-native
python install_from_source.py --plan      # 先看会装到哪、跑哪 13 步，不动系统
python install_from_source.py             # 真装（管理员终端）
```

它和 exe 版本的**唯一实质差别**：没有进度条窗口，输出走控制台 + 日志文件
（`%LOCALAPPDATA%\Zhitu\logs\install-*.log` 与 `install-report-*.txt`）。

前端产物 `platform/web/dist` 已随仓库提交（536 KB），所以服务器上**不需要 Node**。
改过前端源码后，本机 `cd platform/web && npm run build` 再提交 dist 即可。

---

## 0.1 五个组件

| 组件 | 用什么 | 说明 |
|---|---|---|
| 数据库 | PostgreSQL for Windows（官方 binaries zip） | 解压 + `initdb` + `pg_ctl` 直接起进程 |
| 编排层 | Python venv + pip | 本项目自己的 FastAPI 服务，端口 8300（只监听本机） |
| 标注引擎 | `pip install label-studio` | 官方 pip 包，Django 应用，端口 8100 |
| 统一入口 | `caddy.exe` for Windows | 80 端口：静态前端 + `/api` 反代 |
| 保活 | `--watch` 守护进程 | **不注册 Windows 服务、不注册计划任务、不开机自启**（服务器常年不关，用户明确要求） |

**全程不会启动 WSL、不需要 Docker、不需要嵌套虚拟化。** 服务器就是一台 Windows。

**安装是在服务器本机完成的**：不需要远程连接、不需要开发机在线、不需要任何人连进来操作。
方式 ① 直接 clone 到服务器上装；方式 ②③ 把 `release/` 里的 zip 拷进去解压。
装完之后运维也只用这台服务器自己的「智途监控」看状态。

---

## 1. 和「容器版」的区别

同一套平台代码，两种落地方式：

|  | 原生版（本目录） | 容器版（`../windows-client`） |
|---|---|---|
| 是否需要 WSL 2 | 不需要 | 需要（且要嵌套虚拟化） |
| 是否需要 Docker | 不需要 | 需要（WSL 里的 Engine） |
| 服务器上有没有 Linux 痕迹 | 没有 | 有（容器内核） |
| 首装耗时 | 20–40 分钟（大头是 pip） | 20–40 分钟（大头是 build） |
| 交付目录 | 约 15 MB | 约 25 MB（或带镜像 2–3 GB） |
| 升级方式 | 换 `payload/` 重跑安装 | 重新 load 镜像 |
| 备份 | `pg_dump` + 拷 `data\` 目录 | 同 |

云 Windows 主机多数**没有**嵌套虚拟化，容器版会在第 07 步直接 `BLOCKED`；
原生版没有这个前提，所以「服务器就一台 Windows」时它是更稳的那条路。

---

## 2. 前置（只有两样，其余安装程序自己下载）

| 项 | 要求 | 说明 |
|---|---|---|
| Windows | Windows 10 / Server 2016 以上 | 安装程序第 01 步会判 |
| Python | **3.11 – 3.13**（3.12 最佳） | 没有的话第 02 步会尝试 `winget` 装 3.12；仍失败会给出下载地址 |

PostgreSQL（约 330 MB）与 Caddy（约 16 MB）由安装程序自动下载。
**完全断网**部署时，把这两个包提前放进 `payload\cache\`，安装时就不会联网。

> **PostgreSQL 有两个下载源，会自动兜底**：先试 EnterpriseDB 官方 zip（330 MB，
> 带 `psql` / `pg_dump`）；**卡住 45 秒没数据就换** Maven Central 的备用包
> （23 MB，只有服务端）。备用包没有客户端工具，于是建库/迁移改由编排层 venv 里的
> `psycopg` 执行、备份自动退化成冷备 —— 功能不缺，只是工具链换了实现。
> 实测：本机走代理拉 EDB 只有 ~160 KB/s，备用源 100 秒就位；国内大镜像站
> **都没有** `postgresql/binary/` 目录（实测 404），所以备用源只能是 Maven。

> Python 为什么要卡 3.11–3.13：Label Studio 的依赖里 numpy/pandas 这类在新
> Python 上常常没有预编译 wheel，pip 会退化成源码编译并失败。安装程序刻意
> **不挑最新版**，只挑兼容的。

---

## 3. 交付：就是一个包

发出去的东西只有**一个 zip**，解压出来三个文件，没有目录要配对：

```
智途-Windows原生-<日期>.zip          ← 只发这一个（23 MB）
  智途安装程序（原生）.exe           12 MB，载荷已内嵌（前端/源码/迁移/品牌/模板）
  智途监控（原生）.exe               11 MB
  安装说明.txt                       一页：怎么装、断网怎么办、常见问题
```

* **两个 exe 都是自包含的**：载荷（1.5 MB / 71 文件）打进了安装程序，
  不再需要 `payload\` 目录，漏拷一个文件也不会装不上。
* 断网部署：在 exe **旁边**建 `cache\`，把 PostgreSQL / Caddy 的包放进去
  （安装程序会认 exe 旁的 `cache\`、`payload\cache\` 和载荷自带的 `cache\`）。
* 仓库里仍然保留 `payload/` 这个**中间产物**（由 `make_payload.py` 铺）：
  它只在开发机上存在，是打 exe 的原料，不进交付包。

打包（开发机上一条链跑完）：

```powershell
cd platform\deploy\windows-native
python make_payload.py --with-cache   # 1) 铺载荷（要预置 PG/caddy 包才加 --with-cache）
python build_native.py --clean        # 2) 打两个 exe（安装程序自动内嵌载荷）
python make_release.py                # 3) 收成 release\ + 一个 zip
python verify_payload.py             # 4) 60 项静态自检：不需要管理员/Docker/联网
```

---

## 4. 安装：13 步

| 步骤 | 做什么 | 常见失败原因 |
|---|---|---|
| 01 环境体检 | 判 Windows 版本 / 管理员 / 磁盘 / 端口 80·8100·8300 | 80 被 IIS 占用；非管理员 |
| 02 Python | 找 3.11–3.13，缺了就 `winget` 装 3.12 | 机器禁了 winget，只能手装 |
| 03 PostgreSQL | 下载/解压 zip → `initdb` → 配端口 → 注册服务 | 5432 被占则自动顺延 5433+ |
| 04 编排层依赖 | 建 venv、`pip install -r requirements.txt` | 阿里云镜像不通 |
| 05 建库与迁移 | 建 `forge` 库、建 `forge_app` 角色、跑 11 个迁移、查表数量 | 迁移脚本出错会指名是哪个文件 |
| 06 标注引擎 LS | 建 venv、`pip install label-studio==1.23.2` | **最慢的一步，10–20 分钟**；`WinError 5` 会自动重试 |
| 07 品牌与单点登录 | 对 LS 的 site-packages 跑 `apply_brand.py` | 补丁锚点不匹配（LS 版本变了） |
| 08 配置与管理员 | 写 `forge.env`（9 类密钥）、建首个管理员 | 手机号格式不对 |
| 09 前端与入口 | 落盘前端、生成 Caddyfile、下 Caddy、`caddy validate` | 80 端口被占 |
| 10 计划任务与启动 | 注册并启动 3 个计划任务 | 非管理员无法注册 |
| 11 健康检查 | 轮询 `/healthz`、`:80`、`:8100`，最多 7 分钟 | LS 首次建表慢 |
| 12 LS 长期凭证 | 取 refresh token 写进 `forge.env` 并重启编排层 | 需要 LS 管理员口令 |
| 13 收尾 | 桌面网址快捷方式、写状态文件 | 不影响使用 |

要点：

* **第 06 步是最长的一步**，界面上会持续刷进度，不是卡死。
* **第 08 步的管理员手机号**是给人登录用的；初始密码在日志里只显示一次，
  首次登录会被强制改密。
* **第 12 步不能省**：LS 的 access token 只有 5 分钟，编排层靠 refresh 续；
  没有它，服务全绿、网页能开，但标注员标完之后平台数字不动。
* 重装会**保留已有密钥**（`forge.env` 里已有的值不覆盖），否则所有人会被踢下线。

命令行等价物：

```powershell
.\智途安装程序（原生）.exe --auto --admin-phone 13800138001 --admin-name 张伟
.\智途安装程序（原生）.exe --auto --no-download       # 断网，只用 payload\cache
.\智途安装程序（原生）.exe --selftest                 # 只体检
```

日志：`%ProgramData%\Zhitu\...`（装之前）→ `<data>\logs\`（装之后），
另有 `%LOCALAPPDATA%\Zhitu\logs\cli-*.log`。

---

## 5. 日常运维

| 做什么 | 怎么做 |
|---|---|
| 看整体状态 | `智途监控（原生）.exe`（或 `--status`，退出码 0 = 全部正常） |
| 起停 | 监控台「全部启动 / 全部停止 / 重启编排层」 |
| 看日志 | 监控台「打开日志目录」→ `orchestrator.log` / `labelstudio.log` / `caddy.log` |
| 数据库备份 | `cd <app>\services\orchestrator` → `<app>\venv\Scripts\python.exe -m tools.backup_db`（默认留 7 份，`--keep 14` 可调）；另拷 `<data>\labelstudio` |
| 换服务器 IP | 重跑安装第 08/09 步（会重写 `forge.env` 与 `Caddyfile` 的地址） |
| 升级代码 | 换 `payload\` 后重跑安装（密钥保留），再重启三个任务 |

计划任务名：`ZhituOrchestrator` / `ZhituLabelStudio` / `ZhituCaddy`
（外加 PostgreSQL 的 Windows 服务 `ZhituPostgreSQL`）。

---

## 6. 已知限制（别当成已解决）

1. **`windows/install.ps1` 那条老脚本是半成品**，本目录是它的替代：老脚本只装
   编排层，品牌补丁、SSO、前端、Caddy、LS 凭证全要手工做。
2. **Label Studio 官方推荐 Docker 部署**，pip 装在 Windows 上是支持的，但
   多用户高并发下的表现不如容器。10 人以内、单机使用没问题。
3. **Caddy 默认 http 明文**：内网可接受；要上公网必须配域名 + 证书
   （改 `Caddyfile.tpl` 增加域名站点块）。
4. **本机验证过的边界**见第 7 节。真实服务器上「从零装到三服务全绿」仍需跑一次。
5. **没有做自动更新**：升级靠重跑安装程序（幂等，密钥保留）。

---

## 7. 本地验证到哪一步（诚实清单）

已在开发机（Windows 11 26200）实测：

* `zn_common.py` 自检：Python 版本筛选（会跳过 3.15 选 3.11）、端口探测、
  局域网地址、密钥生成、HTTP 探测（绕系统代理）；
* PowerShell 走 `-EncodedCommand`，中文与特殊字符不经命令行编码，`Get-ScheduledTask`
  状态解析干净（关掉 `$ProgressPreference` 后不再被 CLIXML 噪声污染）；
* 计划任务不存在时监控台正确显示「不存在」，不会误报成「已停止」；
* PostgreSQL **两条源都跑通了全链路**（`tools/probe_pg.py` / `probe_pg_fallback.py`）：
  * 官方源（`full` 工具集，含 psql）：解压 → `initdb`（本机 62–90 秒）→ 起库 →
    建库 → 建角色 → 10 个迁移全 OK → **32 张表** → `forge_app` 能登录；
  * 备用源（`server` 工具集，**没有 psql / pg_dump**）：同一条链，SQL 由 venv 里的
    `psycopg` 逐条执行（自己切语句，认得 `$$...$$`、引号、注释），10 个迁移全 OK；
* 监控台的数据库判断改成「先问 `ZhituPostgreSQL` 服务状态、再看端口」：
  本机 5432 上跑着另一个 PostgreSQL，旧逻辑会误报「在监听」，现在正确显示
  「服务=未安装 监听=是 不可用」；
* 载荷自检 `verify_payload.py`：60 项不需要管理员/Docker/联网的静态断言全绿
  （文件齐不齐、是不是最新构建、模板占位符能否全部替换、GUI 与 `--auto` 步骤表
  是否同一份、缓存包名字是否和安装程序查找的一致）；
* Label Studio：`pip install label-studio==1.23.2` 在 Windows + Python 3.11 上
  能把依赖装完（中途撞 `WinError 5` 属于文件锁，重试即过 → 安装程序已内置重试）。

**未在真实服务器上验证**：

* `pg_ctl register` 注册 Windows 服务（本机非管理员，只验到 `pg_ctl start`）；
* 三个计划任务以 SYSTEM 身份启动并崩溃自愈（本机没有管理员权限，注册不了）；
* Caddy 在 80 端口接管后的完整链路（本机 80 空闲但没装到系统目录）；
* 从零到「三服务全绿」的完整一遍。

> 两个踩到的坑已经写进代码，别再改回去：
> 1. **`pg_ctl start` 不能用 `capture_output`**：postgres 会继承父进程的
>    stdout/stderr 管道，pg_ctl 退出后管道仍被它持有，`subprocess.run` 一直等
>    EOF，表现为"启动时卡死"。用 `run_daemon()`（DEVNULL）。
> 2. **`initdb` 在本机要 60–90 秒**（不是卡住）。别用 40 秒超时去判"失败"。
