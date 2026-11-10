"""
FORGE · 企业统一登录前端链路验证（真浏览器）
================================================================
后端契约由 services/orchestrator/tests/e2e_sso.py 保证（23 项断言）。
但那套测试是直接用 HTTP 客户端模拟回调的，它证明不了三件只有浏览器能暴露的事：

  1. 登录页到底有没有「用企业账号登录」这个入口（且只在 SSO 启用时出现）；
  2. 点下去之后，浏览器会不会带着 code/state 回到 /#/sso/callback
     —— 哈希路由 + 302 拼接查询参数这一步最容易写错，错了的表现是
     "转一圈回登录页"，光看接口测试永远是绿的；
  3. 回调页拿到 token 后是否真的进了工作台（而不是白屏卡在中间态）。

所以这个脚本自己起一整套：假 IdP（带 /authorize 自动同意）+ 后端（SSO 开）
+ 静态服务（SSO 版前端），然后用 CDP 驱动真实 Edge/Chrome 走一遍。

前置
----
  1. 已构建 SSO 版前端（API 指向本脚本用的后端端口，默认 8303）：
       npm run build -- --outDir dist-sso     # 需 VITE_API_BASE=http://127.0.0.1:8303
     脚本会检查 dist-sso/index.html 是否存在，不存在直接报错，不偷偷用旧产物。
  2. 端口 8303 / 9401 / 9121 空闲。

用法
----
  python tools/verify_sso_ui.py
  python tools/verify_sso_ui.py --api-port 8303 --web-port 9121 --name 单点登录测试
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[1]
PLATFORM = WEB_ROOT.parent
ORCH = PLATFORM / "services" / "orchestrator"

sys.path.insert(0, str(ORCH / "tests"))
sys.path.insert(0, str(WEB_ROOT / "tools"))

from e2e_sso import (  # noqa: E402
    CLIENT_ID,
    CLIENT_SECRET,
    FakeIdp,
    wait_up,
)
from verify_auth_ui import Browser, check, report  # noqa: E402

# 回调地址必须与后端 FORGE_SSO_REDIRECT_URI 完全一致，否则 IdP 换码会拒。
# 这里的端口由 --web-port 决定，所以 REDIRECT_URI 要跟着改（见 main）。
REDIRECT_TMPL = "http://127.0.0.1:{port}/#/sso/callback"


def fresh_phone() -> str:
    return "+8619" + str(secrets.randbelow(10 ** 8)).zfill(8)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-port", type=int, default=8303)
    ap.add_argument("--idp-port", type=int, default=9401)
    ap.add_argument("--web-port", type=int, default=9121)
    ap.add_argument("--dist", default="dist-sso")
    ap.add_argument("--name", default="单点登录测试")
    ap.add_argument("--out", default="_shots_sso")
    ap.add_argument("--browser", default=None)
    args = ap.parse_args()

    dist = (WEB_ROOT / args.dist).resolve()
    if not (dist / "index.html").exists():
        raise SystemExit(
            f"找不到 {dist}/index.html。先构建 SSO 版前端：\n"
            f"  cd {WEB_ROOT}\n"
            f"  VITE_API_BASE=http://127.0.0.1:{args.api_port} "
            f"npm run build -- --outDir {args.dist}"
        )

    web = f"http://127.0.0.1:{args.web_port}"
    api = f"http://127.0.0.1:{args.api_port}"
    redirect_uri = REDIRECT_TMPL.format(port=args.web_port)

    # e2e_sso 模块导入时会清掉代理变量；这里再确认一次，避免本地代理劫持
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    idp = FakeIdp(args.idp_port)
    # /authorize 被访问时"自动同意"，签出这个人
    idp.auto_login_claims = {
        "sub": "idp-sub-ui-" + secrets.token_hex(4),
        "name": args.name,
        "phone_number": fresh_phone(),
        "email": "sso-ui@example.cn",
        # 故意塞一个高权限声明：前端/后端都不应该理会它
        "roles": ["platform_admin"],
    }
    idp.start()
    print(f"假 IdP：{idp.base}")

    env = dict(os.environ)
    env.update({
        "FORGE_AUTH_MODE": "native",
        "FORGE_JWT_SECRET": os.environ.get("FORGE_JWT_SECRET") or secrets.token_hex(48),
        "FORGE_PORT": str(args.api_port),
        "FORGE_SSO_ISSUER": idp.base,
        "FORGE_SSO_CLIENT_ID": CLIENT_ID,
        "FORGE_SSO_CLIENT_SECRET": CLIENT_SECRET,
        "FORGE_SSO_REDIRECT_URI": redirect_uri,
        "FORGE_SSO_PROVIDER": "oidc",
        "FORGE_SSO_ALLOW_JIT": "true",
        "FORGE_SSO_JIT_ROLE": "annotator",
        "FORGE_SSO_FRONTEND_REDIRECT": "/#/sso/callback",
        "FORGE_CORS_ORIGINS": web,
    })
    # 注意：这里的 redirect_uri 与 e2e_sso 里的常量端口不同，
    # 而假 IdP 的 /token 与 /authorize 都比对那个模块级常量 —— 所以临时覆盖。
    import e2e_sso as _es

    _es.REDIRECT_URI = redirect_uri

    api_log = Path(os.environ.get("TEMP", ".")) / "forge-sso-ui-api.log"
    api_fh = open(api_log, "w", encoding="utf-8")
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "app.serve"], cwd=str(ORCH), env=env,
        stdout=api_fh, stderr=subprocess.STDOUT,
    )

    static_proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(args.web_port),
         "--bind", "127.0.0.1", "--directory", str(dist)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        if not wait_up(api):
            tail = api_log.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise SystemExit(f"后端 {api} 起不来：\n{tail}")

        out = (WEB_ROOT / args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        b = Browser(_find_browser(args.browser), out)
        try:
            # --- 1. 登录页应有企业账号入口 ---------------------------------
            b.goto(f"{web}/#/login")
            time.sleep(1.2)
            body = b.js("document.body.innerText") or ""
            check("用企业账号登录" in body, "登录页出现「用企业账号登录」入口")
            b.shot("sso-01-login")

            # --- 2. 点下去 → 走 IdP → 回到回调页 → 进工作台 ----------------
            clicked = bool(b.js(
                "(() => { const el = [...document.querySelectorAll('button')]"
                ".find(x => (x.textContent || '').includes('用企业账号登录'));"
                " if (!el) return false; el.click(); return true; })()"
            ))
            check(clicked, "点击企业账号登录按钮")

            # 完整往返：本应用 → IdP(/authorize 302) → 回调页 → 换码 → 工作台
            landed = ""
            for _ in range(40):
                time.sleep(0.5)
                landed = b.js("location.hash") or ""
                if landed and landed != "#/login" and not landed.startswith("#/sso"):
                    break
            check(landed in ("#/", "#") or landed.startswith("#/overview"),
                  "SSO 登录后进入工作台（哈希路由交接正确）", f"hash={landed!r}")
            b.shot("sso-02-overview")

            token = b.js("localStorage.getItem('forge.platform.token')") or ""
            check(isinstance(token, str) and token.count(".") == 2,
                  "存下的是本服务自签 JWT（三段式），不是对方 IdP 的 token",
                  f"prefix={token[:12]!r}")

            shell = b.js("document.body.innerText") or ""
            check(args.name in shell, "界面显示 IdP 带来的姓名", args.name)
            # 关键：IdP 在 id_token 里塞了 roles=["platform_admin"]，
            # 若界面出现"平台管理员"就说明授权被外部接管了。
            check("平台管理员" not in shell,
                  "id_token 里的 roles 没有变成平台角色（授权仍在我方）")

            # --- 3. 退出后回到登录页 ---------------------------------------
            ok = b.js(
                "(() => { const el = document.querySelector('[title=\"退出登录\"]');"
                " if (!el) return false; el.click(); return true; })()"
            )
            time.sleep(1.2)
            h = b.js("location.hash") or ""
            check(bool(ok) and h.startswith("#/login"), "退出后回到登录页", f"hash={h!r}")
            tok2 = b.js("localStorage.getItem('forge.platform.token')")
            check(not tok2, "退出后 token 已清除", f"token={tok2!r}")
            b.shot("sso-03-after-logout")
        finally:
            b.close()
            # 每跑一次都会在库里留一个 JIT 账号（本脚本用的号段 +8619），
            # 不清的话跑十次就有十个"单点登录测试"。复用 e2e_sso 里那套清理。
            try:
                from e2e_sso import reset_fake_idp_residue

                print(reset_fake_idp_residue())
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 残留清理失败（不影响本次结论）：{exc}")
        return report(b, out)
    finally:
        api_proc.terminate()
        static_proc.terminate()
        for p in (api_proc, static_proc):
            try:
                p.wait(timeout=8)
            except Exception:  # noqa: BLE001
                p.kill()
        api_fh.close()
        idp.stop()


def _find_browser(explicit: str | None) -> str:
    from verify_auth_ui import find_browser

    return find_browser(explicit)


if __name__ == "__main__":
    sys.exit(main())
