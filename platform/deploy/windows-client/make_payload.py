#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""组装载荷 payload/（EXE 的"食材"）。

交付目录长这样
----------------------------------------------------------------------------
    <发布目录>\\智途安装程序.exe
    <发布目录>\\智途监控.exe
    <发布目录>\\payload\\manifest.json
    <发布目录>\\payload\\compose\\docker-compose.yml
    <发布目录>\\payload\\compose\\env.example
    <发布目录>\\payload\\compose\\migrations\\*.sql
    <发布目录>\\payload\\compose\\caddy\\Caddyfile        ← 入口反代配置（必需）
    <发布目录>\\payload\\compose\\docker\\*.Dockerfile    ← 联网构建用
    <发布目录>\\payload\\compose\\src\\web\\**            ← 联网构建用（不含 node_modules）
    <发布目录>\\payload\\compose\\src\\orchestrator\\**   ← 联网构建用
    <发布目录>\\payload\\compose\\src\\labelstudio\\brand\\**
    <发布目录>\\payload\\images\\*.tar          ← docker save 产物（离线装必须自带）
    <发布目录>\\payload\\docker\\*.deb          ← 可选：离线装 Docker Engine 用

两种安装方式对载荷的要求不同
----------------------------------------------------------------------------
* **联网构建**（服务器能出外网）：必须有 caddy 配置 + docker/*.Dockerfile +
  src/**（源码树）。compose 里 build.context 写的是 `.`，Dockerfile 引用
  `src/web/…`、`src/orchestrator/…`、`src/labelstudio/brand`，缺任何一块
  现场 build 都会在"找不到 Dockerfile / COPY 源"这一步失败。源码总共约
  2 MB，比 2-3 GB 的镜像 tar 好带得多。
* **离线载入**：另外必须有 images/*.tar。

不管哪种方式，**caddy/Caddyfile 都是必需的**：compose 里 caddy 挂载
`./caddy/Caddyfile`，而官方 caddy 镜像自己不带这个文件 —— 缺了它，80 端口
那一路根本起不来，平台入口直接不可达（而且 caddy 容器会一直重启）。
Linux 那边由 install.sh 从 Caddyfile.domain / Caddyfile.ip 里选一个复制成
Caddyfile；Windows 客户端没有 install.sh，所以载荷里直接放好纯 IP 版。

用法
----------------------------------------------------------------------------
    python make_payload.py                 # 只铺 compose/源码/迁移/清单，不碰镜像
    python make_payload.py --save-images   # 顺便 docker save（需要本机有 docker）
    python make_payload.py --build-images --save-images   # 打包机全套
    python make_payload.py --images-from D:\\images   # 用已导出的 tar 目录
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent                      # platform/deploy
PAYLOAD = HERE / "payload"
MANIFEST_VERSION = time.strftime("%Y%m%d")


def compose_images(compose_file: Path) -> list[str]:
    """从 compose 里抠出所有 image: 值（够用，不引入 yaml 依赖）。"""
    images: list[str] = []
    text = compose_file.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r"^\s*image:\s*([^\s#]+)", text, re.M):
        ref = m.group(1).strip("\"'")
        # ${VAR} 形式的镜像引用交给人看：它说明这个服务是 build 出来的
        if not ref.startswith("$") and ref not in images:
            images.append(ref)
    return images


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# 联网构建要带过去的源码。排除项按**白名单思路**反过来列：
# 凡是不参与 docker build 的目录（依赖、构建产物、测试夹具、截图）一律不进，
# 因为它们会把载荷从 2 MB 涨到 80 MB，而且 node_modules 跨平台不可用。
SKIP_DIRS = {
    "node_modules", "dist", "dist-sso", ".vite", "coverage", "__pycache__",
    ".pytest_cache", ".mypy_cache", "_shots", "_shots_sso", "_shots-wired",
    "tools", "tests", ".git",
}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite3", ".map"}


def copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """递归复制源码树，跳过依赖/产物/测试。返回 (文件数, 字节数)。"""
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
                if s.stat().st_size > 40 * 1024 * 1024:   # 源码里不该有这么大的文件
                    print("  [跳过] 异常大文件 %s" % s.relative_to(src))
                    continue
                shutil.copy2(s, dst / rel / f)
                n += 1
                total += s.stat().st_size
            except OSError as exc:
                print("  [!] 复制失败 %s：%s" % (s, exc))
    return n, total


def copy_build_sources(comp_dst: Path) -> dict:
    """把联网构建需要的三样东西按 Dockerfile 期望的相对路径铺好。

    docker-compose.yml 里 build.context 是 `.`，三个 Dockerfile 分别引用
    `docker/xxx.Dockerfile`、`src/web/…`、`src/orchestrator/…`、
    `src/labelstudio/brand`。所以这棵树必须与仓库里 platform/deploy 的
    布局**逐字一致**，否则 docker build 第一步就报找不到文件。
    """
    out = {"files": 0, "mb": 0.0, "parts": []}

    # 1) Dockerfile
    d_dst = comp_dst / "docker"
    d_dst.mkdir(parents=True, exist_ok=True)
    for f in sorted((DEPLOY / "docker").glob("*.Dockerfile")):
        shutil.copy2(f, d_dst / f.name)
    out["parts"].append("docker/*.Dockerfile")

    # 2) 前端（Dockerfile 里 COPY src/web/ 整棵，再 npm run build）
    n, b = copy_tree(DEPLOY.parent / "web", comp_dst / "src" / "web")
    out["files"] += n
    out["mb"] += b / 1048576
    out["parts"].append("src/web (%d 文件)" % n)

    # 3) 编排层（只需 requirements.txt + app/）
    o_src = DEPLOY.parent / "services" / "orchestrator"
    o_dst = comp_dst / "src" / "orchestrator"
    o_dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(o_src / "requirements.txt", o_dst / "requirements.txt")
    n, b = copy_tree(o_src / "app", o_dst / "app")
    out["files"] += n + 1
    out["mb"] += b / 1048576
    out["parts"].append("src/orchestrator (%d 文件)" % (n + 1))

    # 4) LS 品牌补丁（apply_brand.py 会按相对路径回找 web/public/brand，
    #    所以 src/labelstudio/brand 与 src/web/public/brand 的层级不能压平）
    n, b = copy_tree(DEPLOY.parent / "labelstudio" / "brand",
                     comp_dst / "src" / "labelstudio" / "brand")
    out["files"] += n
    out["mb"] += b / 1048576
    out["parts"].append("src/labelstudio/brand (%d 文件)" % n)
    return out


def has_docker() -> bool:
    try:
        rc = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                            capture_output=True, timeout=25)
        return rc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def save_images(images: list[str]) -> list[Path]:
    img_dir = PAYLOAD / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for ref in images:
        name = re.sub(r"[/:@]", "_", ref) + ".tar"
        target = img_dir / name
        print("docker save %s → %s …" % (ref, target.name), flush=True)
        rc = subprocess.run(["docker", "save", "-o", str(target), ref]).returncode
        if rc != 0:
            print("  [!] 保存失败：%s（镜像可能没在本地，先 docker pull）" % ref)
            continue
        out.append(target)
    return out


def build_images(tag: str) -> int:
    """在打包机上把 build 型服务（web / orchestrator / labelstudio）打成镜像。

    离线交付的镜像必须**全部**来自本地构建产物：这三个服务在 install 机器上
    没法 build（要 npm/pip 源），所以只能在有网、有 Docker 的打包机上 build
    好，再 docker save 带过去。compose 里已给它们固定了 image 名
    （zhitu/<svc>:${ZHITU_TAG}），这里用同一个 tag 构建，两边才对得上。
    """
    print("docker compose build（tag=%s）…" % tag, flush=True)
    env = dict(os.environ)
    env["ZHITU_TAG"] = tag
    rc = subprocess.run(
        ["docker", "compose", "--project-directory", str(DEPLOY),
         "-f", str(DEPLOY / "docker-compose.yml"), "build"],
        env=env).returncode
    if rc != 0:
        print("[!] 构建失败（rc=%d）" % rc)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-images", action="store_true")
    ap.add_argument("--build-images", action="store_true",
                    help="先 docker compose build 再 save（打包机用）")
    ap.add_argument("--tag", default=MANIFEST_VERSION,
                    help="镜像 tag，必须与安装时的 ZHITU_TAG 一致")
    ap.add_argument("--images-from", default=None,
                    help="从已有 tar 的目录复制（打包机导出的产物）")
    args = ap.parse_args()

    comp_src = DEPLOY / "docker-compose.yml"
    if not comp_src.is_file():
        raise SystemExit("找不到 %s" % comp_src)

    PAYLOAD.mkdir(parents=True, exist_ok=True)
    comp_dst = PAYLOAD / "compose"
    comp_dst.mkdir(parents=True, exist_ok=True)
    # 落进 payload 的 compose 直接把 tag 定死：安装机上没有 ZHITU_TAG 这个
    # 环境变量，带 ${ZHITU_TAG:-dev} 会退化成 dev，和 load 进来的镜像对不上。
    raw = comp_src.read_text(encoding="utf-8")
    raw = raw.replace("${ZHITU_TAG:-dev}", args.tag)
    (comp_dst / "docker-compose.yml").write_text(raw, encoding="utf-8", newline="\n")

    mig_src = DEPLOY / "migrations"
    mig_dst = comp_dst / "migrations"
    mig_dst.mkdir(exist_ok=True)
    n_mig = 0
    for f in sorted(mig_src.glob("*.sql")):
        shutil.copy2(f, mig_dst / f.name)
        n_mig += 1

    env_src = DEPLOY / ".env.example"
    if env_src.is_file():
        shutil.copy2(env_src, comp_dst / "env.example")
    else:
        print("[!] 没找到 .env.example，安装程序生成 .env 时会缺少模板注释")

    # 入口反代配置：compose 挂载 ./caddy/Caddyfile，官方 caddy 镜像本身没有
    # 这个文件。缺了它不是"打开是默认页"，而是 caddy 反复重启 → 80 端口没了。
    # 这里固定用纯 IP 模板（Windows 客户端就是给 http://<服务器IP> 用的）。
    caddy_src = DEPLOY / "caddy" / "Caddyfile.ip"
    if not caddy_src.is_file():
        raise SystemExit("找不到 %s（caddy 容器挂载依赖它）" % caddy_src)
    (comp_dst / "caddy").mkdir(exist_ok=True)
    shutil.copy2(caddy_src, comp_dst / "caddy" / "Caddyfile")
    # 域名版一起带上，将来换 Caddyfile.domain 时不用重新打包
    for extra in ("Caddyfile.domain",):
        if (DEPLOY / "caddy" / extra).is_file():
            shutil.copy2(DEPLOY / "caddy" / extra, comp_dst / "caddy" / extra)

    src_rep = copy_build_sources(comp_dst)

    images = compose_images(comp_dst / "docker-compose.yml")

    if args.build_images and args.save_images:
        if not has_docker():
            raise SystemExit("--build-images 需要本机有可用的 docker")
        if build_images(args.tag) != 0:
            raise SystemExit("镜像构建失败，先修好再打包")
    if args.save_images:
        if not has_docker():
            print("[!] 本机没有可用的 docker，跳过 docker save（--save-images 无效）")
        else:
            save_images(images)
    if args.images_from:
        src_dir = Path(args.images_from)
        if not src_dir.is_dir():
            raise SystemExit("--images-from 指向的目录不存在：%s" % src_dir)
        img_dir = PAYLOAD / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        for tar in sorted(src_dir.glob("*.tar")):
            print("复制镜像包 %s (%.1f MB)" % (tar.name, tar.stat().st_size / 1048576))
            shutil.copy2(tar, img_dir / tar.name)

    img_dir = PAYLOAD / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    found = []
    for tar in sorted(img_dir.glob("*.tar")):
        found.append({"file": tar.name, "mb": round(tar.stat().st_size / 1048576, 1),
                      "sha256": sha256(tar)})

    manifest = {
        "app": "智途",
        "version": MANIFEST_VERSION,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "expected_images": images,
        "images": found,
        "migrations": n_mig,
        "build_sources": {
            "included": src_rep["files"] > 0,
            "files": src_rep["files"],
            "mb": round(src_rep["mb"], 2),
            "parts": src_rep["parts"],
        },
        "caddy": "caddy/Caddyfile（纯 IP 模板）",
        "notes": [
            "build_sources 存在 → 可以在能出外网的服务器上现场构建（联网安装）。",
            "images/*.tar 存在 → 可以完全离线 docker load（离线安装）。",
            "两者都没有 = 装不成；安装程序第 02 步会直接说明缺哪一块。",
            "docker/*.deb 可选：放进 docker-ce 相关 deb 后，安装程序在 WSL 里离线装引擎。",
        ],
    }
    (PAYLOAD / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # images 目录不要空着：留一句人话，免得运维以为打包脚本漏了
    if not found:
        (img_dir / "README.txt").write_text(
            "这个目录必须放 docker save 出来的 .tar 镜像包。\n"
            "在**有 Docker 的机器**上执行：\n"
            "    python make_payload.py --save-images\n"
            "然后把 payload/images/*.tar 一起交付。缺镜像 → 安装程序第 05 步直接失败。\n",
            encoding="utf-8")

    print("\n" + "=" * 68)
    print("payload 已就绪：%s" % PAYLOAD)
    print("  compose      : docker-compose.yml")
    print("  caddy 入口    : caddy/Caddyfile")
    print("  构建源码      : %d 文件 / %.2f MB" % (src_rep["files"], src_rep["mb"]))
    for p in src_rep["parts"]:
        print("      - %s" % p)
    print("  迁移脚本      : %d 个" % n_mig)
    print("  期望镜像      : %d 个" % len(images))
    for i in images:
        print("      - %s" % i)
    print("  已就位镜像    : %d 个 (%.1f MB)" % (
        len(found), sum(f["mb"] for f in found)))
    print("\n  联网安装      : %s" % ("可行（源码已带齐）" if src_rep["files"]
                                      else "不可行（缺源码树）"))
    print("  离线安装      : %s" % ("可行（镜像已带齐）" if found else "不可行（缺镜像包）"))
    if not found:
        print("      需要离线时在有 Docker 的机器上跑：python make_payload.py --save-images")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
