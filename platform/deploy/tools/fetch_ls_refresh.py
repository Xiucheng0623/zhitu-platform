#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录 Label Studio 并把 API refresh token 打印到 stdout。

为什么需要这个脚本
----------------------------------------------------------------------------
orchestrator 调 LS 业务接口（建项目/导入任务）需要一个长期 token。
LS 1.23 的 access token 只有 5 分钟，长期凭证是 /api/token/ 返回的
refresh 字段（也就是账号设置里的 Personal Access Token）。

install.sh 要做到「一条命令部署完」，不能让用户先手工登录一遍 LS。
本脚本自动完成：拿 CSRF → 表单登录 → POST /api/token/ → 打印 refresh。

设计约束：只用标准库（urllib + http.cookiejar + re + json），
目标服务器上不用 pip 装任何东西。token 只进 stdout，日志走 stderr，
方便 install.sh 直接 `TOKEN=$(python3 tools/fetch_ls_refresh.py ...)`。

用法：
    python3 tools/fetch_ls_refresh.py \
        --ls-url http://127.0.0.1:8100 --email admin@x.cn --password 'xxx'
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def log(msg: str) -> None:
    print(f"[fetch_ls_refresh] {msg}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="获取 LS API refresh token")
    ap.add_argument("--ls-url", required=True, help="LS 地址，如 http://127.0.0.1:8100")
    ap.add_argument("--email", required=True, help="LS 管理员邮箱（LS_ADMIN_EMAIL）")
    ap.add_argument("--password", required=True, help="LS 管理员密码（LS_ADMIN_PASSWORD）")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    base = args.ls_url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # ---- 1. 登录页：拿 csrfmiddlewaretoken -------------------------------
    try:
        html = op.open(base + "/user/login/", timeout=args.timeout).read().decode(
            "utf-8", "replace"
        )
    except (urllib.error.URLError, OSError) as exc:
        log(f"打不开登录页 {base}/user/login/：{exc}")
        return 1
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    if not m:
        log("登录页里找不到 csrfmiddlewaretoken（LS 版本变了？）")
        return 1
    csrf = m.group(1)

    # ---- 2. 表单登录（字段名 email/password 是 LS 1.23 实测）------------
    body = urllib.parse.urlencode(
        {"csrfmiddlewaretoken": csrf, "email": args.email, "password": args.password}
    ).encode()
    req = urllib.request.Request(
        base + "/user/login/",
        data=body,
        headers={"Referer": base + "/user/login/"},
    )
    try:
        resp = op.open(req, timeout=args.timeout)
    except urllib.error.HTTPError as exc:
        log(f"登录请求失败 HTTP {exc.code}")
        return 1
    # 登录成功 Django 302 到 /projects/；失败回到 /user/login/ 渲染表单错误。
    # urllib 会自动跟随重定向，所以看最终 URL 即可。
    if "/user/login" in resp.geturl():
        log("登录失败：邮箱或密码错误（核对 .env 里 LS_ADMIN_EMAIL / LS_ADMIN_PASSWORD）")
        return 1
    log(f"登录成功，落在 {resp.geturl()}")

    # ---- 3. 换 token：POST /api/token/ ----------------------------------
    # 需要带 CSRF 头（session 认证的写接口）+ 登录 cookie（jar 自动带）
    csrf2 = next((c.value for c in jar if c.name == "csrftoken"), csrf)
    req2 = urllib.request.Request(
        base + "/api/token/",
        data=b"{}",
        headers={
            "Referer": base + "/",
            "X-CSRFToken": csrf2,
            "Content-Type": "application/json",
        },
    )
    try:
        data = json.load(op.open(req2, timeout=args.timeout))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log(f"POST /api/token/ 失败：{exc}")
        return 1

    refresh = data.get("refresh") or (data.get("data") or {}).get("refresh")
    if not refresh:
        log(f"/api/token/ 没返回 refresh 字段：{json.dumps(data)[:300]}")
        return 1

    # token 只进 stdout，方便命令替换直接取用
    print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
