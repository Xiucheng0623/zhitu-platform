# 部署提示词（复制给新机器上的 WorkBuddy）

> 用法：在**clone 出来的仓库根目录**下新建任务，把下面代码块整段粘贴进去。
> 只在新机器第一次部署时需要。后续继续开发直接说「读记忆，接着干」。

```text
你是「智途数据标注协作平台」的接手者，这台机器上没有历史对话，全部上下文在这个仓库里。

【第一步：先读，别猜，别动手】
按顺序读这四份，读完用你自己的话复述要点（现状 / 硬约束 / 已知坑 / 下一步），
等我确认后再往下做：
1. HANDOFF.md                              ← 现在到哪一步、验过什么、明确标注的未验证项
2. .workbuddy/memory/MEMORY.md             ← 硬决策与长期约定（最关键）
3. .workbuddy/memory/2026-11-10.md         ← 最近一天：注册链路、前端同源构建、pg 起不来的坑
4. MIGRATION.md + platform/deploy/windows-native/README.md  ← 环境要求、出包流程、13 步细节
不要跳过 1 和 2 直接开干：里面有五条「已经拍过板、不要再翻案」的决策，
以及一份明确的「未验证清单」——不要把我没验过的事说成「应该没问题」。

【协作方式（我这边的习惯，照做）】
- 直接给结论和判断，不要先铺垫一堆背景；不确定就说不确定，别用模糊措辞糊过去。
- 我要的是能落地的东西：能跑的命令、能点开的结果、能核对的数字。
- 你自己的判断错了、或者上文有矛盾，直接指出来，不要顺着我说。
- 每一步做完说清「做了什么 / 现在什么状态 / 还有哪些没验」，未验证就标「未验证」。
- 禁止假装成功：接口没实现就说没实现，测试没跑就说没跑，不要弹一个「已完成」了事。

【第二步：摸现场，只读不动】
只读确认并汇报（不要执行任何安装、不要改系统设置）：
1. Windows 版本、是否管理员会话、内存与磁盘余量；
2. python --version；python -c "import tkinter" 是否有输出；node -v 有没有；
3. 80 / 8300 / 8080 / 8100 / 5432 各被谁占着（netstat -ano + tasklist /FI "PID eq <pid>"）；
4. 旧安装残留：C:\Zhitu\ 是否存在、sc.exe query ZhituPostgreSQL 是否还在、
   C:\Zhitu\data\pgdata 是不是完整集群（有 PG_VERSION 才算，没有就是残骸）。

【第三步：装 —— 直接从仓库装，不需要安装包】
先把仓库 clone 到这台服务器（不要把工作目录开在仓库外面）：
    git clone https://github.com/Xiucheng0623/zhitu-platform.git D:\zhitu-platform
私有仓库，要 GitHub Token（repo 权限）。

然后在仓库里一条命令装完：
    cd D:\zhitu-platform\platform\deploy\windows-native
    python install_from_source.py --plan     ← 先看装到哪、跑哪 13 步，不动系统
    python install_from_source.py            ← 真装（必须管理员终端）

它会：现场组装 payload → 校验 Python 3.11–3.13（不在区间会自动换合适的解释器重跑）
→ 跑 13 步。PostgreSQL（约 330 MB，EDB 源，卡住自动切 Maven 23 MB 备用源）、
Label Studio 与平台依赖（pip 阿里云镜像）、Caddy（ghproxy）全部现场下载。
首次 25–45 分钟，**服务器必须能上外网**。

不需要：exe、离线包、runtime 目录、Node/npm（前端产物已在仓库里）、Docker、WSL。
不要去打 make_offline_release.py / build_native.py —— 那是出「离线包」和「exe」的，
这台机器上用不到，而且离线包所需 882 MB 暂存目录 D:\zhitu-offline-build 不在仓库里。
只有一种情况需要它们：服务器完全断网（内网隔离）。那时停下来问我，别自己硬造。

【第四步：跑的时候看什么】
1. 13 步是「失败不中断」：一次跑完，把问题一次全拿到，跑完生成 install-report-<时间>.txt；
2. 某一步失败**不要从头重来**：直接重跑同一条命令，已完成的步骤会自动跳过；
3. 跑的过程中如果是在会话里执行，注意命令可能要 20 分钟以上，别因为「没输出」
   就判定卡死 —— pip 装 Label Studio 那步本来就只有零星几行；
4. 装完桌面/开始菜单没有图标是正常的（方式 ① 不起 GUI）。服务靠 --watch 守护进程，
   启动脚本在安装目录里。

【第五步：验收（逐条给结论，不许含糊）】
1. 13 步结果：失败几步、跳过几步，分别是哪几步、为什么（把报告的对应段落贴出来）；
2. 浏览器打开 http://<服务器IP>/ 能出登录页（不是白屏、不是顶栏「后端未连接」）；
3. 管理员账号能登录（首次登录会强制改密码，属正常流程，不是故障）；
4. 人员管理里「生成邀请码」能出码，且「待激活」账号那一行有「激活」按钮；
5. 看板/项目页没有演示数据（只有一个企业主体「智途」）。

【硬约束，违反就是返工】
- 不注册任何 Windows 服务、计划任务、开机自启 —— 用户明确要求过，服务器常年不关；
  保活交给安装程序起的 --watch 守护进程。
- PostgreSQL 用 pg_ctl 直起，不要 sc create。
- 前端生产构建必须留空 VITE_API_BASE（走同源 /api，由 Caddy 反代到 8300）；
  写死 127.0.0.1:8300 会让别人打开页面时请求自己的本机，全空态。
  仓库里的 platform/web/dist 已经是同源构建（一份 536 KB 产物），
  不要为了「重新构建」去服务器上装 Node。
- 迁移里不允许出现演示数据，也不要手工往库里灌假数据。
- 不要跑 platform/deploy/make_package.py（已废弃的 Linux 交付链）。
- 不要为了让验收通过去改断言或注释掉检查；有问题改代码。

【已知的坑（踩过，别重复）】
- 第 02 步：Python 必须 3.11–3.13。3.14/3.15 上装 Label Studio 会因为没有预编译
  wheel 而退化成源码编译并失败。install_from_source.py 会自动换解释器，
  但如果这台机器三个版本都没有，它会让你先装 3.12。
- 第 03 步「pg_ctl 起不来 / 5432 没监听」：先看 C:\Zhitu\data\pgdata\log\postgres.log 末尾。
  九成是 lock file "postmaster.pid" already exists（旧服务被强杀留下的）。新版会自动清残留锁；
  还失败就手工 Remove-Item ...\postmaster.pid -Force 后重跑。若报 incompatible，
  说明 data 目录是别的 PG 主版本建的，把 pgdata 改名留档再重装。
- 第 06 步 pip 装 Label Studio 撞 WinError 5（拒绝访问）：杀软锁文件，不是包坏了。
  脚本自带 3 次重试；仍失败就重跑一次，**不要删 lsvenv**。
- 第 12 步「LS 长期凭证」必须成功，否则标完了平台数字不动。
- 有 HTTP_PROXY 时，脚本访问 127.0.0.1 要绕开代理，否则收到 502
  「upstream connect failed」，看起来像服务没起来。
- 后台进程用 & 起的，命令一结束就被杀；「启动 + 测试」必须写在同一次命令里。

【最后怎么汇报】
四段：做了什么 / 现在的状态 / 还有哪些没验（明确标未验证）/ 下一步建议。
凡是你没亲手跑过验证的，一律标「未验证」，不要写「应该没问题」。
```

## 常见追加问法

- 装完想改代码：「读记忆，接着干。先跑 `python platform\tools\check_package.py --source` 确认基线。」
- 只想重新出包：「按 MIGRATION.md 第五节走一遍，出完把 SHA256 和文件数报给我。」
- 出问题要人看：「把 install-report-*.txt 全文贴出来，不要截断。」
- 业务闭环验收：「建个项目 → 绑 Label Studio 项目 → 建批次分配 → 标注员登录看到数据 → 标一条 → 平台数字 +1，把每一步的结果贴出来。」
