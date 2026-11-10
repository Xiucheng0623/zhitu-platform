#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""专测「备用源」那条路：Maven 的 23 MB 包 + Python 执行 SQL（没有 psql）。

为什么单独验：官方包只有 EDB 一家在发，国内经常慢到没法用；备用源是真正的
兜底，必须确定它**装得上、库建得出、迁移跑得完**，否则写着"自动兜底"等于骗人。

    python probe_pg_fallback.py
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "windows-native" / "src"))

import zn_common as C  # noqa: E402
import probe_pg as P  # noqa: E402

ALT_ROOT = Path("D:/zhitu-pg-alt")
LOCAL_JAR = Path("D:/pgz/pgwin.zip")          # 之前手工下过的同一个包，省一次下载


def main() -> int:
    ALT_ROOT.mkdir(parents=True, exist_ok=True)
    cache = ALT_ROOT / "cache"
    cache.mkdir(exist_ok=True)
    jar = cache / C.PG_SOURCES[1]["file"]
    if not jar.is_file() and LOCAL_JAR.is_file():
        print("复用已下载的备用包：%s → %s" % (LOCAL_JAR, jar), flush=True)
        shutil.copy2(LOCAL_JAR, jar)

    only_alt = (C.PG_SOURCES[1],)
    print("只走备用源：%s" % only_alt[0]["label"], flush=True)
    rc = P.main(root=ALT_ROOT, port=55433, sources=only_alt)

    print("\n工具集 = %s（server 表示没有 psql / pg_dump）"
          % C.pg_toolset(ALT_ROOT / "pgsql"), flush=True)
    has_psql = (ALT_ROOT / "pgsql" / "bin" / "psql.exe").is_file()
    print("psql 存在 = %s（预期 False）" % has_psql, flush=True)
    print("结论：%s" % ("备用源可用，SQL 走 Python 执行" if rc == 0 else "备用源失败"),
          flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
