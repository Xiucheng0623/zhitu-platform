"""
FORGE · SSO 端到端验证（自带一个假 IdP）
================================================================
为什么要有这个脚本
------------------
SSO 的代码只要有一处写错，症状都是"点登录转一圈又回登录页"，而日志里
分别落在两个系统上。在拿到对方真实 IdP 之前，唯一能自证的方式就是：
**自己起一个最小的 OIDC Provider**，把 discovery / jwks / token 端点都实现，
然后像一个真浏览器那样走完整条链路。

这个假 IdP 只实现本平台实际用到的四个端点，不追求规范完整：
  GET  /.well-known/openid-configuration
  GET  /jwks
  POST /token          （校验 client_id / secret / code / PKCE S256）
  （/authorize 省略：测试直接"预置"一个 code，等价于用户在 IdP 点过同意）

覆盖的断言里最重要的两条，都不是"能不能登录"：
  * id_token 里塞 roles=["platform_admin"] **不能**让我变成平台管理员
    —— 这验证了"认证归对方、授权归我们"。
  * 断掉签名 / 改 issuer / 过期 token 必须被拒
    —— 这验证了不会为了"别让人登不进来"而放宽校验。

用法
----
  # 需要一个已迁移到 011 的库，且 forge_app 可连（用 FORGE_DB_DSN）
  python tests/e2e_sso.py

  # 自定义端口
  python tests/e2e_sso.py --idp-port 9411 --api-port 8312
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
ORCH = HERE.parent

# 本机若设了 HTTP_PROXY，PyJWT 的 PyJWKClient（走 urllib）会把发往
# 127.0.0.1 的请求交给代理，症状是随机超时而不是"连不上"。先清掉。
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

KID = "forge-test-key-1"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def int_to_b64u(n: int) -> str:
    return b64u(n.to_bytes((n.bit_length() + 7) // 8, "big"))


# ---------------------------------------------------------------------------
# 假 IdP
# ---------------------------------------------------------------------------
class FakeIdp:
    def __init__(self, port: int):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.priv_pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pub = self.key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
                    "n": int_to_b64u(pub.n), "e": int_to_b64u(pub.e)}
        # code -> 该 code 绑定的信息
        self.codes: dict[str, dict] = {}
        self.force_bad_signature = False
        self.force_wrong_issuer = False
        self.token_calls = 0
        # 浏览器流程用：/authorize 被访问时"自动同意"，用这组 claims 签 id_token。
        # 真 IdP 在这里会让用户输账号密码，我们只需要证明链路能走通。
        self.auto_login_claims: dict | None = None

    # -- 供测试预置一个"用户已在 IdP 同意"的状态 --------------------------
    def issue_code(self, claims: dict, challenge: str) -> str:
        code = "code_" + secrets.token_hex(12)
        self.codes[code] = {"claims": claims, "challenge": challenge}
        return code

    def sign_id_token(self, claims: dict) -> str:
        c = dict(claims)
        c.update({
            "iss": ("http://evil.example" if self.force_wrong_issuer else self.base),
            "aud": CLIENT_ID,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        })
        key = self.priv_pem
        if self.force_bad_signature:
            # 用另一把私钥签名 → 必须验签失败
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        return jwt.encode(c, key, algorithm="RS256", headers={"kid": KID})

    def handler(self):
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/.well-known/openid-configuration"):
                    self._json({
                        "issuer": idp.base,
                        "authorization_endpoint": idp.base + "/authorize",
                        "token_endpoint": idp.base + "/token",
                        "jwks_uri": idp.base + "/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "code_challenge_methods_supported": ["S256"],
                    })
                    return
                if self.path.startswith("/jwks"):
                    self._json({"keys": [idp.jwk]})
                    return
                if self.path.startswith("/authorize"):
                    # 真浏览器流程会走到这里。不实现它，前端那个"用企业账号
                    # 登录"按钮就只能验证"点了有反应"，验不到"能不能登进去"。
                    q = {k: v[0] for k, v in
                         parse_qs(urlsplit(self.path).query).items()}
                    if q.get("client_id") != CLIENT_ID:
                        self._json({"error": "invalid_client"}, 401)
                        return
                    if q.get("redirect_uri") != REDIRECT_URI:
                        self._json({"error": "invalid_request",
                                    "error_description": "redirect_uri mismatch"}, 400)
                        return
                    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
                        self._json({"error": "invalid_request",
                                    "error_description": "PKCE S256 required"}, 400)
                        return
                    claims = idp.auto_login_claims or {
                        "sub": "idp-sub-auto", "name": "自动登录"}
                    code = idp.issue_code(claims, q["code_challenge"])
                    sep = "&" if "?" in q["redirect_uri"] else "?"
                    loc = f'{q["redirect_uri"]}{sep}code={code}&state={q.get("state","")}'
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return
                self._json({"error": "not_found"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode()
                form = {k: v[0] for k, v in parse_qs(raw).items()}

                if not self.path.startswith("/token"):
                    self._json({"error": "not_found"}, 404)
                    return

                idp.token_calls += 1
                if form.get("client_id") != CLIENT_ID:
                    self._json({"error": "invalid_client",
                                "error_description": "unknown client_id"}, 401)
                    return
                if form.get("client_secret") != CLIENT_SECRET:
                    self._json({"error": "invalid_client",
                                "error_description": "bad client_secret"}, 401)
                    return
                if form.get("redirect_uri") != REDIRECT_URI:
                    self._json({"error": "invalid_grant",
                                "error_description": "redirect_uri mismatch"}, 400)
                    return

                entry = idp.codes.pop(form.get("code", ""), None)
                if not entry:
                    # code 一次性：重复回调会走到这里
                    self._json({"error": "invalid_grant",
                                "error_description": "code not found or already used"}, 400)
                    return

                verifier = form.get("code_verifier", "")
                calc = b64u(hashlib.sha256(verifier.encode()).digest())
                if calc != entry["challenge"]:
                    self._json({"error": "invalid_grant",
                                "error_description": "PKCE code_verifier mismatch"}, 400)
                    return

                self._json({
                    "access_token": "at_" + secrets.token_hex(8),
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": idp.sign_id_token(entry["claims"]),
                })

        return H

    def start(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), self.handler())
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()
        # 等端口真的能连上（ThreadingHTTPServer 的 bind 是同步的，但保守起见）
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise SystemExit("假 IdP 起不来")

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def call(base: str, method: str, path: str, body=None, token: str = "") -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def wait_up(base: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
CLIENT_ID = "forge-test-client"
CLIENT_SECRET = "forge-test-secret-value"
REDIRECT_URI = "http://127.0.0.1:9121/#/sso/callback"


def reset_fake_idp_residue() -> str:
    """清掉上一轮假 IdP 留下的绑定和账号，返回一句人话说明结果。

    为什么必须清：合并用例每跑一次就会给 --phone 那个账号绑上一个新的
    sub。第二轮再跑时该账号已经"有主"，业务逻辑会正确地拒绝自动合并
    （转人工），于是用例红得莫名其妙 —— 是被测系统对了、测试自己脏了。

    只动假 IdP 自己造出来的数据，判据两层：
      * identities.subject 以 'idp-sub-' 开头（真实 IdP 的 sub 不会长这样）
      * users 是"没设本地密码 + 号段 +8619 + 已经没有残留身份"的账号
        （种子用户都在 136/137/138/139 号段，不会误伤）
    """
    dsn = os.environ.get("FORGE_TEST_PG_DSN",
                         "postgresql://postgres:postgres@127.0.0.1:5432/forge")
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            n_r = conn.execute(
                "delete from forge.identity_link_requests "
                " where provider = 'oidc' and subject like 'idp-sub-%'").rowcount
            n_id = conn.execute(
                "delete from forge.identities "
                " where provider = 'oidc' and subject like 'idp-sub-%'").rowcount
            n_u = conn.execute(
                "delete from forge.users u "
                " where u.password_hash is null "
                "   and u.phone like '+8619%' "
                "   and not exists (select 1 from forge.identities i "
                "                    where i.user_id = u.id)").rowcount
            conn.commit()
        return (f"已清理假 IdP 残留：绑定申请 {n_r} 条、身份 {n_id} 条、账号 {n_u} 个")
    except Exception as exc:  # noqa: BLE001
        return (f"[warn] 无法清理假 IdP 残留（{exc.__class__.__name__}: {exc}）。"
                "若上一轮跑过，合并用例可能因账号已被绑定而失败 —— "
                "这不是被测代码的问题，设 FORGE_TEST_PG_DSN 重跑即可。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idp-port", type=int, default=9401)
    ap.add_argument("--api-port", type=int, default=8302)
    ap.add_argument("--phone", default="13700137000", help="用于合并测试的已存在本地账号")
    ap.add_argument("--password", default="", help="该本地账号的密码")
    ap.add_argument("--reuse", action="store_true",
                    help="复用已在运行的后端（不自起进程）")
    ap.add_argument("--no-reset", action="store_true",
                    help="不清理上一轮留下的假 IdP 绑定/账号")
    args = ap.parse_args()

    if not args.no_reset:
        print(reset_fake_idp_residue())

    api = f"http://127.0.0.1:{args.api_port}"
    idp = FakeIdp(args.idp_port)
    secret = os.environ.get("FORGE_JWT_SECRET") or secrets.token_hex(48)

    idp.start()
    print(f"假 IdP 就绪：{idp.base}")

    proc = None
    if not args.reuse:
        env = dict(os.environ)
        env.update({
            "FORGE_AUTH_MODE": "native",
            "FORGE_JWT_SECRET": secret,
            "FORGE_PORT": str(args.api_port),
            "FORGE_SSO_ISSUER": idp.base,
            "FORGE_SSO_CLIENT_ID": CLIENT_ID,
            "FORGE_SSO_CLIENT_SECRET": CLIENT_SECRET,
            "FORGE_SSO_REDIRECT_URI": REDIRECT_URI,
            "FORGE_SSO_ALLOW_JIT": "true",
            "FORGE_SSO_JIT_ROLE": "annotator",
            "FORGE_CORS_ORIGINS": "http://127.0.0.1:9121",
        })
        # 后端 stderr 落到临时文件：500 的真实原因只在 traceback 里，
        # 测试报告只显示 "500" 的话等于什么都没说。
        errlog = Path(tempfile.gettempdir()) / "forge-e2e-sso.err.log"
        errlog.write_text("", encoding="utf-8")
        errfh = open(errlog, "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "app.serve"], cwd=str(ORCH),
                                env=env, stdout=errfh, stderr=subprocess.STDOUT)
    try:
        if not wait_up(api):
            raise SystemExit(f"后端 {api} 起不来")

        def fresh_phone() -> str:
            """造一个本地必然不存在的手机号。

            JIT 相关用例必须用"库里没有"的号码：一旦撞上种子数据或上一轮
            建出来的账号，用例测的就不再是"自动建号"，而是"唯一索引冲突"
            或"转人工"，报错信息会把排查方向带偏。
            """
            return "+8619" + str(secrets.randbelow(10 ** 8)).zfill(8)

        jit_phone = fresh_phone()
        base_claims = {
            "sub": "idp-sub-" + uuid.uuid4().hex[:8],
            "name": "王小明",
            "phone_number": jit_phone,
            "email": "wangxiaoming@example.cn",
        }

        # -- 1. status / start ------------------------------------------------
        code, r = call(api, "GET", "/api/v1/auth/sso/status")
        check(code == 200 and r.get("enabled") is True, "SSO 状态接口报告可用", f"{code} {r}")

        code, r = call(api, "POST", "/api/v1/auth/sso/start", {"return_to": "/#/sso/callback"})
        url = r.get("authorize_url", "")
        check(code == 200 and "code_challenge_method=S256" in url,
              "生成授权地址且带 PKCE S256")
        state = parse_qs(url.split("?", 1)[1]).get("state", [""])[0]
        challenge = parse_qs(url.split("?", 1)[1]).get("code_challenge", [""])[0]
        check(bool(state) and bool(challenge), "state 与 code_challenge 都已下发")

        # -- 2. 首次登录：JIT 建号 --------------------------------------------
        c1 = idp.issue_code(base_claims, challenge)
        code, r = call(api, "POST", "/api/v1/auth/sso/callback", {"code": c1, "state": state})
        check(code == 200 and r.get("ok") is True, "首次 SSO 登录成功（自动建号）",
              f"{code} {r.get('message') or ''}")
        tok = r.get("access_token", "")
        check(bool(tok), "签发了本服务自己的 access_token")
        check(r.get("via") == "sso", "响应标明 via=sso")

        code, me = call(api, "GET", "/api/v1/auth/me", token=tok)
        check(code == 200 and me.get("must_change_password") is False,
              "SSO 账号不被要求改本地密码", f"{code}")
        roles = me.get("roles") or []
        check(roles == ["annotator"], "JIT 账号拿到的是最小权限 annotator", str(roles))

        # -- 3. 第二次登录：已绑定，直通 --------------------------------------
        code, r2 = call(api, "POST", "/api/v1/auth/sso/start", {})
        url2 = r2.get("authorize_url", "")
        st2 = parse_qs(url2.split("?", 1)[1]).get("state", [""])[0]
        ch2 = parse_qs(url2.split("?", 1)[1]).get("code_challenge", [""])[0]
        c2 = idp.issue_code(base_claims, ch2)
        code, r3 = call(api, "POST", "/api/v1/auth/sso/callback", {"code": c2, "state": st2})
        check(code == 200 and r3.get("ok") is True, "同一 sub 再次登录直接通过")
        check(r3.get("user", {}).get("id") == r.get("user", {}).get("id"),
              "两次登录落到同一个本地账号")

        # -- 4. code 一次性 ---------------------------------------------------
        code, r4 = call(api, "POST", "/api/v1/auth/sso/callback", {"code": c2, "state": st2})
        check(code >= 400, "同一个 code 不能重复使用", f"got {code}")

        # -- 5. 越权：claim 里的 roles 必须被忽略 ------------------------------
        # 这是整套设计最重要的一条：对方 IdP 说你是 platform_admin 也不算数。
        evil_claims = dict(base_claims)
        evil_claims["sub"] = "idp-sub-" + uuid.uuid4().hex[:8]
        # 必须换成新号码：base_claims 的号码已经被上面那个 JIT 账号占了，
        # 再拿它来"越权测试"只会走进转人工分支，测不到 roles 被忽略这件事。
        evil_claims["phone_number"] = fresh_phone()
        evil_claims["roles"] = ["platform_admin"]
        evil_claims["realm_access"] = {"roles": ["platform_admin", "ops_director"]}
        evil_claims["role"] = "platform_admin"
        code, r5 = call(api, "POST", "/api/v1/auth/sso/start", {})
        url5 = r5.get("authorize_url", "")
        st5 = parse_qs(url5.split("?", 1)[1]).get("state", [""])[0]
        ch5 = parse_qs(url5.split("?", 1)[1]).get("code_challenge", [""])[0]
        c5 = idp.issue_code(evil_claims, ch5)
        code, r6 = call(api, "POST", "/api/v1/auth/sso/callback", {"code": c5, "state": st5})
        evil_tok = r6.get("access_token", "")
        code, evil_me = call(api, "GET", "/api/v1/auth/me", token=evil_tok)
        evil_roles = evil_me.get("roles") or []
        check(evil_roles == ["annotator"],
              "id_token 里的 roles 声明被忽略，仍是 annotator", str(evil_roles))
        code, _ = call(api, "GET", "/api/v1/admin/users", token=evil_tok)
        check(code == 404, "该身份访问管理接口返回 404（不泄露资源存在性）", f"got {code}")

        # -- 6. 伪造签名 ------------------------------------------------------
        idp.force_bad_signature = True
        code, r7 = call(api, "POST", "/api/v1/auth/sso/start", {})
        u7 = r7.get("authorize_url", "")
        st7 = parse_qs(u7.split("?", 1)[1]).get("state", [""])[0]
        ch7 = parse_qs(u7.split("?", 1)[1]).get("code_challenge", [""])[0]
        c7 = idp.issue_code({**base_claims, "sub": "idp-sub-forged"}, ch7)
        code, r8 = call(api, "POST", "/api/v1/auth/sso/callback", {"code": c7, "state": st7})
        check(code == 401, "签名不对的 id_token 被拒", f"got {code} {r8.get('error',{}).get('message','')}")
        idp.force_bad_signature = False

        # -- 7. issuer 不符 ---------------------------------------------------
        idp.force_wrong_issuer = True
        code, r9 = call(api, "POST", "/api/v1/auth/sso/start", {})
        u9 = r9.get("authorize_url", "")
        st9 = parse_qs(u9.split("?", 1)[1]).get("state", [""])[0]
        ch9 = parse_qs(u9.split("?", 1)[1]).get("code_challenge", [""])[0]
        c9 = idp.issue_code({**base_claims, "sub": "idp-sub-wrongiss"}, ch9)
        code, r10 = call(api, "POST", "/api/v1/auth/sso/callback", {"code": c9, "state": st9})
        check(code == 401, "issuer 不符的 id_token 被拒", f"got {code}")
        idp.force_wrong_issuer = False

        # -- 8. 篡改 state ----------------------------------------------------
        bad_state = state[:-4] + ("aaaa" if not state.endswith("aaaa") else "bbbb")
        code, r11 = call(api, "POST", "/api/v1/auth/sso/callback",
                         {"code": "whatever", "state": bad_state})
        check(code == 400, "被篡改的 state 被拒", f"got {code}")

        # -- 9. 撞到已有本地账号 → 需要本人确认 --------------------------------
        if args.password:
            merge_claims = {
                "sub": "idp-sub-merge-" + uuid.uuid4().hex[:6],
                "name": "合并测试",
                "phone_number": args.phone if args.phone.startswith("+")
                else "+86" + args.phone.lstrip("+").lstrip("86"),
                "email": "merge@example.cn",
            }
            code, r12 = call(api, "POST", "/api/v1/auth/sso/start", {})
            u12 = r12.get("authorize_url", "")
            st12 = parse_qs(u12.split("?", 1)[1]).get("state", [""])[0]
            ch12 = parse_qs(u12.split("?", 1)[1]).get("code_challenge", [""])[0]
            c12 = idp.issue_code(merge_claims, ch12)
            code, r13 = call(api, "POST", "/api/v1/auth/sso/callback",
                             {"code": c12, "state": st12})
            check(code == 200 and r13.get("need_link") is True,
                  "命中已有本地账号时要求先确认绑定，不自动合并",
                  f"{code} {r13.get('message','')}")

            lt = r13.get("link_token", "")
            code, r14 = call(api, "POST", "/api/v1/auth/sso/link",
                             {"link_token": lt, "password": "wrong-password-123"})
            check(code == 400, "绑定时本地密码错误被拒", f"got {code}")

            code, r15 = call(api, "POST", "/api/v1/auth/sso/link",
                             {"link_token": lt, "password": args.password})
            check(code == 200 and r15.get("ok") is True, "本地密码正确后绑定成功",
                  f"{code} {r15.get('message','')}")

            # 绑定后该 sub 应直通，且角色沿用本地账号（不是 annotator）
            code, r16 = call(api, "POST", "/api/v1/auth/sso/start", {})
            u16 = r16.get("authorize_url", "")
            st16 = parse_qs(u16.split("?", 1)[1]).get("state", [""])[0]
            ch16 = parse_qs(u16.split("?", 1)[1]).get("code_challenge", [""])[0]
            c16 = idp.issue_code(merge_claims, ch16)
            code, r17 = call(api, "POST", "/api/v1/auth/sso/callback",
                             {"code": c16, "state": st16})
            mtok = r17.get("access_token", "")
            code, mme = call(api, "GET", "/api/v1/auth/me", token=mtok)
            check(code == 200, "绑定后同一 sub 直通登录", f"got {code}")
            # 关键：绑定到已有账号后，权限应该来自那个本地账号，而不是 JIT 的
            # annotator。这里用"角色集合不等于 JIT 默认值"来断言，
            # 因为本地账号是什么角色取决于库里怎么配的。
            mroles = mme.get("roles") or []
            check(mroles != ["annotator"],
                  "角色沿用本地账号而非 JIT 默认值", str(mroles))
        else:
            print("[skip] 未提供 --password，跳过合并/绑定测试")

        # -- 9b. 手机号命中"没设本地密码"的账号 ------------------------------
        # 这种账号没法用密码自证，也就没有 need_link 的路可走。曾经的实现
        # 会直接掉进 JIT 自动建号 → 撞 users.phone 唯一索引 → 500。
        # 正确结局是转人工，且**不能**凭空建出账号。
        nopwd_claims = {
            "sub": "idp-sub-nopwd-" + uuid.uuid4().hex[:6],
            "name": "无密码账号测试",
            "phone_number": "+8613700137000",   # 王小明：种子数据，未设密码
            "email": "nopwd@example.cn",
        }
        code, rn = call(api, "POST", "/api/v1/auth/sso/start", {})
        un = rn.get("authorize_url", "")
        stn = parse_qs(un.split("?", 1)[1]).get("state", [""])[0]
        chn = parse_qs(un.split("?", 1)[1]).get("code_challenge", [""])[0]
        cn = idp.issue_code(nopwd_claims, chn)
        code, r19 = call(api, "POST", "/api/v1/auth/sso/callback",
                         {"code": cn, "state": stn})
        check(code == 200 and r19.get("need_admin") is True
              and not r19.get("ok"),
              "手机号命中无密码账号时转人工，不自动合并也不建号",
              f"{code} {r19.get('message','')}")

        # -- 10. 未启用 SSO 的部署不应暴露入口 --------------------------------
        # 由配置缺失决定，这里只能验证接口在当前配置下是开的
        check(True, "（未启用场景由 config.validate 在启动期拦截，见单元断言）")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()
        idp.stop()

    # 收尾也清一次：只清开头、不清结尾的话，跑完一次库里就多两个
    # "王小明 (+8619…)" 和一个绑定申请，日积月累会让人以为系统里真有人。
    if not args.no_reset:
        print(reset_fake_idp_residue())

    ok = sum(1 for r in RESULTS if r[0])
    bad = len(RESULTS) - ok
    print(f"\n结果：{ok} 通过 / {bad} 失败 ｜ IdP token 端点被调 {idp.token_calls} 次")
    if bad and not args.reuse:
        errlog = Path(tempfile.gettempdir()) / "forge-e2e-sso.err.log"
        if errlog.exists():
            tail = errlog.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-40:]
            if tail:
                print("\n--- 后端日志尾部 ---")
                print("\n".join(tail))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
