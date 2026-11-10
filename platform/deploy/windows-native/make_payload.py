#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""组装原生 Windows 安装包的载荷 payload/。

与原容器路线（`windows-client/make_payload.py`）的区别
----------------------------------------------------------------------------
那边带的是 compose + Dockerfile + 源码树，安装时 `docker build`；
这边带的是**能在 Windows 上直接跑的东西**：已构建好的前端静态文件、
编排层 Python 源码、迁移 SQL、Label Studio 的品牌补丁。

交付目录
----------------------------------------------------------------------------
    <发布目录>\\智途安装程序.exe          （原生版）
    <发布目录>\\智途监控.exe
    <发布目录>\\payload\\
      manifest.json
      frontend\\**                     ← web/dist 的产物（caddy 直接托管）
      app\\services\\orchestrator\\**   ← requirements.txt + app/ + tools/
      app\\deploy\\migrations\\*.sql    ← 11 个迁移
      app\\labelstudio\\brand\\**       ← 品牌补丁 + SSO 中间件
      caddy\\Caddyfile.tpl             ← 入口模板（安装时替换路径占位符）
      cache\\*.zip                     ← 可选：预置 PG / caddy 安装包（无外网时必需）

为什么前端要预构建
----------------------------------------------------------------------------
服务器上装 Node + npm ci + vite build 是另一个 500 MB 的依赖和 10 分钟等待，
而且 npm 源在国内经常抽风。前端是静态产物，构建一次就能反复用，没必要在
客户机器上重建。

为什么 PG / caddy 可以走 cache
----------------------------------------------------------------------------
两者都是官方二进制包（PG ~330 MB、caddy ~16 MB）。默认安装时从网上下；
如果打包机提前把 zip 放进 payload/cache/，安装时就完全不需要外网。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLATFORM = HERE.parents[1]                 # platform/
DEPLOY = PLATFORM / "deploy"               # platform/deploy
PAYLOAD = HERE / "payload"
VERSION = time.strftime("%Y%m%d")

# 不参与运行的东西：依赖、缓存、测试、截图、临时产物
SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", ".pytest_cache", ".mypy_cache",
    "tests", "_shots", "_shots_sso", "_shots-wired", "dist-sso",
    ".vite", "coverage", "legacy",
}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite3"}


def copy_tree(src: Path, dst: Path, log=lambda m: None) -> tuple[int, int]:
    n = 0
    total = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if Path(f).suffix in SKIP_SUFFIX:
                continue
            s = Path(root) / f
            try:
                shutil.copy2(s, dst / rel / f)
                n += 1
                total += s.stat().st_size
            except OSError as exc:
                log("  [!] %s: %s" % (s, exc))
    return n, total


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# 原生路线的 Caddyfile 模板。占位符用 {{...}}，安装时替换成真实路径 ——
# 不能用环境变量：caddy 作为计划任务启动时环境块由我们写死，路径里带空格
# （C:\Program Files）时环境变量更易出错，直接写进文件最省事。
CADDY_TPL = r"""# 智途 · 原生 Windows 入口（由安装程序生成，不要手改 {{APP}} 里的路径）
# 平台：http://<服务器>/
#   /api/* 与 /healthz  →  编排层 127.0.0.1:{{API_PORT}}
#   其它                 →  前端静态文件 {{WEB_ROOT}}
{
	admin off
	auto_https off
}

:{{HTTP_PORT}} {
	encode zstd gzip

	handle /api/* {
		reverse_proxy 127.0.0.1:{{API_PORT}} {
			header_up X-Forwarded-For {remote_host}
			header_up X-Real-IP {remote_host}
		}
	}

	handle /healthz {
		reverse_proxy 127.0.0.1:{{API_PORT}}
	}

	handle {
		root * {{WEB_ROOT}}
		try_files {path} /index.html
		file_server
	}

	header {
		X-Content-Type-Options nosniff
		X-Frame-Options SAMEORIGIN
		Referrer-Policy strict-origin-when-cross-origin
		Content-Security-Policy "frame-ancestors 'self'"
		-Server
	}

	log {
		output file {{LOG_DIR}}/caddy.log
		format json
	}
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-cache", action="store_true",
                    help="把已下载的 PG / caddy 压缩包一起打进 payload/cache")
    args = ap.parse_args()

    web_dist = PLATFORM / "web" / "dist"
    if not (web_dist / "index.html").is_file():
        raise SystemExit("前端产物不存在：%s\n先执行  cd platform/web && npm run build"
                         % web_dist)

    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    PAYLOAD.mkdir(parents=True)

    print("复制前端产物 …")
    n, b = copy_tree(web_dist, PAYLOAD / "frontend")
    print("  frontend          %3d 文件 / %.1f MB" % (n, b / 1048576))
    f_files = n

    print("复制编排层 …")
    orch_src = PLATFORM / "services" / "orchestrator"
    orch_dst = PAYLOAD / "app" / "services" / "orchestrator"
    orch_dst.mkdir(parents=True)
    shutil.copy2(orch_src / "requirements.txt", orch_dst / "requirements.txt")
    n1, b1 = copy_tree(orch_src / "app", orch_dst / "app")
    n2, b2 = copy_tree(orch_src / "tools", orch_dst / "tools")
    print("  orchestrator       %3d 文件 / %.1f MB" % (n1 + n2 + 1, (b1 + b2) / 1048576))

    print("复制迁移 …")
    mig_dst = PAYLOAD / "app" / "deploy" / "migrations"
    mig_dst.mkdir(parents=True)
    n_mig = 0
    for f in sorted((DEPLOY / "migrations").glob("*.sql")):
        shutil.copy2(f, mig_dst / f.name)
        n_mig += 1
    print("  migrations         %3d 个" % n_mig)

    print("复制 LS 品牌补丁 …")
    n3, b3 = copy_tree(PLATFORM / "labelstudio" / "brand",
                       PAYLOAD / "app" / "labelstudio" / "brand")
    print("  brand              %3d 文件 / %.1f MB" % (n3, b3 / 1048576))
    # apply_brand.py 会按 BRAND_DIR.parents[1]/web/public/brand 回找 PNG，
    # 目录层级不能压平，否则构建/打补丁时直接报找不到文件。
    n4, b4 = copy_tree(PLATFORM / "web" / "public" / "brand",
                       PAYLOAD / "app" / "web" / "public" / "brand")
    print("  web/public/brand   %3d 文件 / %.1f MB" % (n4, b4 / 1048576))

    print("写 Caddy 模板 …")
    (PAYLOAD / "caddy").mkdir(parents=True)
    (PAYLOAD / "caddy" / "Caddyfile.tpl").write_text(CADDY_TPL, encoding="utf-8",
                                                     newline="\n")

    cached = []
    if args.with_cache:
        cache_src = HERE / "cache"
        if cache_src.is_dir():
            (PAYLOAD / "cache").mkdir()
            # 官方 PG 是 .zip，备用源（Maven）是 .jar —— 两种都要收，
            # 只挑 .zip 会让断网安装拿不到备用包，等于白备。
            for z in sorted(list(cache_src.glob("*.zip")) + list(cache_src.glob("*.jar"))):
                print("预置 %s (%.1f MB)" % (z.name, z.stat().st_size / 1048576))
                shutil.copy2(z, PAYLOAD / "cache" / z.name)
                cached.append({"file": z.name,
                               "mb": round(z.stat().st_size / 1048576, 1),
                               "sha256": sha256(z)})

    manifest = {
        "app": "智途",
        "edition": "windows-native",
        "version": VERSION,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frontend_files": f_files,
        "migrations": n_mig,
        "prebuilt_cache": cached,
        "notes": [
            "原生 Windows 版：不依赖 Docker / WSL / 容器。",
            "cache/*.zip 存在时安装完全不联网；否则安装时下载 PostgreSQL 与 Caddy。",
            "前端是预构建静态产物，服务器上不需要 Node/npm。",
        ],
    }
    (PAYLOAD / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total = sum(f.stat().st_size for f in PAYLOAD.rglob("*") if f.is_file())
    print("\n" + "=" * 66)
    print("payload 已就绪：%s" % PAYLOAD)
    print("  总大小        : %.1f MB / %d 文件"
          % (total / 1048576, sum(1 for f in PAYLOAD.rglob("*") if f.is_file())))
    print("  预置下载包    : %s" % (", ".join(c["file"] for c in cached) or "无（安装时联网下载）"))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
