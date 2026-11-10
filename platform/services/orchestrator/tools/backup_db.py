#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行备份：把 forge 库导出来（自包含，不依赖安装程序）。

用法（装完之后）：
    cd <app>\\services\\orchestrator
    ..\\..\\venv\\Scripts\\python.exe -m tools.backup_db
    ..\\..\\venv\\Scripts\\python.exe -m tools.backup_db --keep 14

为什么单独一个脚本：备份要能被计划任务在凌晨调用，GUI 不方便。

两种实现自动选：
  * 有 `pg_dump`（官方 zip 装出来的）：逻辑备份，**不停服务**；
  * 只有服务端（Maven 备用包）：冷备 —— 停库 → 压缩整个数据目录 → 起库，
    **会短暂中断服务**，日志里会写明。

刻意不 import 安装程序的 zn_common：那个模块在安装器 exe 里，服务器上并不存在。
这里只依赖标准库 + 环境里已有的 pg 二进制。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

DEFAULT_APP = r"C:\Zhitu\app"
DEFAULT_DATA = r"C:\Zhitu\data"


def log(m: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def load_state() -> dict:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "Zhitu" / "native.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_env(path: Path) -> dict:
    out: dict = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def run(cmd, timeout=3600, env=None):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    raw = (p.stdout or b"") + (b"\n" if p.stdout and p.stderr else b"") + (p.stderr or b"")
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return p.returncode, raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return p.returncode, raw.decode("utf-8", errors="replace").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=7, help="保留最近几份（默认 7）")
    ap.add_argument("--out", default="", help="备份到指定目录")
    ap.add_argument("--port", type=int, default=5432)
    args = ap.parse_args()

    st = load_state()
    # 目录优先取环境变量，再取 native.json，最后用默认值。
    # 为什么需要环境变量这一层：这个脚本是**以 SYSTEM 身份**被计划任务调起来的，
    # 而 load_state() 靠 %LOCALAPPDATA%\Zhitu\native.json —— SYSTEM 的
    # LOCALAPPDATA 是 systemprofile 那个目录，读不到用户装的 native.json。
    # 更麻烦的是它会「静默」退回默认 C:\Zhitu\...：装在自定义目录的机器上，
    # 备份会每晚失败在「找不到 PostgreSQL」，而白天一切正常。
    app_dir = Path(os.environ.get("ZHITU_APP_DIR") or st.get("app_dir") or DEFAULT_APP)
    data_dir = Path(os.environ.get("ZHITU_DATA_DIR") or st.get("data_dir") or DEFAULT_DATA)
    pg_root = app_dir / "pgsql"
    pg_data = data_dir / "pgdata"
    env = read_env(data_dir / "config" / "forge.env")
    superpass = env.get("PG_SUPER_PASSWORD", "")
    port = int(st.get("pg_port") or env.get("PG_PORT") or args.port)

    bin_dir = pg_root / "bin"
    pg_dump = bin_dir / "pg_dump.exe"
    pg_ctl = bin_dir / "pg_ctl.exe"
    if not pg_ctl.is_file():
        log("找不到 PostgreSQL：%s（这台机器装过智途吗？）" % pg_root)
        return 2
    if not superpass:
        log("forge.env 里没有 PG_SUPER_PASSWORD，没法连库（重装会重建）")
        return 2
    if not (pg_data / "PG_VERSION").is_file():
        log("数据目录不像 PG 集群：%s" % pg_data)
        return 2

    dest = Path(args.out) if args.out else (data_dir / "backup")
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = dest / ("forge-%s%s" % (stamp, ".dump" if pg_dump.is_file() else ".zip"))

    if pg_dump.is_file():
        log("pg_dump 逻辑备份（不停服务）…")
        rc, out = run([pg_dump, "-h", "127.0.0.1", "-p", str(port), "-U", "postgres",
                       "-d", "forge", "-Fc", "-f", str(target)],
                      env={"PGPASSWORD": superpass})
        if rc != 0 or not target.is_file():
            log("pg_dump 失败（rc=%s）：%s" % (rc, out[-400:]))
            return 1
    else:
        log("没有 pg_dump，改用冷备：停库 → 压缩数据目录 → 起库（服务会中断一会儿）")
        rc, out = run([pg_ctl, "stop", "-D", str(pg_data), "-m", "fast"], timeout=300)
        log("  停库 rc=%s %s" % (rc, out[-160:]))
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(pg_data.rglob("*")):
                    if p.is_file():
                        zf.write(p, p.relative_to(pg_data))
        finally:
            rc2, out2 = run([pg_ctl, "start", "-D", str(pg_data), "-w", "-t", "90"],
                            timeout=300)
            log("  起库 rc=%s %s" % (rc2, out2[-160:]))
            if rc2 != 0:
                log("[严重] 起库失败，去服务里手工启动 %s" % pg_data)

    size = target.stat().st_size if target.is_file() else 0
    log("完成：%s（%.1f MB）" % (target, size / 1048576))
    if size < 1024:
        log("[警告] 备份不到 1 KB，可能是空库或导出失败，务必人工确认一次")

    olds = sorted(dest.glob("forge-*"), key=lambda q: q.stat().st_mtime, reverse=True)
    for q in olds[max(1, args.keep):]:
        try:
            q.unlink()
            log("清掉旧备份 %s" % q.name)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
