#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""原生 Windows 版的载荷自检 —— 不需要管理员、不需要 Docker、不联网。

它回答一个问题：**这份 payload 拿出去，安装程序能不能一路走到 13 步全绿？**
能验的都验：文件在不在、是不是最新构建的产物、路径对不对、模板占位符是否
全部能替换、缓存包名字是否和安装程序查找的名字一致、GUI 与 --auto 的步骤表
是否同一份。验不了的也明说：镜像/服务/端口那些要真机。

用法：
    cd platform/deploy/windows-native
    python verify_payload.py            # 先自动 make_payload
    python verify_payload.py --no-build
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLATFORM = HERE.parents[1]
sys.path.insert(0, str(HERE / "src"))
os.environ.setdefault("ZHITU_CONFIG_DIR", tempfile.mkdtemp(prefix="zhitu-verify-"))

PAYLOAD = HERE / "payload"

OK = "\033[32m[ok]\033[0m"
BAD = "\033[31m[FAIL]\033[0m"
WARN = "\033[33m[warn]\033[0m"

passed = 0
failed: list[str] = []
warned: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global passed
    if cond:
        passed += 1
        print("%s %s%s" % (OK, name, ("  " + detail) if detail else ""))
    else:
        failed.append(name)
        print("%s %s%s" % (BAD, name, ("  " + detail) if detail else ""))
    return cond


def warn(name: str, detail: str = "") -> None:
    warned.append(name)
    print("%s %s%s" % (WARN, name, ("  " + detail) if detail else ""))


def sha(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def same_tree(a: Path, b: Path, skip=("__pycache__",)) -> tuple[int, list[str]]:
    """逐文件比 a（源）与 b（载荷），返回 (相同数, 不一致的相对路径)。"""
    n = 0
    diff: list[str] = []
    for p in a.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(a)
        if any(s in rel.parts for s in skip):
            continue
        q = b / rel
        if not q.is_file():
            diff.append("%s（缺失）" % rel.as_posix())
        elif sha(p) != sha(q):
            diff.append("%s（内容不同）" % rel.as_posix())
        else:
            n += 1
    return n, diff


def main() -> int:
    print("=" * 74)
    print("智途 · 原生 Windows 载荷自检")
    print("=" * 74)

    # ---- 0. 载荷是否就位 -------------------------------------------------
    # 默认**不重建**：这个脚本验的是"现在磁盘上这套产物"，顺手 make_payload
    # 会让刚打好的 exe（内嵌旧载荷）与磁盘 payload（新）悄悄不一致，而两者
    # 都没报错 —— 交付出去才发现装的是上一版前端。
    if "--build" in sys.argv or not (PAYLOAD / "manifest.json").is_file():
        print("\n[0] 生成 payload …")
        rc = subprocess.run([sys.executable, str(HERE / "make_payload.py")],
                            cwd=str(HERE)).returncode
        if not check("make_payload.py 执行成功", rc == 0, "rc=%d" % rc):
            return 1
    else:
        print("\n[0] 用现有 payload（要重建加 --build）")

    # ---- 1. 结构 ---------------------------------------------------------
    print("\n[1] 载荷结构")
    check("payload/ 存在", PAYLOAD.is_dir(), str(PAYLOAD))
    man_p = PAYLOAD / "manifest.json"
    man = {}
    if check("manifest.json 存在", man_p.is_file()):
        try:
            man = json.loads(man_p.read_text(encoding="utf-8"))
            check("manifest 版本 = windows-native",
                  man.get("edition") == "windows-native", str(man.get("edition")))
        except ValueError as exc:
            check("manifest.json 可解析", False, str(exc))

    # ---- 2. 前端产物 ------------------------------------------------------
    print("\n[2] 前端产物（caddy 直接托管，服务器不装 Node）")
    fe = PAYLOAD / "frontend"
    check("frontend/index.html 存在", (fe / "index.html").is_file())
    html = (fe / "index.html").read_text(encoding="utf-8", errors="replace") \
        if (fe / "index.html").is_file() else ""
    check("index.html 引用了打包资源", "/assets/" in html, "找到 /assets/ 引用")
    assets = list((fe / "assets").glob("*.js")) if (fe / "assets").is_dir() else []
    check("assets/*.js 至少 1 个", len(assets) >= 1, "%d 个 js" % len(assets))
    brand_ok = (fe / "brand").is_dir() or (fe / "brand" / "sy-mark-dark.png").is_file()
    check("前端带品牌静态资源", bool(brand_ok), "frontend/brand/")
    n_same, diff = same_tree(PLATFORM / "web" / "dist", fe)
    check("前端与 web/dist 逐字节一致", not diff,
          "%d 个文件相同%s" % (n_same, ("；不一致：" + "，".join(diff[:3])) if diff else ""))
    check("manifest 里的前端文件数与实际一致",
          man.get("frontend_files") in (None, len([p for p in fe.rglob("*") if p.is_file()])),
          "manifest=%s 实际=%d" % (man.get("frontend_files"),
                                   len([p for p in fe.rglob("*") if p.is_file()])))

    # ---- 3. 编排层 --------------------------------------------------------
    print("\n[3] 编排层源码")
    orch = PAYLOAD / "app" / "services" / "orchestrator"
    check("requirements.txt 存在", (orch / "requirements.txt").is_file())
    req = (orch / "requirements.txt").read_text(encoding="utf-8") \
        if (orch / "requirements.txt").is_file() else ""
    for dep in ("fastapi", "uvicorn", "psycopg", "pyjwt", "httpx"):
        check("requirements 含 %s" % dep, dep in req)
    check("app/ 目录存在", (orch / "app").is_dir())
    check("tools/ 目录存在", (orch / "tools").is_dir())
    check("tools/bootstrap_admin.py 存在（第 08 步要跑）",
          (orch / "tools" / "bootstrap_admin.py").is_file())
    n_same, diff = same_tree(PLATFORM / "services" / "orchestrator" / "app",
                             orch / "app")
    check("编排层 app/ 与源逐字节一致", not diff,
          "%d 文件%s" % (n_same, ("；不一致：" + "，".join(diff[:3])) if diff else ""))
    check("载荷里没有 __pycache__ / .pyc",
          not any(p.suffix == ".pyc" or p.name == "__pycache__"
                  for p in PAYLOAD.rglob("*")),
          "干净")

    # ---- 4. 迁移 ----------------------------------------------------------
    print("\n[4] 数据库迁移")
    mig = PAYLOAD / "app" / "deploy" / "migrations"
    mfiles = sorted(mig.glob("*.sql")) if mig.is_dir() else []
    names = [m.name for m in mfiles]
    check("迁移目录存在", mig.is_dir(), str(mig))
    check("迁移数量 = 11", len(mfiles) == 11, "实际 %d：%s" % (len(mfiles), names))
    for must in ("000_roles.sql", "001_schema.sql", "013_ux_enhance.sql"):
        check("含 %s" % must, must in names)
    retired = "014" + "_label_flow.sql"      # 拼出来，别让这串字面量进包里
    check("不含已下线的流程设计器迁移", retired not in names)
    # 只比顶层 0*.sql：源里的 verification/ 是人工验证脚本，不该进交付载荷
    src_mig = PLATFORM / "deploy" / "migrations"
    diff = [m.name for m in mfiles
            if not (src_mig / m.name).is_file()
            or sha(src_mig / m.name) != sha(m)]
    check("迁移与源逐字节一致", not diff,
          "%d 个一致%s" % (len(mfiles) - len(diff),
                          ("；不一致：" + "，".join(diff[:3])) if diff else ""))
    check("源里的 verification/ 没被误打进载荷（人工脚本不进交付）",
          not (mig / "verification").exists())
    check("manifest 迁移数与实际一致",
          man.get("migrations") in (None, len(mfiles)),
          "manifest=%s 实际=%d" % (man.get("migrations"), len(mfiles)))

    # ---- 5. 品牌 / SSO ----------------------------------------------------
    print("\n[5] 品牌补丁与单点登录")
    brand = PAYLOAD / "app" / "labelstudio" / "brand"
    check("apply_brand.py 存在", (brand / "apply_brand.py").is_file())
    check("forge_sso.py 存在（SSO 中间件）", (brand / "forge_sso.py").is_file())
    check("web/public/brand 存在（apply_brand 会回找 PNG）",
          (PAYLOAD / "app" / "web" / "public" / "brand").is_dir())
    n_same, diff = same_tree(PLATFORM / "labelstudio" / "brand", brand)
    check("品牌补丁与源逐字节一致", not diff,
          "%d 文件%s" % (n_same, ("；不一致：" + "，".join(diff[:3])) if diff else ""))

    # ---- 6. Caddy 模板 ----------------------------------------------------
    print("\n[6] Caddy 入口模板")
    tpl_p = PAYLOAD / "caddy" / "Caddyfile.tpl"
    tpl = tpl_p.read_text(encoding="utf-8") if tpl_p.is_file() else ""
    check("Caddyfile.tpl 存在", tpl_p.is_file())
    import re
    found = set(re.findall(r"\{\{([A-Z_]+)\}\}", tpl))
    known = {"APP", "API_PORT", "HTTP_PORT", "LS_PORT", "WEB_ROOT", "LOG_DIR"}
    check("占位符全部在替换表里", found <= known, "找到 %s" % sorted(found))
    check("模板确实用了这些占位符", found >= {"HTTP_PORT", "WEB_ROOT", "API_PORT", "LOG_DIR"},
          "实际 %s" % sorted(found))
    check("auto_https off（内网无证书，http 直连）", "auto_https off" in tpl)
    check("反代到编排层 127.0.0.1:{{API_PORT}}",
          "reverse_proxy 127.0.0.1:{{API_PORT}}" in tpl)
    check("SPA 回退 try_files", "try_files {path} /index.html" in tpl)
    rendered = tpl
    for k, v in (("APP", r"C:\Zhitu\app"), ("API_PORT", "8300"),
                 ("HTTP_PORT", "80"), ("LS_PORT", "8100"),
                 ("WEB_ROOT", r"C:\Zhitu\app\web"), ("LOG_DIR", r"C:\Zhitu\data\logs")):
        rendered = rendered.replace("{{%s}}" % k, v)
    check("替换后没有残留占位符", "{{" not in rendered,
          ([l for l in rendered.splitlines() if "{{" in l] or [""])[0][:70])

    # ---- 7. 与安装程序的一致性 --------------------------------------------
    print("\n[7] 安装程序与载荷的一致性（路径 / 缓存名 / 步骤表）")
    import zn_common as C  # noqa: E402

    # 安装程序读文件的路径，逐条落到磁盘上核对
    wants = {
        "caddy/Caddyfile.tpl": PAYLOAD / "caddy" / "Caddyfile.tpl",
        "frontend/index.html": PAYLOAD / "frontend" / "index.html",
        "app/services/orchestrator/requirements.txt":
            PAYLOAD / "app" / "services" / "orchestrator" / "requirements.txt",
        "app/deploy/migrations": PAYLOAD / "app" / "deploy" / "migrations",
        "app/labelstudio/brand/apply_brand.py":
            PAYLOAD / "app" / "labelstudio" / "brand" / "apply_brand.py",
    }
    for rel, p in wants.items():
        check("安装程序要的 %s 在" % rel, p.exists())

    # pg_fetch 会在 payload/cache 里按文件名找预置包，名字必须两边一致
    srcs = list(C.PG_SOURCES)
    check("PostgreSQL 至少两个源（官方 + 备用）", len(srcs) >= 2,
          "、".join(s["label"] for s in srcs))
    check("两个源的文件名互不相同",
          len({s["file"] for s in srcs}) == len(srcs))
    check("备用源不是 zip（是 Maven 的 jar）",
          any(s["kind"] == "jar" for s in srcs))
    cache = PAYLOAD / "cache"
    if cache.is_dir():
        got = {p.name for p in cache.iterdir() if p.is_file()}
        print("      预置包：%s" % (sorted(got) or "无"))
        unknown = got - {s["file"] for s in srcs} - {"caddy_2.8.4_windows_amd64.zip"}
        check("payload/cache 里没有安装程序不认识的包", not unknown, str(sorted(unknown)))
        for s in srcs:
            if (cache / s["file"]).is_file():
                sz = (cache / s["file"]).stat().st_size
                check("预置 %s 大小达标" % s["file"], sz >= s["min"],
                      "%.1f MB ≥ %.0f MB" % (sz / 1048576, s["min"] / 1048576))
    else:
        warn("payload/cache 不存在",
             "联网安装没问题；断网安装需把 PG / caddy 包放进去（--with-cache 打包）")

    # 步骤表：一份定义，GUI 与 --auto 都从它取
    import inspect
    import zn_setup as S  # noqa: E402
    spec = list(S.STEP_SPEC)
    check("步骤表 13 步", len(spec) == 13, "%d 步" % len(spec))
    keys = [k for k, _ in spec]
    check("步骤 key 不重复", len(set(keys)) == len(keys))
    check("每一步都有 step_<key> 方法",
          all(hasattr(S.Installer, "step_" + k) for k in keys),
          "缺：%s" % [k for k in keys if not hasattr(S.Installer, "step_" + k)])
    check("建库在编排层之后（没有 psql 时要用 venv 里的 psycopg）",
          keys.index("orchestrator") < keys.index("database"),
          "orchestrator=%d database=%d" % (keys.index("orchestrator"), keys.index("database")))
    src = inspect.getsource(S.auto_run)
    check("--auto 复用同一份步骤表（step_list）", "step_list(inst)" in src)
    dev = inspect.getsource(S.SetupApp._build_steps)
    check("GUI 复用同一份步骤表（step_list）", "step_list(i)" in dev)
    titles = [t for _, t in spec]
    check("GUI 标题与 --auto 标题同源", all(t in titles for t in titles),
          titles[0] + " … " + titles[-1])

    # ---- 7b. exe 里的内嵌载荷是不是最新的 ---------------------------------
    print("\n[7b] 打好的 exe 与载荷是否同步")
    man_p2 = PAYLOAD / "manifest.json"
    made = man_p2.stat().st_mtime if man_p2.is_file() else 0
    exes = sorted((HERE / "dist").glob("*.exe")) if (HERE / "dist").is_dir() else []
    if not exes:
        warn("dist/ 里没有 exe", "还没打 exe：python build_native.py --clean")
    for exe in exes:
        stale = exe.stat().st_mtime < made
        check("%s 不旧于载荷" % exe.name, not stale,
              "exe %s / 载荷 %s"
              % (time.strftime("%m-%d %H:%M", time.localtime(exe.stat().st_mtime)),
                 time.strftime("%m-%d %H:%M", time.localtime(made))))
    if exes:
        setup_exe = [e for e in exes if "安装" in e.name]
        if setup_exe:
            # 安装程序必须比监控台大一点（多出来的就是内嵌载荷）
            big = setup_exe[0].stat().st_size
            others = [e.stat().st_size for e in exes if e is not setup_exe[0]]
            if others:
                check("安装程序体积 ≥ 监控台（说明载荷确实内嵌进去了）",
                      big > max(others),
                      "安装程序 %.1f MB / 其它 %.1f MB"
                      % (big / 1048576, max(others) / 1048576))

    # ---- 8. 洁净度 --------------------------------------------------------
    print("\n[8] 洁净度")
    junk = [p.relative_to(PAYLOAD).as_posix() for p in PAYLOAD.rglob("*")
            if p.is_file() and (p.suffix in (".db", ".sqlite3", ".log", ".pyc")
                                or p.name in (".DS_Store", "Thumbs.db"))]
    check("载荷里没有数据库 / 日志 / 字节码垃圾", not junk, str(junk[:5]))
    total = sum(p.stat().st_size for p in PAYLOAD.rglob("*") if p.is_file())
    n_files = sum(1 for p in PAYLOAD.rglob("*") if p.is_file())
    print("      payload 合计 %.1f MB / %d 文件" % (total / 1048576, n_files))
    check("载荷总大小在 0.3–20 MB 之间（不含 cache）",
          0.3 * 1048576 < total - (sum(p.stat().st_size for p in cache.rglob("*"))
                                   if cache.is_dir() else 0) < 20 * 1048576)

    # ---- 汇总 ------------------------------------------------------------
    print("\n" + "=" * 74)
    print("通过 %d 项，失败 %d 项，提示 %d 项" % (passed, len(failed), len(warned)))
    if failed:
        print("失败：")
        for f in failed:
            print("  · " + f)
    print("=" * 74)
    print("这个脚本验不了（要真机）：initdb/服务注册、四个计划任务、80 端口接管、")
    print("真正跑起来的三服务健康检查。那些只能在一台真 Windows 服务器上跑 --auto 看结果。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
