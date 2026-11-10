# 在 Windows 服务器上用 Docker 跑 FORGE

目标：在 **Windows Server 2022 / 2025** 上，用**一套 docker compose** 把平台跑起来
（Caddy + 前端 + 编排层 + PostgreSQL×2 + Label Studio + MinIO），和 Linux 服务器完全同一份编排。

---

## 0. 先接受三个事实（不承认这三点会白折腾半天）

1. **Docker Desktop 不支持 Windows Server。** Docker 官方文档写得很直白：Desktop 只支持
   Windows 10/11 客户端；订阅协议也把它限定在「个人电脑 / 开发工作站」，不是服务器。
   所以别在服务器上装 Desktop。
2. **Windows Server 上跑 Linux 容器只有一条路：WSL 2 里装 Docker Engine。**
   现有 compose 里的镜像（`postgres:17-alpine`、`caddy`、`minio`、LS）全是 Linux 镜像，
   Windows 容器运行时跑不了它们；LCOW 早已废弃。
3. **WSL 2 需要嵌套虚拟化。** 云主机上这一项经常不给（低价套餐普遍没有 VT 透传）。
   没有它，`wsl --install` 能装完，但发行版起不来，Docker 也就无从谈起。

所以**第一步永远是检测，不是安装**。

---

## 1. 三条路线，怎么选

| 路线 | 适用 | 好处 | 代价 |
|---|---|---|---|
| **A. WSL 2 + Docker**（本目录） | Server 2022/2025；物理机或支持嵌套虚拟化的云主机 | 与 Linux 部署 100% 同一套 compose；`backup.sh` / `restore.sh` / `smoke.sh` 直接复用；升级只需重建镜像 | 多一层 WSL 虚拟机；内存 8 GB 起（建议 16 GB）；WSL 的 ext4.vhdx 只增不减，要定期压缩 |
| **B. 裸机**（`windows/install.ps1`） | 任意 Windows Server，**包括没有嵌套虚拟化的云主机** | 不依赖虚拟化；内存占用低 | 要在 Windows 上装 Python 3.11+ / PostgreSQL 17；Label Studio 跑在 venv 里；升级要逐台同步文件 |
| **C. 换 Linux 服务器** | 还没定服务器 | 最省事、最稳、社区验证最多 | 需要采购或迁移 |

**判断顺序**：先跑 `check-host.ps1`，它直接告诉你走 A 还是 B。

---

## 2. 前置

| 项目 | 要求 | 说明 |
|---|---|---|
| Windows | Server 2022（build 20348+）或 Server 2025；或 Win10 19041+/Win11 | Server 2019 只有 WSL 1，**不能**跑 Docker |
| 虚拟化 | BIOS 开 VT-x/AMD-V；云主机需支持嵌套虚拟化 | `check-host.ps1` 会看 `HypervisorPresent` |
| 内存 | 8 GB 起，建议 16 GB | WSL 默认会占 50% 内存，脚本按「总内存-4G」封顶 |
| 磁盘 | 40 GB 起（镜像约 6 GB，数据另计） | 放在数据盘 |
| 权限 | Administrator | 装功能、写 portproxy、注册计划任务都要 |
| 网络 | 能访问 `get.docker.com` / `download.docker.com` / Microsoft CDN | 内网见 §7 离线安装 |

---

## 3. 部署步骤

把部署包解开，假设落到 `D:\Forge\deploy`。**以下命令都在「管理员 PowerShell」里执行。**

### 3.1 检测

```powershell
cd D:\Forge\deploy\windows-docker
.\check-host.ps1 -OutFile D:\Forge\host-check.txt
```

三种结论：

| 结论 | 退出码 | 下一步 |
|---|---|---|
| `READY` | 0 | 已有可用 Docker，直接 §3.3 |
| `INSTALLABLE` | 2 | 跑 §3.2 |
| `BLOCKED` | 1 | **停**，改走 `windows\install.ps1`（裸机），别硬装 |

### 3.2 安装 Docker（WSL 2 里）

```powershell
.\install-docker-wsl.ps1                # 默认 Ubuntu-22.04
.\install-docker-wsl.ps1 -Mirror Aliyun # 国内网络走镜像
```

- 会启用 `Microsoft-Windows-Subsystem-Linux` 与 `VirtualMachinePlatform`；
  如果提示退出码 `3010`，**重启后把同一条命令再跑一遍**。
- 装完会做 `docker run --rm hello-world` 验证。看到 `Hello from Docker!` 才算成功。
- 发行版注册在**当前账户**名下 —— 这条很重要，后面自启任务必须用同一个账户。

### 3.3 填 .env

```powershell
cd D:\Forge\deploy
copy .env.example .env
notepad .env
```

必填项（其余可留默认）：

| 变量 | 说明 |
|---|---|
| `DOMAIN` | 域名；没有域名就先填服务器 IP，用 IP 模式 |
| `ACME_EMAIL` | 真实邮箱（域名模式必须，否则 Caddy 申请证书失败） |
| `ORCH_DB_PASSWORD` / `LS_DB_PASSWORD` / `MINIO_PASSWORD` / `LS_ADMIN_PASSWORD` | 强口令，别用 `change_me_*` |
| `LS_SSO_SECRET` / `LS_WEBHOOK_SECRET` | 随机串，`install.sh` 会自动生成；手工填也要够长 |
| `LS_REFRESH_TOKEN` | 见 §3.4，必须拿到，否则平台读不到 LS 数据 |

> `.env` 里的换行建议用 **LF**。Windows 记事本默认 CRLF，compose 多数情况能容忍，
> 但 `printf` 生成的密钥带 `\r` 会让 LS token 校验失败。用 `notepad++` 或
> `Set-Content -NoNewline` 更稳。

### 3.4 拿 Label Studio 的 refresh token

这一步和 Linux 一样，只是要在 WSL 里执行（`fetch_ls_refresh.py` 依赖 requests）：

```powershell
# 先只起 LS 和它依赖的库
.\windows-docker\forge-stack.ps1 -Action start -NoPortProxy

# 等 LS 起来（约 1 分钟），再取 token
wsl -d Ubuntu-22.04 -u root -- bash -lc "cd /mnt/d/Forge/deploy && python3 tools/fetch_ls_refresh.py --email <LS_ADMIN_EMAIL> --password <LS_ADMIN_PASSWORD>"
```

把输出的 token 填进 `.env` 的 `LS_REFRESH_TOKEN`，然后 `-Action restart`。

### 3.5 启动

```powershell
.\windows-docker\forge-stack.ps1 -Action start -StackDir D:\Forge\deploy
```

`start` 会自动做两件事：`docker compose up -d`，然后**刷新 portproxy**（见 §4）。

### 3.6 验收

```powershell
.\windows-docker\forge-stack.ps1 -Action smoke
```

`smoke.sh` 里那些断言（webhook 回调地址、`USE_DEFAULT_BANNED_SUBNETS=false`、
LS 容器能否直连编排层、定时对账、权限自检、数据行指纹）在容器里同样适用。

人工再走一遍：

1. 浏览器打开 `https://<域名>` 或 `http://<服务器IP>`，用管理员登录；
2. 建项目 → 绑定 LS 项目 → 「标注界面」能打开设计器；
3. 标注员账号登录 → 「我的任务」看到真实数据行 → 点未标注行进标注流、已标注行看结果；
4. 在 LS 提交一条标注 → 平台 1–2 秒 +1；删掉 → 回退；
5. 查 `forge-stack.ps1 -Action logs -Service labelstudio`，不应出现
   `reserved network address`。

### 3.7 开机自启

```powershell
.\windows-docker\register-autostart.ps1 -StackDir D:\Forge\deploy
```

会要你输入当前账户的密码（任务要「不管用户是否登录都运行」）。不想存密码就用
`-OnLogon`：账户登录后才拉起，适合常年保持登录的服务器。

任务每 5 分钟重跑一次 `compose up -d`（幂等）：重启时 WSL 还没就绪导致第一次失败，
下一次自动补上；容器意外退出也会在 5 分钟内被拉回来。

---

## 4. 为什么必须 portproxy（最容易漏的一步）

WSL 2 里的容器把 80/443 发布在**WSL 虚拟机的网卡**上：

- Windows 本机访问 `http://localhost` → 通（WSL 自带 localhost 转发）；
- **局域网/公网访问 `http://<服务器IP>` → 不通**，因为 Windows 这一侧没有监听。

`sync-portproxy.ps1` 就是补这个洞：

```powershell
netsh interface portproxy add v4tov4 listenport=80 listenaddress=0.0.0.0 \
       connectport=80 connectaddress=<WSL的IP>
```

**WSL 的 IP 每次重启都会变**，所以这条规则不能只写一次 —— `forge-stack.ps1 -Action start`
每次都会重写它，开机自启任务也因此会顺带修好。

验证：

```powershell
netsh interface portproxy show v4tov4
```

在**另一台机器**上 `curl http://<服务器IP>/` 能拿到页面，才算对外真的通了。

---

## 5. 日常运维

| 想做什么 | 命令 |
|---|---|
| 看状态 | `.\forge-stack.ps1 -Action status` |
| 看某服务日志 | `.\forge-stack.ps1 -Action logs -Service orchestrator -Tail 200` |
| 重启 | `.\forge-stack.ps1 -Action restart` |
| 停止（保留数据卷） | `.\forge-stack.ps1 -Action stop` |
| 升级（重建镜像） | 覆盖 `src/`、`migrations/` 后 `.\forge-stack.ps1 -Action update` |
| 备份 | `.\forge-stack.ps1 -Action backup`（在 WSL 里跑 `backup.sh`） |
| 看生效配置（脱敏） | `.\forge-stack.ps1 -Action env` |
| 进容器前的 shell | `.\forge-stack.ps1 -Action shell` |

备份产物在 `D:\Forge\deploy\backups\forge-<时间戳>\`（WSL 里是 `/mnt/d/...`），
含 `.env.bak`。恢复同样回 WSL 里执行 `bash ./restore.sh <目录>`，
跨机恢复记得加 `--env=backup`，否则 SSO/LS token 对不上。

---

## 6. 数据在哪

| 内容 | 位置 |
|---|---|
| PostgreSQL / LS / MinIO 数据 | Docker 命名卷，在 WSL 的 `ext4.vhdx` 里 |
| 代码 + `.env` + backups | `D:\Forge\deploy` |
| WSL 磁盘文件 | `%LOCALAPPDATA%\Packages\CanonicalGroupLimited*\LocalState\ext4.vhdx`（几十 GB 很常见） |

`ext4.vhdx` **只增不减**。清完数据后压缩：

```powershell
wsl --shutdown
Optimize-VHD -Path "$env:LOCALAPPDATA\Packages\CanonicalGroupLimited*\LocalState\ext4.vhdx" -Mode Full
# 没有 Hyper-V 模块时用：diskpart -> select vdisk file="..." -> compact vdisk
```

---

## 7. 离线安装（内网无外网）

在有网的机器上准备，然后拷到服务器：

```bash
# 1) 导出镜像（版本号以 docker-compose.yml 为准）
docker pull postgres:17-alpine postgres:16-alpine caddy:2.8 \
  minio/minio:RELEASE.2024-10-13T13-34-11Z minio/mc:RELEASE.2024-10-08T09-37-26Z
docker save -o forge-images.tar postgres:17-alpine postgres:16-alpine caddy:2.8 \
  minio/minio:RELEASE.2024-10-13T13-34-11Z minio/mc:RELEASE.2024-10-08T09-37-26Z
```

服务器上：

```powershell
wsl -d Ubuntu-22.04 -u root -- docker load -i /mnt/d/Forge/forge-images.tar
```

平台自己的两个镜像（`web` / `orchestrator`）由 `docker compose up -d --build` 在本地构建，
需要宿主机能从 npm/PyPI 拉依赖；完全离线的内网需要预先准备 npm 缓存与 wheel 离线包，
这部分没有现成脚本 —— 属于额外工作量，别默认它能离线跑。

WSL 发行版本身也需联网下载（Microsoft CDN）。内网可离线导入 `.appx`：

```powershell
Add-AppxPackage .\Ubuntu_2204.0.10.0_x64.appx
```

---

## 8. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `wsl --install` 成功但发行版起不来 | 没有嵌套虚拟化 | `check-host.ps1` 会提前判定。只能换支持嵌套虚拟化的机器，或走裸机 |
| `WslRegisterDistribution failed with error 0x80370102` | 同上（VT 未开/未透传） | BIOS 开 VT；云主机找厂商开嵌套虚拟化 |
| 计划任务报「distribution not found」 | 任务跑在 SYSTEM 下，看不到按用户注册的发行版 | 用 `register-autostart.ps1` 默认方式（同一账户 + 密码），别用 SYSTEM |
| 本机 `localhost` 能开、别的机器打不开 | 缺 portproxy | `.\sync-portproxy.ps1`；确认防火墙放行 80/443 |
| 重启后网站 502 | WSL IP 变了，portproxy 指向旧 IP | 重跑 `forge-stack.ps1 -Action start`（会重写规则）；自启任务也会修 |
| `docker: command not found`（Windows 侧） | 正常：Docker 在 WSL 里 | 用 `forge-stack.ps1`，别直接敲 `docker` |
| 容器里写文件权限错 | `/mnt/d` 是 9p 文件系统，不保留 POSIX 权限 | 数据走命名卷（默认就是）；脚本用 `bash ./x.sh` 显式调用，别依赖 `+x` |
| `ext4.vhdx` 涨到几十 GB | WSL 磁盘不自动回收 | §6 的压缩命令 |
| 时间漂移导致 JWT/证书异常 | 虚拟机休眠后时钟不同步 | `w32tm /resync`；确保 Windows 时间服务在跑 |
| 内存被吃满 | WSL 默认最多占 50% 内存 | `%USERPROFILE%\.wslconfig` 里调 `memory=`；`wsl --shutdown` 生效 |

---

## 9. 卸载

```powershell
# 1) 删自启任务
.\register-autostart.ps1 -Unregister

# 2) 停栈（数据卷保留）
.\forge-stack.ps1 -Action stop

# 3) 删容器与卷（不可逆，先备份）
wsl -d Ubuntu-22.04 -u root -- bash -lc "cd /mnt/d/Forge/deploy && docker compose down -v"

# 4) 删 portproxy 规则
foreach ($p in 80,443) { netsh interface portproxy delete v4tov4 listenport=$p listenaddress=0.0.0.0 }

# 5) 注销发行版（会删掉 WSL 里所有数据）
wsl --unregister Ubuntu-22.04
```

---

## 10. 已知限制（不粉饰）

- **WSL 2 这层不是官方推荐的生产形态。** 微软自己的文章把它定位为「开发与测试」。
  生产要求再高一档时，换 Linux 才是正解。
- **本目录的脚本没有在这台开发机上端到端跑过**：这台机器没有装 Docker，
  且 `wsl.exe` 被安全策略拦，无法执行。脚本的语法、编码、判定逻辑都验过，
  但「在真实 Server 2022 上从零到 smoke 全绿」这一步必须由你在服务器上完成，
  第一次跑请留出时间看输出，别当成一键安装。
- 内存与磁盘开销比裸机方案高（多一层 VM），8 GB 的机器会很紧。
- WSL 的发行版跟着账户走：换账户、改密码、把机器加入域都可能影响自启任务。
