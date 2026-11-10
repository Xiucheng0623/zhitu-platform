# 智途 · Windows 客户端（安装程序 + 监控台）

两个真正的 `.exe`，不是 `.bat`：一个负责把整套栈装起来，一个负责日常起停与看状态。
本地验收见文末「验证到哪一步」。

---

## 0. 一句话：服务器能上网就跑得通

| 你的服务器 | 装法 | 交付目录多大 | 首装耗时 |
|---|---|---|---|
| **Windows 11 + 能出外网** | 默认「联网构建」：现场拉基础镜像 + build 三个服务 | 约 25 MB | 20–40 分钟 |
| 内网 / 不能出外网 | 「离线镜像」：`docker load` 打包机导出的 tar | 约 2–3 GB | 5–10 分钟 |

两种模式的差别只在**镜像从哪来**，其余步骤完全一样。界面第 02 步上方有
「自动 / 联网构建 / 离线镜像」三个按钮，默认「自动」= 有镜像包就走离线，没有就联网。

---

## 1. 先说三个不能绕过的事实

先把话说清楚，省得装到一半才发现装不了。

| 事实 | 后果 | 出处 |
|---|---|---|
| **Docker Desktop 不支持 Windows Server** | 服务器上没法用 Desktop 一键装 | Docker 官方文档 + 订阅协议（Desktop 限个人电脑/开发工作站） |
| **Server 2019（build 17763）只有 WSL 1** | 跑不了 Linux 容器，只能升到 2022/2025 | 微软 WSL 版本矩阵 |
| **WSL 2 需要嵌套虚拟化** | 云主机多数没开 VT 透传 → 装上引擎也起不来 | `check-host.ps1` 会先判定 |

因此本客户端做的事是**编排与监护**，不是提供容器运行时：
它在 WSL 里准备 Docker Engine，把部署文件落盘、起栈、自检、开端口、建快捷方式。

如果第 01 步判定成 `BLOCKED`，那就是真跑不了 —— 不要硬装。
（注意：**没有 `wsl.exe` 不等于 BLOCKED**，那只是功能没启用，属于 `INSTALLABLE`。）

---

## 2. 交付目录长什么样

```
<发布目录>\
  智途安装程序.exe        安装向导（约 11 MB）
  智途监控.exe            监控台（约 11 MB）
  payload\
    manifest.json         版本 / 期望镜像清单 / build_sources / 每个 tar 的 sha256
    compose\
      docker-compose.yml  已把镜像 tag 定死（不留 ${ZHITU_TAG}）
      env.example         生成 .env 时的注释模板
      caddy\Caddyfile     入口反代配置（纯 IP 模板）—— 缺它 80 端口起不来
      migrations\*.sql    11 个必需迁移
      docker\*.Dockerfile 三个自建服务的构建定义
      src\web\**          前端源码（不含 node_modules，约 0.8 MB）
      src\orchestrator\** 编排层源码（requirements.txt + app/）
      src\labelstudio\brand\**  LS 品牌补丁与 SSO 中间件
    images\*.tar          ← docker save 产物（只有离线安装需要）
    docker\*.deb          ← 可选：离线装 Docker Engine 用
```

`payload\compose\` 就是安装后的 `C:\Zhitu\deploy\`，结构**必须**与仓库里
`platform\deploy\` 一致 —— compose 的 `build.context` 写的是 `.`，Dockerfile 按
`docker/…`、`src/web/…` 取文件，路径错一层 build 就报找不到文件。

**为什么不是单个巨型 exe**：镜像动辄 2–3 GB，塞进单文件后 FAT32 U 盘放不下、
每次升版要重打整包、杀软对超大单文件的处理不可预测。想给客户"一个文件"，用
7-Zip 自解压壳套这个目录即可，客户端逻辑不用改。

---

## 3. 怎么打出这套交付物

### 3.1 打 exe（任意 Windows 机器，需 Python 3.10+ 且带 tkinter）

```powershell
cd platform\deploy\windows-client
python -m pip install pyinstaller
python build_client.py --clean
```

产物在 `dist\`。`build_client.py` 会自动挑一个「有 tkinter 且有 PyInstaller」的解释器；
本机实测用 `C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe`（Tk 8.6）。
**别用没有 tkinter 的解释器打包**（如 Label Studio 的 venv），那样打出来的 exe 一开界面就 ImportError。

### 3.2 备载荷

只做联网安装（默认路线，服务器能上网）：

```powershell
python make_payload.py
```

要支持离线安装（另外必需一台**有 Docker 和网络**的机器）：

```powershell
python make_payload.py --build-images --save-images --tag 20261108
```

- `--build-images`：把 `web / orchestrator / labelstudio` 三个 build 型服务构建成
  `zhitu/<svc>:<tag>`（compose 里已给它们固定 image 名，否则 `docker save` 无从引用）；
- `--save-images`：把 8 个镜像全部 `docker save` 成 `payload\images\*.tar`。

### 3.3 校验（不需要 Docker，也不需要 WSL）

```powershell
python verify_payload.py
```

它验 33 项：载荷结构、payload→安装目录逐字节落盘、compose 里每个
`dockerfile:` / `context:` / bind mount 能否解析、`.env` 必填项与公开地址、
两种模式各自可行、GUI 与 `--auto` 的步骤表没漂移。

**它验不了什么**：镜像拉不拉得到、`npm ci`/`pip install` 会不会超时、容器能不能起来。
这三件事只有真机跑一次才知道。

### 3.4 组装发布目录

```
dist\智途安装程序.exe  →  <发布目录>\
dist\智途监控.exe      →  <发布目录>\
payload\               →  <发布目录>\payload\
```

联网安装：整个 `<发布目录>` 约 25 MB，微信/U 盘都行。
离线安装：另外把 `payload\images\*.tar` 一起带上（2–3 GB，单文件超 4 GB 时 FAT32 放不下）。

---

## 4. 安装：11 个步骤，每步都能单独重跑

| 步骤 | 做什么 | 失败时你会看到 |
|---|---|---|
| 01 环境体检 | 判 build 号 / WSL / 发行版 / 虚拟化，给 READY / INSTALLABLE / BLOCKED | 具体哪条不满足，以及能不能补救 |
| 02 载荷与安装方式 | 按「联网 / 离线」分别校验：核心项 + 源码树 / 核心项 + 镜像 tar | 缺哪个文件、缺的是哪种模式的 |
| 03 WSL 2 与发行版 | `wsl --install -d Ubuntu-22.04` | 需要重启时会明说 |
| 04 Docker Engine | 优先用 `payload\docker\*.deb` 离线装，没有才走 get.docker.com | 离线时要求补齐 deb |
| 05 镜像（离线载入） | 逐个 `docker load`；联网模式**跳过**并说明 | 哪个 tar 坏了 |
| 06 部署文件与随机密钥 | 整棵 `payload\compose` 落盘 + 生成 9 个随机密钥的 `.env` | 目录权限问题 |
| 07 启动容器栈 | 联网：`compose up -d`（现场 build）；离线：`up -d --no-build` | compose 的原始报错 |
| 08 健康检查 | 轮询 `:80`、`/healthz`、`:8100` 三个入口，最多 5 分钟 | 哪个入口没起来 |
| 09 端口映射与防火墙 | `netsh portproxy`（80 / 8100）+ 防火墙放行 | 哪条规则没建成 |
| 10 LS 长期凭证 | 登录 LS 取 refresh token 写回 `.env`，重启编排层 | 拿不到就给出补救入口 |
| 11 快捷方式与收尾 | 桌面快捷方式（监控台 + 平台网址） | 不影响使用 |

细节：

* **第 10 步是整套部署里最容易漏的一环**。LS 1.23 的 access token 只有 5 分钟，
  编排层靠 refresh token 续；留空时**服务照样全绿、网页照样能开**，但标注员标完之后
  平台数字不动。这一步不做就等于交付了一个"数不上去"的平台。
* **重装不换钥匙**：第 06 步发现已有完整 `.env` 会原样保留，只把两个公开地址刷成本次
  检测到的 IP。换密钥会把已登录的人全部踢下线，也会让 LS 的 refresh token 失效。
* **离线为何必须 `--no-build`**：离线环境 build 必然失败，而且失败点很深（3 分钟后
  pip 超时）。镜像缺了要立刻报错，不能让用户以为在装。联网模式反过来，必须允许 build。
* 命令行等价物：`智途安装程序.exe --auto`，可加 `--online` / `--offline` /
  `--host-ip 10.0.0.8`，输出同时写进 `%LOCALAPPDATA%\Zhitu\logs\cli-auto-<日期>.log`。
* **服务器地址**填的是**员工在自己电脑上能打开的那个地址**（局域网 IP 或域名）。
  填 127.0.0.1 的话，员工点"进标注工作台"会指向他自己的机器。

---

## 5. 监控台怎么用

界面把三层状态摊平，哪一层断了就指哪一层：

```
宿主机   WSL 发行版 / Docker Engine 版本
容器     每个 service 的 state / health / image
服务     HTTP 健康端点是否真的回 200（容器 running ≠ 服务可用）
```

按钮：

| 按钮 | 做什么 |
|---|---|
| 启动 / 停止 / 重启 | 对本 compose 项目执行 `up -d`（本地已有镜像时自动带 `--no-build`）/ `stop` / `restart` |
| 补凭证 | 重新登录 LS 取 refresh token 写回 `.env` 并重启编排层 —— 平台数字不跟标注动时按这个 |
| 修复网络 | 重写 80 / 8100 的端口映射。**WSL 的 IP 每次重启都会变**，局域网访问断了先按这个 |
| 打开平台 / 标注引擎 | 打开 `http://<服务器地址>/` 与 `http://<服务器地址>:8100/` |
| 备份 | 在 WSL 里 `pg_dump` 平台库到 `/opt/zhitu/backup/`（完整备份请用 `deploy/backup.sh`） |

自动刷新 5 秒一次（按钮可开关，显示为「自动刷新：开（5s）」），F5 立即刷新。
命令行：`智途监控.exe --status`（打印一次状态后退出，退出码 0 = 全部正常）。

---

## 6. 常见问题

| 症状 | 原因 | 处理 |
|---|---|---|
| 本机能开网页，局域网打不开 | 容器端口在 WSL 网卡上，Windows 没做 portproxy；或 WSL IP 变了 | 监控台点「修复网络」 |
| 平台 :80 打不开、caddy 反复重启 | `deploy\caddy\Caddyfile` 缺失（官方 caddy 镜像不带它） | 重新跑安装第 06 步；载荷里必须有 `caddy/Caddyfile` |
| 标注能标，但平台数字不动 | `.env` 里 `LS_REFRESH_TOKEN` 为空，回调断链 | 监控台点「补凭证」 |
| 第 04 步失败 | 离线机器没有 `payload\docker\*.deb`，又连不上 get.docker.com | 在有网机器下载 docker-ce 相关 deb 放进该目录 |
| 第 05 步失败 | 选了「离线镜像」但镜像 tar 缺失/损坏 | 改选「联网构建」，或回打包机重跑 `--save-images` |
| 第 07 步 build 报 `dockerfile not found` | `payload\compose` 少了 `docker/` 或 `src/` | 重跑 `make_payload.py`，再跑 `verify_payload.py` 应 33 项全过 |
| 计划任务里报 `distribution not found` | WSL 发行版是**按用户**注册的，SYSTEM 账户看不到 | 用注册 WSL 的同一账户 + 密码运行，或用 `-OnLogon` 触发 |
| exe 双击没反应 | 被安全软件拦截（`wsl.exe` 调用常被拦） | 加白名单；用 `--auto` 跑一次看日志定位 |

---

## 7. 已知限制（别当成已解决）

1. **Docker Desktop 不能在 Windows Server 上用** —— 这不是限制，是事实，见第 1 节。
2. **本仓库自带的两条 Windows 路径都没在真实 Server 上跑过全流程**：
   `windows-docker/`（WSL2 + Engine）与本目录（客户端）只做了语法/编码/分支验证；
   开发机上 `wsl.exe` 被安全策略拦截，`INSTALLABLE` 是能给出的最强结论。
3. **安装需要管理员**：写 `C:\Zhitu`、`netsh portproxy`、防火墙规则都要提权。
   未提权时向导会提示并尝试 `ShellExecuteW(runas)` 提权重启。
4. **联网构建依赖外网质量**：三个服务要拉 `node:22-alpine` / `python:3.13-slim` /
   `nginx` / LS 基础镜像，还要 `npm ci` 与 `pip install`。国内走 npmmirror + 阿里云
   PyPI（Dockerfile 默认值），海外要在 `.env` 里改成官方源。
5. **DPI 只验证了 100% 与约 150% 两档**：界面已开 DPI 感知（不再被系统位图拉伸），
   字号/行高/窗口尺寸按比例缩放且**不超出屏幕工作区**；多屏混档（如 100% + 200%）拖拽未测。
6. **监控台不常驻**：没有托盘守护，也不做自愈。要开机自启请用
   `windows-docker/register-autostart.ps1`。
7. **备份只含平台库**：对象存储（MinIO）里的图片/音频不在 `.exe` 备份范围内。

---

## 8. 本地验证到哪一步（诚实清单）

已实测：

* 两个 exe 均可启动 GUI，版面在 100% 与约 150% 缩放下截图核对（无裁切/重叠/乱码，
  底栏按钮在窗口变矮时不会被挤出屏幕）；
* 三种安装方式的分支实测：`自动`→联网、`联网构建`→联网、`离线镜像`→离线；
  离线且无镜像时第 02 步明确失败，联网时通过；
* `--selftest` 正确读出载荷目录、`manifest.json`、11 个迁移、caddy、构建源码；
* `verify_payload.py` **33/33 全绿**：含 payload→deploy 逐字节一致、
  3 个 `dockerfile:` + 3 个 `context:` + 2 个 bind mount 全部解析、
  `.env` 9 项随机密钥与两个公开地址、GUI 与 `--auto` 步骤表一致；
* `--status` 真实探三个入口（`:80` ×2、`:8100`）；WSL 不可用时正确报
  `WSL 未就绪` 且退出码为 1；
* 宿主判定在本机（wsl.exe 被安全策略拦）给出 `INSTALLABLE` + 待办两条，未误判成 `BLOCKED`；
* 控制台中文在 cmd / 管道 / 日志文件三种通道下均不乱码（含代码页切 65001）。

未实测（必须在真机上补）：

* 真实 Windows 11 / Server 2022 上从零到「三个服务全绿」的完整链路；
* 联网构建拉 `node:22-alpine` 等基础镜像 + `npm ci` + `pip install` 的实际耗时与失败率；
* `docker load` 8 个镜像（开发机没有 Docker CLI）；
* `netsh portproxy` 与防火墙规则的实际生效；
* 计划任务开机自启。
