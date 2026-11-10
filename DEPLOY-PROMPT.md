# 部署提示词（复制给新机器上的 WorkBuddy）

> 用法：在**clone 出来的仓库根目录**下新建任务，整段粘贴下面代码块里的内容。
> 只在新机器第一次部署时需要；后续继续开发直接说「读记忆，接着干」即可。

```text
你是「智途数据标注协作平台」的新接手者。先把背景读进去，再动手部署。

【第一步：读记忆，别猜】
依次读这几份文件，读完再用你自己的话总结一遍要点，确认我认可后再往下做：
- .workbuddy/memory/MEMORY.md          （硬决策与长期约定，最关键）
- .workbuddy/memory/2026-11-10.md      （最近一天：注册链路、前端同源构建、pg 起不来的坑）
- MIGRATION.md                          （环境要求、出包流程）
- platform/deploy/windows-native/README.md （13 步安装流程的技术细节）
不能跳过 MEMORY.md 直接开干 —— 里面有五条「已经拍过板、不要再翻案」的决策。

【第二步：摸清现场，只读不动】
以只读方式确认，并把结论告诉我（不要执行任何安装动作）：
1. 这台机器是不是 Windows、是不是管理员会话、系统版本；
2. Python 有没有（`python --version`）、有没有 tkinter（`python -c "import tkinter"`）；
3. Node 有没有（`node -v`）；
4. 80 端口、8300、8080/8100、5432 分别被谁占着（`netstat -ano` + `tasklist`）；
5. 有没有旧的安装残留：`C:\Zhitu\`、服务 `ZhituPostgreSQL`（`sc.exe query ZhituPostgreSQL`）、
   以及 C:\Zhitu\data\pgdata 是不是一个完整集群（有 PG_VERSION 才算）。

【第三步：找到安装包 —— 这是最容易卡住的地方，先问清楚】
完整离线包 `智途-Windows完整离线-*.zip`（约 366 MB）**不在 Git 仓库里**（.gitignore 排除了 *.zip），
仓库里只有源码。所以：
- 如果这台机器上已经有这个 zip（搜一下 D:\ 和桌面），直接用它；
- 如果没有，**停下来问我**包在哪，不要试图在服务器上从源码重新构建离线包 ——
  那需要 882 MB 的运行时暂存目录 D:\zhitu-offline-build（pgsql / py-ls / py-orch / caddy），
  它也不在仓库里，缺了它 make_offline_release.py 会直接失败，白折腾。

【第四步：安装】
1. 把 zip 完整解压（不要只解出 exe，runtime 和 payload 必须在一起）；
2. 右键「智途安装程序（原生）.exe」→ 以管理员身份运行；
3. 走完 13 步。中途若某一步失败，**不要重来一遍**：
   程序是「失败不中断」，跑完后会生成安装报告 install-report-<时间>.txt，
   直接跑第二次，已完成的步骤会自动跳过。

【第五步：验收（必须逐条给结论，不许含糊）】
1. 13 步是否全绿（失败/跳过各几步、分别是哪几步、为什么）；
2. 浏览器打开 http://<服务器IP>/ 能出登录页（不是白屏、不是「后端未连接」）；
3. 用管理员账号能登录（首次登录会强制改密码，属正常）；
4. 人员管理里能「生成邀请码」，且「待激活」账号有「激活」按钮；
5. 看板/项目页没有演示数据（只有一个企业主体「智途」）。

【硬约束，违反就是返工】
- 不要注册任何 Windows 服务、计划任务、开机自启 —— 用户明确要求过，服务器常年不关，
  保活交给安装程序起的 --watch 守护进程（桌面有「启动智途.bat」）；
- PostgreSQL 用 pg_ctl 直起，不要 `sc create`；
- 前端生产构建必须留空 VITE_API_BASE（同源 /api，由 Caddy 反代到 8300），
  写死 127.0.0.1:8300 会导致别人打开页面全空态；
- 迁移里不允许出现演示数据，也不要手工往库里灌假数据；
- 不要跑 platform/deploy/make_package.py（那是已废弃的 Linux 交付链）。

【已知会踩的坑（我踩过，别重复）】
- 第 03 步「pg_ctl 起不来 / 5432 没监听」：先看 `C:\Zhitu\data\pgdata\log\postgres.log` 末尾，
  九成是 `lock file "postmaster.pid" already exists`（旧服务被强杀留下的）。
  新版安装程序已会自动清死进程的残留锁；如果还失败，手工：
  `Remove-Item C:\Zhitu\data\pgdata\postmaster.pid -Force` 后重跑。
  若是 `database files are incompatible`，说明 data 目录是别的 PG 主版本建的，
  把 `pgdata` 改名留档再重装。
- 第 12 步「LS 长期凭证」必须成功，否则标注玩完平台数字不动。
- 沙箱/本机若有 HTTP_PROXY，访问 127.0.0.1 的脚本要绕开代理，否则会收到 502
  「upstream connect failed」，看起来像服务没起来。

【最后】
把「做了什么、现在的状态、还有哪些没验、下一步建议」四段写清楚给我。
凡是没能亲手验证的（比如必须真机才有结果的），明确标注「未验证」，不要写成「应该没问题」。
```

## 常见追加问法

- 装完之后想改代码：「读记忆，接着干。先跑 `python platform\tools\check_package.py --source` 确认基线。」
- 只想重新出包：「按 MIGRATION.md 第五节走一遍，出完包把 SHA256 和文件数报给我。」
- 出问题要人看：「把 install-report-*.txt 全文贴出来，不要截断。」
