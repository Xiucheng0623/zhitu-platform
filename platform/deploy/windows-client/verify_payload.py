#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交付前校验：payload/ 与安装落盘结果是否真的能撑起一次安装。

为什么需要这个脚本
----------------------------------------------------------------------------
本机（开发机）没有 Docker、也跑不了 wsl.exe，所以"装一遍试试"是不可能的。
但安装失败的绝大多数原因**不是运行时问题，而是缺文件**：
  * compose 挂载 ./caddy/Caddyfile，官方 caddy 镜像不带它 → 入口起不来；
  * 联网构建的 build.context 是 `.`，Dockerfile 按 src/web/… 取文件，
    载荷里少了源码树 → build 第一步就报找不到 Dockerfile；
  * 迁移少了 .sql → 库结构建不起来；
  * .env 少了公开地址 → 能登录但进不去标注工作台。
这四类都能在**没有 Docker 的前提下**静态验出来，本脚本就干这件事。

它验证的边界（诚实清单）
----------------------------------------------------------------------------
验得了：文件齐不齐、路径能不能解析、落盘有没有丢、.env 必填项对不对。
验不了：镜像拉不拉得到、build 会不会因为 npm/pip 源超时失败、容器能不能起来。
最后那句话只能在真服务器上跑一遍才知道。

用法
----------------------------------------------------------------------------
    python verify_payload.py            # 全量校验，退出码 0 = 通过
    python verify_payload.py --quiet    # 只打失败项
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

import zhitu_common as C  # noqa: E402
import zhitu_setup as S   # noqa: E402

OK, BAD = 0, 0
QUIET = "--quiet" in sys.argv


def check(cond: bool, label: str, detail: str = "") -> bool:
    global OK, BAD
    if cond:
        OK += 1
        if not QUIET:
            print("  OK   " + label)
    else:
        BAD += 1
        print("  FAIL " + label + (("  ← " + detail) if detail else ""))
    return cond


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def rels(root: Path) -> dict:
    """目录下所有文件的 {相对路径: sha256}。"""
    out = {}
    for r, _d, fs in os.walk(root):
        for f in fs:
            p = Path(r) / f
            out[str(p.relative_to(root)).replace(os.sep, "/")] = sha(p)
    return out


def main() -> int:
    print("=" * 70)
    print("智途 Windows 客户端 · 交付前校验")
    print("=" * 70)

    # -- 1. 载荷静态结构 ---------------------------------------------------
    print("\n[1] payload 结构")
    rep = C.validate_payload()
    check(rep["exists"], "payload/ 存在", rep["dir"])
    check(not rep["problems"], "核心文件齐备",
          "; ".join(rep["problems"]))
    check(rep["caddy"], "caddy/Caddyfile 存在（平台 80 端口依赖）")
    check(rep["build_src"], "联网构建源码树齐备", "; ".join(rep["need_build"]))
    check(rep["migrations"] >= 10, "迁移脚本 >= 10 个", "实际 %d" % rep["migrations"])
    online_capable = rep["build_src"] and not rep["problems"]
    offline_capable = bool(rep["images"]) and not rep["problems"]
    check(online_capable or offline_capable,
          "至少一种安装方式可行",
          "联网=%s 离线=%s" % (online_capable, offline_capable))
    check(rep["manifest"] is not None, "manifest.json 可解析")
    if rep["manifest"]:
        bs = (rep["manifest"].get("build_sources") or {})
        check(bool(bs.get("included")), "manifest 记录了 build_sources",
              "重跑 make_payload.py")

    # -- 2. 落盘不丢文件 ---------------------------------------------------
    print("\n[2] 落盘（payload/compose → 安装目录/deploy）")
    tmp = Path(tempfile.mkdtemp(prefix="zhitu-verify-"))
    # 把状态文件隔离到临时目录：否则 step_files 会把假 IP / 假安装目录写进
    # 用户真实的 client.json，监控台之后就一直显示那个假地址。
    os.environ["ZHITU_CONFIG_DIR"] = str(tmp / "cfg")
    try:
        logs = []
        inst = S.Installer(logs.append, install_dir=str(tmp),
                           host_ip="10.0.0.9", online=True)
        ok_p, msg_p = inst.step_payload()
        check(ok_p, "第 02 步 step_payload", msg_p)
        ok_f, msg_f = inst.step_files()
        check(ok_f, "第 06 步 step_files", msg_f)
        dep = tmp / "deploy"

        src_map = rels(C.payload_dir() / "compose")
        dst_map = rels(dep)
        missing = sorted(set(src_map) - set(dst_map) - {".env"})
        check(not missing, "payload/compose 全部落盘", "漏 %d 个：%s" % (
            len(missing), ", ".join(missing[:5])))
        differ = [k for k in src_map if k in dst_map and src_map[k] != dst_map[k]]
        check(not differ, "落盘内容逐字节一致", "不一致 %d 个" % len(differ))

        # -- 3. compose 里的相对引用是否都能解析 ---------------------------
        print("\n[3] compose 引用解析（build 与 bind mount）")
        text = (dep / "docker-compose.yml").read_text(encoding="utf-8")
        # 注意：本机 Bash 会把反斜杠转成斜杠，所以这里的正则全部只用于
        # Python 内部（脚本文件里的 \s 是真正的转义，不受 shell 影响）。
        for m in re.finditer(r"dockerfile:\s*(\S+)", text):
            p = dep / m.group(1)
            check(p.is_file(), "dockerfile 存在：" + m.group(1))
        for m in re.finditer(r"context:\s*(\S+)", text):
            p = (dep / m.group(1)).resolve()
            check(p.is_dir(), "build context 存在：" + m.group(1))
        for m in re.finditer(r"-\s*\./([^\s:]+):(/[^\s:]*)", text):
            p = dep / m.group(1)
            check(p.exists(), "bind mount 源存在：" + m.group(1))
        for m in re.finditer(r"^\s*-\s*\./([A-Za-z0-9_.\-]+):", text, re.M):
            p = dep / m.group(1)
            check(p.exists(), "挂载目录存在：" + m.group(1))

        # -- 4. .env -------------------------------------------------------
        print("\n[4] .env")
        env = C.read_env(dep / ".env")
        check(bool(env), ".env 已生成")
        prox = C.check_env_file(dep / ".env")
        check(not prox, ".env 必填项齐备", "; ".join(prox))
        for k in ("PLATFORM_PUBLIC_URL", "LS_PUBLIC_URL"):
            v = env.get(k) or ""
            check(v.startswith("http://") and "127.0.0.1" not in v,
                  "%s 是可被员工访问的地址" % k, v)
        check(env.get("HTTP_PORT") == "80", "HTTP_PORT=80", str(env.get("HTTP_PORT")))
        check(env.get("LS_PORT") == "8100", "LS_PORT=8100", str(env.get("LS_PORT")))
        check(env.get("LS_REFRESH_TOKEN") == "",
              "LS_REFRESH_TOKEN 交给第 10 步补（不能预填假的）",
              repr(env.get("LS_REFRESH_TOKEN")))
        sec = [k for k in env if k.endswith(("_SECRET", "_TOKEN", "_PASSWORD"))
               and env[k] and k != "LS_REFRESH_TOKEN"]
        check(len(sec) >= 6, "随机密钥已生成（%d 项）" % len(sec))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("ZHITU_CONFIG_DIR", None)

    # -- 5. 步骤表一致性 ---------------------------------------------------
    print("\n[5] 步骤表")
    app_steps = None
    try:
        app = S.SetupApp()
        app.root.withdraw()
        app_steps = [s.title for s in app.steps]
        app.root.destroy()
    except Exception as exc:  # noqa: BLE001
        check(False, "GUI 步骤表可构建", str(exc))
    if app_steps:
        check(len(app_steps) == 11, "GUI 共 11 步", str(len(app_steps)))
        check(any("LS 长期凭证" in t for t in app_steps), "包含「LS 长期凭证」步骤")
        check(app_steps[-1].startswith("11"), "末步编号 11", app_steps[-1])
    # --auto 的步骤表不能和 GUI 漂移（两处各写一遍，最易忘改）
    src = (HERE / "src" / "zhitu_setup.py").read_text(encoding="utf-8")
    check('("LS 长期凭证", inst.step_token)' in src,
          "--auto 步骤表也含「LS 长期凭证」")

    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (OK, BAD))
    print("=" * 70)
    if BAD:
        print("\n失败项见上。注意：本脚本验不了「镜像拉不拉得到 / build 会不会超时 /\n"
              "容器能不能起来」—— 那三件事只能在真服务器上跑一次安装才知道。")
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
