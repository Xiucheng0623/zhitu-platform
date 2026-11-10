#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打「完整离线包」：exe + payload + runtime 一起压成一个 zip。

为什么单独一个脚本：单文件 exe 装不下 1 GB 的运行时（PyInstaller onefile 每次启动
都要把载荷解到 %TEMP%，1 GB 要等半分钟，杀软还会拦）。所以离线包走「目录形态」：
zip 里是 exe + payload/ + runtime/，解压即用，安装程序从旁边读。
"""
from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFFLINE = Path("D:/zhitu-offline-build")
RUNTIME = OFFLINE / "runtime"
OUT_DIR = OFFLINE / "dist"

# 这些目录不进包：调试残留、被删了一半的、冗余底座
SKIP_DIR_NAMES = {"__pycache__", "_bad_pyls", "_bad_pyls2", "_trash_pyls",
                  "_trash_pyls2", "_new_pyls", "py313"}
SKIP_SUFFIX = {".pyc", ".pyo"}

README_EXTRA = """【本包是完整离线包，不需要联网】

解压后目录里除了安装程序，还带着一个 runtime 文件夹（约 1 GB），里面是：
  runtime/pgsql     PostgreSQL 17.2（含 psql）
  runtime/py-ls     标注引擎运行时（Python 3.13 + Label Studio 1.23.2 全套依赖）
  runtime/py-orch   平台运行时（Python 3.13 + 平台依赖）
  runtime/caddy     入口反代

安装程序会自动把它们铺到安装目录，**服务器上不需要装 Python、不需要 pip、
不需要下载 PostgreSQL**。旧版本最慢的两步（下 330 MB 数据库、pip 装 20 分钟
标注引擎）在这里都是几十秒的复制。

注意事项：
  * runtime 文件夹必须和安装程序放在同一层，不要只把 exe 单独拷出去。
  * 解压要完整（有些压缩软件会跳过无扩展名文件）。
  * 整个解压 + 安装过程大约 3-8 分钟，比以前快很多。

------------------------------------------------------------------

"""


def should_skip(p: Path) -> bool:
    if p.suffix.lower() in SKIP_SUFFIX:
        return True
    return any(part in SKIP_DIR_NAMES for part in p.parts)


def collect(root: Path, arc_prefix: str, out: list) -> None:
    if not root.is_dir():
        print("  [跳过] 不存在：%s" % root)
        return
    n = 0
    for f in root.rglob("*"):
        if not f.is_file() or should_skip(f):
            continue
        out.append((f, arc_prefix + "/" + f.relative_to(root).as_posix()))
        n += 1
    print("  %-28s %6d 文件" % (arc_prefix + "/", n))


def main() -> int:
    import make_release as MR

    exe = HERE / "dist" / "智途安装程序（原生）.exe"
    mon = HERE / "dist" / "智途监控（原生）.exe"
    for p in (exe, mon):
        if not p.is_file():
            print("缺 exe：%s（先跑 build_native.py --no-embed）" % p)
            return 1

    entries: list = []
    print("收集文件：")
    entries.append((exe, "智途/智途安装程序（原生）.exe"))
    entries.append((mon, "智途/智途监控（原生）.exe"))
    collect(HERE / "payload", "智途/payload", entries)
    for name in ("pgsql", "py-ls", "py-orch", "caddy"):
        collect(RUNTIME / name, "智途/runtime/" + name, entries)

    total = sum(f.stat().st_size for f, _ in entries if f.is_file())
    print("\n合计 %d 个文件 / %.0f MB" % (len(entries), total / 1048576))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    zpath = OUT_DIR / ("智途-Windows完整离线-%s.zip" % stamp)
    if zpath.exists():
        zpath.unlink()

    readme = README_EXTRA + MR.README_TXT
    t0 = time.time()
    print("\n压缩中（ZIP_DEFLATED，可能要几分钟）…")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("智途/安装说明.txt", readme.encode("utf-8-sig"))
        for i, (src, arc) in enumerate(entries, 1):
            z.write(src, arc)
            if i % 8000 == 0:
                print("  %d/%d  %.0fs" % (i, len(entries), time.time() - t0), flush=True)

    size = zpath.stat().st_size
    print("\n" + "=" * 66)
    print("离线包：%s" % zpath)
    print("  原始 %.0f MB -> 压缩 %.0f MB（%.1f%%）"
          % (total / 1048576, size / 1048576, size * 100.0 / max(total, 1)))
    print("  用时 %.0f 秒" % (time.time() - t0))
    print("=" * 66)
    print("发这一个文件：解压 -> 右键「智途安装程序（原生）.exe」-> 以管理员身份运行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
