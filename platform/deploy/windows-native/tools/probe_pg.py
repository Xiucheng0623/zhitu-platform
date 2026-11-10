#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机验证：原生 PostgreSQL 这条路到底能不能走通。

验证链：多源下载 → 解压 → initdb → 起库 → 建库 → 建角色 → 跑 11 个迁移 →
       查表数量 → 停库。

本机不是管理员，所以**不注册 Windows 服务**（那步只能在真服务器上验），
改用 pg_ctl start 前台起库。另外本机访问不了 EDB，正好能顺带验"第一个源失败
自动退到备用源"这条兜底路径 —— 那才是国内服务器最可能遇到的情况。

用法：
    python probe_pg.py
"""
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]          # platform/deploy
sys.path.insert(0, str(HERE / "windows-native" / "src"))

import zn_common as C  # noqa: E402

ROOT = Path("D:/zhitu-pg-probe")
PORT = 55432
PASS = "ProbePass_2026"
VENV = ROOT / "venv"


def setup_dirs(root: Path):
    global ROOT, VENV
    ROOT = root
    VENV = root / "venv"


def log(m):
    print(m, flush=True)


def main(root: Path = ROOT, port: int = PORT, sources=None) -> int:
    global PORT
    setup_dirs(root)
    PORT = port
    if sources is not None:
        C.PG_SOURCES = tuple(sources)
    ROOT.mkdir(parents=True, exist_ok=True)
    pg_root = ROOT / "pgsql"
    data = ROOT / "pgdata"
    pass

    # ---- 1) 拿包（多源自动兜底） ----------------------------------------
    if C.pg_installed(pg_root):
        log("[1] PostgreSQL 已就位：%s（工具集 %s）"
            % (pg_root, C.pg_toolset(pg_root)))
    else:
        log("[1] 准备 PostgreSQL（官方源失败会自动换备用源）…")
        ok, msg = C.pg_fetch(pg_root, ROOT / "cache", log)
        log("    " + msg.replace("\n", "\n    "))
        if not ok:
            return 1
        log("    工具集：%s（full=含 psql/pg_dump，server=仅服务端）"
            % C.pg_toolset(pg_root))

    rc, out = C.run([str(C.pg_exe(pg_root, "postgres")), "--version"], timeout=60)
    log("[2] 版本：%s" % out[-80:])

    # ---- 2) SQL 执行器要一个装 psycopg 的 python ------------------------
    py = C.venv_python(VENV)
    if not py.is_file():
        log("[3] 建 venv + 装 psycopg …")
        ok, msg = C.venv_create(sys.executable, VENV)
        log("    " + msg)
        if not ok:
            return 1
        ok, msg = C.pip_install(VENV, ["psycopg[binary]>=3.2"], log, timeout=900)
        log("    " + (msg or "")[-300:])
        if not ok:
            return 1
    log("[3] 连接器：%s" % py)

    # ---- 3) initdb + 起库 ----------------------------------------------
    ok, msg = C.pg_initdb(pg_root, data, PASS)
    log("[4] initdb：%s" % msg)
    if not ok:
        return 1

    conf = data / "postgresql.conf"
    text = conf.read_text(encoding="utf-8", errors="replace")
    if "\nport = %d" % PORT not in text:
        text += "\nport = %d\nlisten_addresses = '127.0.0.1'\n" % PORT
        conf.write_text(text, encoding="utf-8")

    if not C.port_open(PORT):
        pglog = ROOT / "pg.log"
        # 必须 run_daemon：capture_output 会被 postgres 继承的管道卡住（踩过）
        rc, out = C.run_daemon([str(C.pg_exe(pg_root, "pg_ctl")), "start",
                                "-D", str(data), "-l", str(pglog),
                                "-w", "-t", "90"], timeout=180)
        log("[5] pg_ctl start rc=%s %s" % (rc, (out or "")[-200:].replace("\n", " ")))
        if rc != 0:
            return 1
    deadline = time.time() + 40
    while time.time() < deadline and not C.port_open(PORT):
        time.sleep(1)
    if not C.port_open(PORT):
        log("[5] 端口 %d 没监听" % PORT)
        return 1
    log("[5] 端口 %d 已监听" % PORT)

    def db(sql, dbname="postgres"):
        return C.pg_apply(pg_root, dbname, sql, PASS, py=str(py), port=PORT,
                          timeout=600)

    # ---- 4) 建库 / 角色 -------------------------------------------------
    rc, out = db("SELECT 1 FROM pg_database WHERE datname='forge'")
    hit = any(ln.startswith("RESULT ") and ln.split()[1] != "0"
              for ln in out.splitlines())
    log("[6] 查库 rc=%s 命中=%s 输出=%s" % (rc, hit, out.replace("\n", " ")[:120]))
    if not hit:
        rc, out = db("CREATE DATABASE forge")
        log("[6] CREATE DATABASE rc=%s %s" % (rc, out.replace("\n", " ")[:160]))
        if rc != 0:
            return 1

    role_sql = (
        "DO $do$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='forge_app') THEN "
        "    CREATE ROLE forge_app LOGIN PASSWORD %s; "
        "  ELSE ALTER ROLE forge_app WITH LOGIN PASSWORD %s; "
        "  END IF; "
        "END $do$; "
        "ALTER ROLE forge_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
        "ALTER ROLE forge_app CONNECTION LIMIT 60;"
        % ("'ProbeApp_2026'", "'ProbeApp_2026'"))
    rc, out = db(role_sql)
    log("[7] 建角色 rc=%s %s" % (rc, out.replace("\n", " ")[:200]))
    if rc != 0:
        return 1

    # ---- 5) 11 个迁移 ---------------------------------------------------
    mig_dir = HERE / "migrations"
    ok_all = True
    files = [m for m in sorted(mig_dir.glob("0*.sql")) if m.name != "000_roles.sql"]
    for m in files:
        t0 = time.time()
        rc, out = C.pg_apply_file(pg_root, "forge", m, PASS, py=str(py), port=PORT)
        log("[8] %-22s %s %5.1fs %s" % (m.name, "OK  " if rc == 0 else "FAIL",
                                        time.time() - t0,
                                        "" if rc == 0 else (out or "")[-260:]))
        ok_all = ok_all and rc == 0
        if rc != 0:
            break
    log("[8] 迁移 %d/%d 通过" % (len(files) if ok_all else 0, len(files)))

    if ok_all:
        rc, out = db("SELECT count(*) AS tables FROM information_schema.tables "
                     "WHERE table_schema = 'forge'", "forge")
        rows = [ln[4:] for ln in out.splitlines() if ln.startswith("ROW ")]
        log("[9] 表数量：%s" % (rows[0] if rows else out.replace("\n", " ")[:120]))
        # 顺带验一下应用账号能连（RLS 的角色必须存在且能登录）
        rc2, out2 = C.pg_apply(pg_root, "forge",
                               "SELECT current_user AS who, current_setting('server_version') AS ver",
                               "ProbeApp_2026", py=str(py), port=PORT, user="forge_app")
        log("[10] forge_app 登录 rc=%s %s"
            % (rc2, " ".join(out2.split())[:160]))

    C.run([str(C.pg_exe(pg_root, "pg_ctl")), "stop", "-D", str(data), "-m", "fast"],
          timeout=180)
    log("[11] 已停库")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
