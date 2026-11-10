#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 dist/ 里打好的两个 exe 收成一个**单文件交付包**。

为什么要有这一步
----------------------------------------------------------------------------
之前交付是「两个 exe + 一个 payload 目录」，拷到服务器要整目录拷，少一个文件
就装不了。现在 payload 已经内嵌进安装程序 exe（`build_native.py` 的 --add-data），
所以整份交付就是**两个独立 exe**；再压成一个 zip，发过去就一个文件。

产物
----------------------------------------------------------------------------
    windows-native/release/智途安装程序（原生）.exe   自包含
    windows-native/release/智途监控（原生）.exe
    windows-native/release/安装说明.txt
    platform/deploy/dist/智途-Windows原生-<日期>.zip  ← 只发这个

用法
----------------------------------------------------------------------------
    python make_release.py                 # 用 dist/ 里现有 exe
    python make_release.py --build         # 先重新打包 exe 再收
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
RELEASE = HERE / "release"
OUT_DIR = HERE.parent / "dist"          # platform/deploy/dist
STAMP = time.strftime("%Y%m%d")

SETUP = "智途安装程序（原生）.exe"
MONITOR = "智途监控（原生）.exe"

README_TXT = """智途 · Windows 原生版（不依赖 Docker / 不碰 WSL）
=================================================

一、怎么装（全部在这台服务器本机完成，不需要远程）
-------------------------------------------------
1. 把压缩包里的整个「智途」文件夹解压到服务器任意目录（例如 D:\\智途\\）。
   **必须整个文件夹一起解压**：exe 旁边的 payload 与 runtime 两个文件夹是
   运行时的本体，只把 exe 单独拷出来会装不上。
2. 右键「智途安装程序（原生）.exe」→ 以管理员身份运行。
3. 填「服务器地址」（默认填了本机局域网 IP）和「管理员手机号」→ 开始安装。
4. 等 13 步走完，全程约 5-10 分钟（大半时间在把运行时复制到 C:\\Zhitu）。
5. 装完桌面会出现「智途平台」快捷方式，双击即可用。

命令行等价物（在服务器本机执行）：
    .\\智途安装程序（原生）.exe --auto --admin-phone 13800138001 --admin-name 张伟
    .\\智途安装程序（原生）.exe --auto --no-download        （断网安装）
    .\\智途安装程序（原生）.exe --selftest                  （只体检，不改系统）

二、需要什么
-------------------------------------------------
* Windows 10 / 11 或 Server 2016 以上；管理员权限。
* 内存 ≥ 4 GB（推荐 8 GB）：Windows 系统自身要占 1.5-2 GB，再跑数据库 +
  标注引擎 + 编排层。低于 4 GB 会被系统杀掉进程。
* 磁盘剩余 ≥ 8 GB（安装目录与数据目录默认在 C:\\Zhitu）：运行时约 1.6 GB，
  再加数据库、标注数据与备份，留 8 GB 更稳。
* **不需要装 Python、不需要 pip、不需要下载 PostgreSQL、不需要联网。**
  本包已自带 PostgreSQL 17.2、Python 3.13、标注引擎与平台的全部依赖、
  以及 Caddy 入口反代；安装程序会把它们从 runtime 铺到安装目录，
  这一步就是「复制文件」，没有编译、没有联网下载。
* 装完要能连上（浏览器打开 http://<服务器IP>/），所以服务器要能被访问到。

【以下是给「在线版」看的，完整离线包直接跳过这一段】
在线版才需要 cache 预置：在本目录建一个 cache 文件夹，把下面两个包放进去，
安装时就不联网：
    cache\\postgresql-17.2-1-windows-x64-binaries.zip    （官方包，约 330 MB）
    cache\\caddy_2.8.4_windows_amd64.zip                 （约 16 MB）
官方 PostgreSQL 包下载不到时，也可以只放这个 23 MB 的备用包（只有服务端）：
    cache\\embedded-postgres-windows-amd64-17.2.0.jar
（备用包没带 psql/pg_dump，安装程序会自动改用 Python 建库、备份走冷备。）

三、装完怎么管
-------------------------------------------------
* 状态/起停：双击「智途监控（原生）.exe」，或命令行 --status（退出码 0 = 全正常）。
  监控台里有「全部启动 / 全部停止 / 重启编排层」三个按钮。
* **没有注册任何系统服务或计划任务**。装完就直接跑，服务器关机重启后
  双击监控台的「全部启动」即可恢复（也可以直接跑桌面上的「启动智途」）。
  这样做是因为这台机器常开，注册服务/任务只会多出管理员权限、组策略、
  CIM 通道这些麻烦。
* 保活：安装程序最后会起一个守护进程（`智途安装程序（原生）.exe --watch`），
  每 5 秒检查一次，谁掉了就拉起来，日志写 <数据目录>\\logs\\watch-*.log。
  监控台最后一行「守护进程」显示 Running 就说明它在。
* 地址：平台 http://<服务器IP>/   标注引擎 http://<服务器IP>:8100/
* 日志：<数据目录>\\logs\\（默认 C:\\Zhitu\\data\\logs）
* 备份：**每天凌晨 03:00 自动备份**（由守护进程触发），
  存到 <数据目录>\\backup\\，保留最近 7 份，更早的自动删掉。
  机器凌晨关着也没关系，开机后会补跑一次（当天没备份过就补）。
  想马上备一份（比如升级前）：
      cd C:\\Zhitu\\app\\services\\orchestrator
      C:\\Zhitu\\app\\venv\\Scripts\\python.exe -m tools.backup_db --keep 7
  备份是否成功看 <数据目录>\\logs\\backup.log，最后一行应是「完成：...（xx MB）」；
  报错看 backup.log.err.log。备份文件不到 1 KB 会在日志里写 [警告]，务必人工确认。
  注意：日志/文件都在，但**没有自动化恢复**，恢复要人工按文档做。
* 重装/升级：再跑一次安装程序即可，已有密钥会保留（不会把人踢下线）。

四、常见问题
-------------------------------------------------
* 出任何问题，先把日志发过来，别截图（截图经常正好截掉最关键的几行）：
  装完之后：<数据目录>\\logs\\install-gui-<日期>-<时分秒>.log
  还没装完：C:\\Users\\<你的账号>\\AppData\\Local\\Zhitu\\logs\\install-gui-*.log
  安装程序第 01 步会把这次的日志路径直接打印在日志区里，照那条找最准。
* 双击 exe 没反应 / 一闪就没了 / 被杀毒软件删掉：
  单文件 exe 会先解包到 %TEMP% 再运行，360、火绒、EDR 和 Defender 的
  「勒索软件防护」经常把这个行为当可疑动作拦掉。装之前先把这两个 exe
  加入白名单（或临时关掉实时防护），装完再打开也行。
  命令行执行能看到具体报错：.\\智途安装程序（原生）.exe --selftest
* 服务器上打得开、同事的电脑打不开：Windows 防火墙没放行入站。
  安装程序第 09 步会自动加规则（规则名「智途 平台入口 80」「智途 标注引擎 8100」）。
  如果是组策略托管的机器会被拒，手工加一次：
      netsh advfirewall firewall add rule name="智途 平台入口 80" dir=in action=allow protocol=TCP localport=80
      netsh advfirewall firewall add rule name="智途 标注引擎 8100" dir=in action=allow protocol=TCP localport=8100
* 第 01 步说端口被占用：80 端口多半是 IIS / 其它站点，先停掉。
* 第 03 步第一次初始化数据库要 1-2 分钟（initdb 自己就要 60-90 秒），
  这不是卡死，别关窗口。日志里如果出现「下载 PostgreSQL」，说明你拿的不是
  完整离线包（只拷了 exe 出来），把 runtime 文件夹一起解压过去即可。
* 第 06 步要复制约 1.5 GB 的标注引擎运行时，1-3 分钟属正常。
* 第 03 步报 initdb 失败、里面有「正在修复已存在目录的权限…Permission denied」：
  这是上一次安装没装完，在 <数据目录>\\pgdata 留下了半成品（有文件但没有
  PG_VERSION）。新版本会自动识别并清掉重来，直接再点一次安装即可。
  如果还是同一句（说明文件被杀软或残留进程锁着），用管理员 PowerShell 跑：
      sc.exe stop ZhituPostgreSQL
      taskkill /F /IM postgres.exe
      takeown /F "C:\\Zhitu\\data\\pgdata" /R /D Y
      Remove-Item -LiteralPath "C:\\Zhitu\\data\\pgdata" -Recurse -Force
  然后重新运行安装程序。**这个目录里没有 PG_VERSION 就说明库里没有数据**，
  删掉不会丢任何业务数据。有 PG_VERSION 的目录不要去删。
* 装完服务起不来、进程反复被杀、日志里有 Killed / 内存不足：这台机器
  内存小于 4 GB，加内存或加大虚拟内存（页面文件）后重跑安装程序。
* 服务都在跑但网页打不开：看 <数据目录>\\logs\\caddy.log。
* 备份一直在失败 / 备份文件很小：先看 <数据目录>\\logs\\backup.log。
  最常见是磁盘满了（备份要跟数据库差不多大的空间）；
  没装 pg_dump 的机器会走冷备，日志里会写「停库 → 压缩 → 起库」，
  那一两分钟内标注页会短暂连不上，属于正常。
* 监控台最后一行「守护进程」显示未运行：它被手动停过（点「全部停止」会一起停），
  或者被安全软件结束过。点「全部启动」或重跑安装程序即可恢复；
  没有它进程崩了不会自动拉起、也不会自动备份。
* **装完想全部停掉做维护**：监控台点「全部停止」（会先停守护再停三个进程）。
  只停进程不停守护的话，5 秒内会被拉回来 —— 这是设计如此，不是卡住了。
* 标注完平台数字不动：第 12 步「LS 长期凭证」没做，重跑安装程序补上。
"""


def log(m: str) -> None:
    print(m, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="先重新打包两个 exe")
    args = ap.parse_args()

    if args.build:
        log("重新打包 exe …")
        rc = subprocess.run([sys.executable, str(HERE / "build_native.py"),
                             "--clean"], cwd=str(HERE)).returncode
        if rc != 0:
            return rc

    missing = [n for n in (SETUP, MONITOR) if not (DIST / n).is_file()]
    if missing:
        log("[X] dist 里缺：%s（先跑 build_native.py）" % missing)
        return 1

    if RELEASE.exists():
        shutil.rmtree(RELEASE)
    RELEASE.mkdir(parents=True)
    total = 0
    for n in (SETUP, MONITOR):
        shutil.copy2(DIST / n, RELEASE / n)
        sz = (RELEASE / n).stat().st_size
        total += sz
        log("  %-28s %6.1f MB" % (n, sz / 1048576))

    note = RELEASE / "安装说明.txt"
    # 记事本对无 BOM 的 UTF-8 会按 GBK 解 → 乱码。这里写 BOM + CRLF。
    note.write_text(README_TXT, encoding="utf-8-sig", newline="\r\n")
    log("  %-28s %6.1f KB" % (note.name, note.stat().st_size / 1024))

    # 交付的"一个包"：解压出来就是上面三个文件，不需要 payload 目录
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zpath = OUT_DIR / ("智途-Windows原生-%s.zip" % STAMP)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(RELEASE.iterdir()):
            zf.write(f, f.name)
    zsz = zpath.stat().st_size
    log("")
    log("=" * 66)
    log("交付包：%s" % zpath)
    log("  内容：%s" % "、".join(f.name for f in sorted(RELEASE.iterdir())))
    log("  大小：%.1f MB（解压后 %.1f MB）" % (zsz / 1048576, total / 1048576))
    log("=" * 66)
    log("发这一个文件就够：解压 → 右键安装程序 → 以管理员身份运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
