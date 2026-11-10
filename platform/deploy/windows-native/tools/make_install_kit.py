#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打「联机安装套件」：只装 exe + payload，**不带 runtime**（约 26 MB）。

和 make_offline_release.py 的区别
--------------------------------
离线包（366 MB / 解压 1 GB）把 PostgreSQL 二进制、两个 Python 运行时、
Caddy 全塞进去，装的时候是纯复制，全程不联网。

本套件不带运行时。安装程序第 02 步找不到 runtime/ 会自动落到「在线安装」分支：
  * 找系统 Python 3.11–3.13（没有就 winget 装 3.12）
  * 第 03 步从 EDB 官方下 PostgreSQL 17.2（约 330 MB），失败自动切 Maven 备用源（23 MB）
  * 第 04/06 步用 pip 从阿里云镜像装依赖（LS 那步 10–20 分钟）
  * 第 09 步下 Caddy（约 45 MB，走 ghproxy）

为什么要这个形态：仓库 + Releases 足够交付，不用人工搬 366 MB 的 zip。
代价是装的时候要联网，总时长 25–45 分钟，且失败面比离线包大（pip 装 LS 最脆）。
"""
from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent      # windows-native/
DIST = HERE / "dist"
PAYLOAD = HERE / "payload"
OUT_DIR = HERE / "release"

README_ONLINE = """智途 · Windows 原生版（联机安装套件）
=================================================

一、这个包是什么
--------------------------------------------------
只有安装程序和平台源码（约 26 MB），**不含 1 GB 运行时**。
装的时候安装程序自己下载并安装：

  PostgreSQL 17.2      约 330 MB   EDB 官方源，失败自动切 Maven 备用源（23 MB）
  标注引擎 LS 1.23.2   约 300 MB   pip 阿里云镜像，这一步 10–20 分钟
  平台编排层依赖        约 60 MB   pip 阿里云镜像
  Caddy 入口            约 45 MB   走 ghproxy 镜像

预计总时长 25–45 分钟。**服务器必须能上外网**；全程断网的服务器请改用
「完整离线包」（智途-Windows完整离线-*.zip，366 MB）。

二、装之前确认三件事
--------------------------------------------------
1. 系统：Windows 10 / 11 / Server 2016 以上，64 位；
2. Python：3.11–3.13 任意一个即可。没有的话安装程序会尝试 winget 装 3.12，
   但服务器上 winget 经常不可用 —— 建议**先手动装 Python 3.12**
   （https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe，
   安装时务必勾选 Add python.exe to PATH）；
3. 端口 80 空着。装之前先看：netstat -ano | findstr ":80 "

三、怎么装
--------------------------------------------------
1. 把整个「智途」文件夹解压出来（例如 D:\\智途\\）。
   **必须整个文件夹一起解压**：exe 旁边的 payload 文件夹是平台源码，
   只把 exe 单独拷出来会报「载荷里缺编排层源码」。
2. 右键「智途安装程序（原生）.exe」→ 以管理员身份运行。
3. 点「开始安装」，走完 13 步。中途某一步失败不要从头再点一遍：
   程序是「失败不中断」，跑完会在同目录生成 install-report-<时间>.txt，
   直接再点一次「开始安装」，已完成的步骤会自动跳过。
4. 装完桌面有「启动智途.bat」，浏览器打开 http://127.0.0.1/ 就是平台。

四、常见失败
--------------------------------------------------
* 第 02 步找不到 Python → 按上面第二节手动装 3.12 后重跑。
* 第 03 步 PostgreSQL 起不来 → 看 C:\\智途\\data\\pgdata\\log\\postgres.log 末尾。
  报 lock file "postmaster.pid" already exists 就删掉那个文件重跑；
  报 incompatible 说明 data 目录是别的 PG 大版本建的，把 pgdata 改名留档再装。
* 第 06 步 LS 装到一半失败 → 多半是 pip 网络抖动或杀软锁文件，
  直接重跑一次（脚本自带 3 次重试），别删 venv。
* 第 09 步 Caddy 下不下来 → 手工下 caddy_2.8.4_windows_amd64.zip
  放进 智途\\payload\\cache\\ 后重跑。

------------------------------------------------------------------

"""


def main() -> int:
    exe = DIST / "智途安装程序（原生）.exe"
    mon = DIST / "智途监控（原生）.exe"
    for p in (exe, mon):
        if not p.is_file():
            print("缺 exe：%s（先跑 build_native.py）" % p)
            return 1
    if not (PAYLOAD / "app").is_dir():
        print("缺 payload：%s（先跑 make_payload.py）" % PAYLOAD)
        return 1

    entries: list = [(exe, "智途/" + exe.name), (mon, "智途/" + mon.name)]
    for f in sorted(PAYLOAD.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            entries.append((f, "智途/payload/" + f.relative_to(PAYLOAD).as_posix()))

    total = sum(f.stat().st_size for f, _ in entries)
    print("合计 %d 个文件 / %.1f MB" % (len(entries), total / 1048576))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    zpath = OUT_DIR / ("智途-安装套件-%s.zip" % stamp)
    if zpath.exists():
        zpath.unlink()

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("智途/安装说明.txt", README_ONLINE.encode("utf-8-sig"))
        for src, arc in entries:
            z.write(src, arc)

    print("=" * 60)
    print("安装套件：%s" % zpath)
    print("  %.1f MB（压缩后）。解压 -> 右键 exe -> 以管理员身份运行"
          % (zpath.stat().st_size / 1048576))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
