#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把平台源码组装成可直接扔上服务器的部署包（tar.gz）。

为什么需要这个脚本
----------------------------------------------------------------------------
docker-compose.yml 里三个服务的 build.context 都是部署包根目录
（COPY src/web/...、src/orchestrator/...、src/labelstudio/...），
而仓库里源码分散在 platform/web、platform/services/orchestrator、
platform/labelstudio 三个目录。这个脚本把它们按镜像构建需要的目录结构
汇进一个干净的 staging 目录再打包 —— 服务器上解压即用，不需要仓库。

打包时刻意排除的东西（都有原因）：
  node_modules / dist        镜像内重新构建，打进去只会让包变大几百 MB
  migrations/verification/   验收夹具（灌演示数据），绝不进生产部署包
  web/tools/                 开发机上的浏览器回归脚本，与部署无关
  __pycache__ / _shots*      编译缓存与截图
  deploy/windows/            Windows 单机部署脚本，Linux 服务器用不上

用法：
    python3 make_package.py              # 产出 dist/forge-deploy-<日期>.tar.gz
    python3 make_package.py --out /tmp   # 指定输出目录
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # platform/deploy
PLATFORM = HERE.parent                          # platform

# (源路径, 包内相对路径)；源是文件就单拷，是目录就递归拷（应用排除规则）
COPIES = [
    # ---- 编排与脚本 ----
    (HERE / "docker-compose.yml", "docker-compose.yml"),
    (HERE / ".env.example", ".env.example"),
    (HERE / ".dockerignore", ".dockerignore"),
    (HERE / "install.sh", "install.sh"),
    (HERE / "preflight.sh", "preflight.sh"),
    (HERE / "backup.sh", "backup.sh"),
    (HERE / "restore.sh", "restore.sh"),
    (HERE / "smoke.sh", "smoke.sh"),
    (HERE / "README.md", "README.md"),
    (HERE / "docker", "docker"),
    (HERE / "tools" / "fetch_ls_refresh.py", "tools/fetch_ls_refresh.py"),
    # caddy 只带两个模板；包内 caddy/Caddyfile 由 install.sh 生成，
    # 这样域名/IP 两种模式共用一份 compose 挂载配置
    (HERE / "caddy" / "Caddyfile.domain", "caddy/Caddyfile.domain"),
    (HERE / "caddy" / "Caddyfile.ip", "caddy/Caddyfile.ip"),
    # ---- 前端源码（web.Dockerfile 在镜像里 npm ci + vite build）----
    (PLATFORM / "web" / "package.json", "src/web/package.json"),
    (PLATFORM / "web" / "package-lock.json", "src/web/package-lock.json"),
    (PLATFORM / "web" / "index.html", "src/web/index.html"),
    (PLATFORM / "web" / "vite.config.ts", "src/web/vite.config.ts"),
    (PLATFORM / "web" / "tsconfig.json", "src/web/tsconfig.json"),
    (PLATFORM / "web" / "nginx.conf", "src/web/nginx.conf"),
    (PLATFORM / "web" / "src", "src/web/src"),
    # public 必须带：里面 brand/sy-mark-dark.png 是 LS 品牌补丁按
    # 相对路径引用的同一份图（apply_brand.py: BRAND_DIR.parents[1]/web/...）
    (PLATFORM / "web" / "public", "src/web/public"),
    # ---- 中间层 ----
    (PLATFORM / "services" / "orchestrator" / "requirements.txt",
     "src/orchestrator/requirements.txt"),
    (PLATFORM / "services" / "orchestrator" / "app", "src/orchestrator/app"),
    # ---- LS 品牌补丁资产 ----
    (PLATFORM / "labelstudio" / "brand", "src/labelstudio/brand"),
    # ---- Windows 部署脚本（同一个包两个平台都能落地）----
    # Linux 上用 install.sh；Windows 上二选一：
    #   windows/install.ps1        裸机（计划任务 + 本机 PostgreSQL + LS venv）
    #   windows-docker/*.ps1       WSL 2 里跑 Docker Engine（Server 2022+，需嵌套虚拟化）
    # 脚本一共几十 KB，带上比维护两个包更不容易出错。
    (HERE / "windows", "windows"),
    (HERE / "windows-docker", "windows-docker"),
    # ---- Windows 客户端（EXE 安装程序 + 监控台）源码 ----
    # 只带源码与打包脚本：exe 由 build_client.py 现场生成，payload 由
    # make_payload.py 在有 Docker 的机器上生成（镜像 2-3 GB，不能进源码包）。
    # 带上它们是为了让"exe 是怎么来的"可复现，而不是只给客户一个黑盒。
    (HERE / "windows-client" / "README.md", "windows-client/README.md"),
    (HERE / "windows-client" / "build_client.py", "windows-client/build_client.py"),
    (HERE / "windows-client" / "make_payload.py", "windows-client/make_payload.py"),
    (HERE / "windows-client" / "make_icon.py", "windows-client/make_icon.py"),
    (HERE / "windows-client" / "verify_payload.py", "windows-client/verify_payload.py"),
    (HERE / "windows-client" / "src", "windows-client/src"),
    # ---- Windows 原生路线（不碰 Docker / WSL，服务器就是一台 Windows）----
    # 同样是只带源码与脚本：两个 exe 由 build_native.py 现场打，payload 由
    # make_payload.py 生成（前端产物 + 编排层源码 + 迁移 + 品牌补丁，1.5 MB）。
    # build/ dist/ payload/ release/ cache/ 都在 EXCLUDE_DIRS 里，不进源码包。
    (HERE / "windows-native" / "README.md", "windows-native/README.md"),
    (HERE / "windows-native" / "build_native.py", "windows-native/build_native.py"),
    (HERE / "windows-native" / "make_payload.py", "windows-native/make_payload.py"),
    (HERE / "windows-native" / "verify_payload.py", "windows-native/verify_payload.py"),
    (HERE / "windows-native" / "make_release.py", "windows-native/make_release.py"),
    (HERE / "windows-native" / "src", "windows-native/src"),
    (HERE / "windows-native" / "tools", "windows-native/tools"),
]

# 目录递归时的排除规则（目录名或文件名后缀）
EXCLUDE_DIRS = {"__pycache__", "node_modules", "dist", "dist-sso",
                ".git", "_shots", "_shots-wired", "_shots_sso", "tests",
                # build 是 PyInstaller 的中间
                # 工作区（几十 MB 垃圾），payload 是 compose+源码+镜像，
                # release 是 exe+payload 组装好的交付目录（25 MB 起）。
                # 三者都由脚本按需生成，绝不能进源码包。
                "build", "payload", "release",
                # cache 是预置的大安装包（PG 330 MB / 备用包 23 MB），
                # 属于"客户现场用"的东西，不进源码包
                "cache"}
EXCLUDE_SUFFIX = (".pyc", ".orig", ".log")

SQL_FILES = [
    "000_roles.sql", "001_schema.sql", "002_rls.sql", "003_bootstrap.sql",
    "005_finance.sql", "006_collection.sql", "007_rls_ext.sql",
    "010_identity.sql", "011_sso.sql", "012_member_link.sql",
    "013_ux_enhance.sql",
]

EXECUTABLES = {"install.sh", "preflight.sh", "backup.sh", "restore.sh", "smoke.sh",
               "tools/fetch_ls_refresh.py"}


def git_rev() -> str:
    """打包溯源：记录 git 版本，出问题能对回代码。拿不到就不写。"""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=PLATFORM, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def excluded(name: str) -> bool:
    return name in EXCLUDE_DIRS or name.endswith(EXCLUDE_SUFFIX)


def add_file(tf: tarfile.TarFile, src: Path, arcname: str) -> None:
    info = tf.gettarinfo(str(src), arcname=f"forge-deploy/{arcname}")
    # 脚本给执行位；其余按源文件权限（644），tar 里显式写死避免 Windows 权限混乱
    info.mode = 0o755 if arcname in EXECUTABLES else 0o644
    with open(src, "rb") as f:
        tf.addfile(info, f)


def add_tree(tf: tarfile.TarFile, src: Path, arcname: str) -> None:
    for p in sorted(src.rglob("*")):
        if p.is_dir() or excluded(p.name):
            continue
        rel = p.relative_to(src).as_posix()
        add_file(tf, p, f"{arcname}/{rel}")


def main() -> int:
    ap = argparse.ArgumentParser(description="组装 FORGE 服务器部署包")
    ap.add_argument("--out", type=Path, default=HERE / "dist", help="输出目录")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().strftime("%Y%m%d")
    out = args.out / f"forge-deploy-{stamp}.tar.gz"

    missing = [str(s) for s, _ in COPIES if not s.exists()]
    missing += [f"deploy/migrations/{n}" for n in SQL_FILES
                if not (HERE / "migrations" / n).exists()]
    if missing:
        print(f"[X] 源文件缺失：{missing}")
        return 1

    with tarfile.open(out, "w:gz") as tf:
        for src, arc in COPIES:
            if src.is_dir():
                add_tree(tf, src, arc)
            else:
                add_file(tf, src, arc)
        # 迁移只带结构/权限/引导/业务域，verification/ 故意不进包
        for name in SQL_FILES:
            add_file(tf, HERE / "migrations" / name, f"migrations/{name}")
        # 打包溯源
        info = tarfile.TarInfo("forge-deploy/build-info.txt")
        body = (f"built={dt.datetime.now().isoformat(timespec='seconds')}\n"
                f"git={git_rev()}\n").encode()
        info.size = len(body)
        info.mode = 0o644
        import io
        tf.addfile(info, io.BytesIO(body))

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[ok] {out}  ({size_mb:.1f} MB)")
    print("解压后按 README.md 的「快速开始」执行 ./install.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
