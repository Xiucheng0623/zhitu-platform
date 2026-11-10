"""
FORGE · 手机号注册/登录 与 作业链路 稳定性体检
================================================================
这个脚本回答的问题不是"功能有没有"，而是"连续用会不会崩"：

  1. 一个新人从拿到邀请码到能干活，要走几步、每一步会不会卡住
  2. 密码输错 5 次到底锁不锁、锁了管理员能不能救、救完能不能立刻用
  3. 改密之后旧 token 是不是真的立刻失效（而不是还能用 8 小时）
  4. 10 个人同时登录/同时刷工作台，服务端会不会 5xx、连接池会不会堆死
  5. 作业链路的关键读接口在并发下的 p95 延迟是多少

断言口径：**出现任何一个 5xx 就算不稳定**。4xx 是业务语义，不算故障；
但像"并发时偶发 500""连接池耗尽"这类问题，只有并发跑才暴露得出来。

副作用与自清理
--------------
会创建/修改一个固定测试账号（默认 13800000099），跑完停用；
会消耗一个邀请码；会短暂锁定该账号再解锁。**不碰 admin 和种子账号**。

用法
----
  python tests/diagnose_stability.py
  python tests/diagnose_stability.py --admin-password admin1111 --concurrency 16
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE = os.environ.get("FORGE_BASE", "http://127.0.0.1:8300")
ADMIN_PHONE = os.environ.get("FORGE_ADMIN_PHONE", "13800138000")
TEST_PHONE = "13800000099"          # 专用体检账号，勿用于真实业务
TEST_PWD = "Stab1lity-Check-2026"
TEST_PWD2 = "Stab1lity-Check-2027"

PASS = 0
FAIL = 0
WARN = 0
FAILURES: List[str] = []
TIMINGS: List[Tuple[str, float]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}".strip())
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
    return cond


def warn(name: str, detail: str = "") -> None:
    global WARN
    WARN += 1
    print(f"  WARN  {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------

class Client:
    """薄封装：每次请求记录耗时，5xx 直接计入错误数（不静默重试）。"""

    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base, timeout=20.0)
        self.server_errors: List[str] = []

    def call(self, method: str, path: str, token: Optional[str] = None,
             body: Optional[Dict[str, Any]] = None,
             label: str = "") -> Tuple[int, Any]:
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        t0 = time.perf_counter()
        try:
            r = self.http.request(method, path, json=body, headers=h)
        except Exception as exc:  # noqa: BLE001
            self.server_errors.append(f"{method} {path} → {type(exc).__name__}: {exc}")
            return 0, {"error": str(exc)}
        dt = (time.perf_counter() - t0) * 1000
        TIMINGS.append((label or f"{method} {path}", dt))
        if r.status_code >= 500:
            self.server_errors.append(f"{method} {path} → {r.status_code} {r.text[:200]}")
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, {"raw": r.text[:300]}


def p95(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, int(round(0.95 * (len(s) - 1))))
    return s[idx]


# ---------------------------------------------------------------------------

def ensure_test_account(c: Client, admin_tok: str) -> Tuple[Optional[str], str]:
    """把测试账号拉到一个已知状态：存在、已激活、密码为 TEST_PWD。

    先尝试注册（幂等：409 说明已存在），已存在就改用管理员重置密码，
    这样重复跑不会在库里堆一堆"体检账号"。
    """
    code, inv = c.call("POST", "/api/v1/admin/invites", admin_tok,
                       {"roles": ["annotator"], "max_uses": 1,
                        "note": "稳定性体检", "expires_in_days": 1})
    if code != 200:
        return None, f"发邀请码失败 {code} {inv}"

    code, r = c.call("POST", "/api/v1/auth/register", None,
                     {"phone": TEST_PHONE, "password": TEST_PWD,
                      "name": "体检账号", "invite_code": inv["code"]},
                     label="POST register(scrypt)")
    if code == 200:
        uid = r["user_id"]
        code2, r2 = c.call("PATCH", f"/api/v1/admin/users/{uid}/status",
                           admin_tok, {"status": "active"})
        if code2 != 200:
            return None, f"新账号激活失败 {code2} {r2}"
        return uid, "新注册并激活（邀请码已消耗一个）"
    if code != 409:
        return None, f"注册失败 {code} {r}"

    # 已存在：从用户列表里按尾号认回来（列表里的手机号是打码的，
    # 形如 138****0099，所以只能比后缀）
    code, r = c.call("GET", "/api/v1/admin/users?status=", admin_tok)
    if code != 200:
        return None, f"列用户失败 {code}"
    for u in (r.get("items") or []):
        if u["phone"].endswith(TEST_PHONE[-4:]):
            uid = u["id"]
            c.call("PATCH", f"/api/v1/admin/users/{uid}/status", admin_tok,
                   {"status": "active"})
            code2, r2 = c.call("POST", f"/api/v1/admin/users/{uid}/reset-password",
                               admin_tok, {"new_password": TEST_PWD},
                               label="POST reset-password(scrypt)")
            if code2 != 200:
                return None, f"重置密码失败 {code2} {r2}"
            return uid, "复用已有账号并重置密码"
    return None, "手机号已注册但用户列表里找不到（可能被别的租户占用）"


def login(c: Client, phone: str, pwd: str, label: str = "") -> Tuple[int, Any]:
    return c.call("POST", "/api/v1/auth/login", None,
                  {"phone": phone, "password": pwd}, label=label or "POST login")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--admin-phone", default=ADMIN_PHONE)
    ap.add_argument("--admin-password", default=os.environ.get("FORGE_ADMIN_PWD", ""))
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--p95-budget-ms", type=float, default=800.0,
                    help="非登录接口的 p95 预算")
    ap.add_argument("--login-p95-ms", type=float, default=3000.0,
                    help="登录接口的 p95 预算（含一次 scrypt，天生更慢）")
    args = ap.parse_args()

    if not args.admin_password:
        raise SystemExit("需要 --admin-password（或用 FORGE_ADMIN_PWD 传）")

    c = Client(args.base)
    print(f"\nFORGE 稳定性体检 → {args.base}\n")

    # ---------------------------------------------------------------- [0]
    print("[0] 基础连通")
    code, r = c.call("GET", "/healthz?deep=1")
    db = (r or {}).get("db") or {}
    check("healthz 深度检查 200", code == 200, f"got {code}")
    check("数据库连通且 RLS 生效",
          db.get("ok") is True and db.get("can_bypass_rls") is False,
          f"pg={db.get('pg')} forced={db.get('rls_forced')}")

    # ---------------------------------------------------------------- [1]
    print("\n[1] 管理员登录")
    code, r = login(c, args.admin_phone, args.admin_password, "POST login(admin)")
    admin_tok = (r or {}).get("access_token") or ""
    check("管理员能登录", code == 200 and bool(admin_tok), f"got {code} {(r or {}).get('error')}")
    if not admin_tok:
        return report(c, None, args)

    # ---------------------------------------------------------------- [2]
    print("\n[2] 邀请码 → 注册 → 激活 → 首次登录")
    uid, note = ensure_test_account(c, admin_tok)
    print(f"        测试账号准备：{note}")
    check("测试账号就绪", bool(uid), note)
    if not uid:
        return report(c, None, args)

    code, r = login(c, TEST_PHONE, TEST_PWD, "POST login(new)")
    check("新账号能登录", code == 200, f"got {code} {(r or {}).get('error')}")
    must_change = (r or {}).get("must_change_password")
    tok = (r or {}).get("access_token") or ""
    check("注册账号被要求先改密码", must_change is True, f"must_change={must_change}")

    # 未改密时只应放行 /me 与改密，其它接口要被拦住
    code_biz, _ = c.call("GET", "/api/v1/projects", tok)
    check("未改密时业务接口被拦（403）", code_biz == 403, f"got {code_biz}")

    code, r = c.call("POST", "/api/v1/auth/change-password", tok,
                     {"old_password": TEST_PWD, "new_password": TEST_PWD2},
                     label="POST change-password")
    check("建档后第一次改密成功", code == 200, f"got {code} {(r or {}).get('error')}")
    new_tok = (r or {}).get("access_token") or ""
    check("改密返回了可用的新 token", bool(new_tok))

    code_old, _ = c.call("GET", "/api/v1/auth/me", tok)
    check("改密后旧 token 立刻失效（401）", code_old == 401, f"got {code_old}")
    code_new, me = c.call("GET", "/api/v1/auth/me", new_tok)
    check("改密后新 token 可用", code_new == 200, f"got {code_new}")
    check("改密后不再要求改密",
          (me or {}).get("must_change_password") is False,
          str((me or {}).get("must_change_password")))

    # ---------------------------------------------------------------- [3]
    print("\n[3] 密码错误 → 计数 → 锁定 → 管理员救回")
    ok_hint, last_code = True, 0
    for i in range(1, 6):
        code, r = c.call("POST", "/api/v1/auth/login", None,
                         {"phone": TEST_PHONE, "password": "definitely-wrong"})
        msg = ((r or {}).get("error") or {}).get("message", "")
        last_code = code
        if i < 5 and code != 401:
            ok_hint = False
        if i == 4:
            check(f"第 {i} 次错误仍提示剩余次数", "还可尝试" in msg, msg)
    check("前 4 次错误密码返回 401", ok_hint, f"last={last_code}")

    code, r = c.call("POST", "/api/v1/auth/login", None,
                     {"phone": TEST_PHONE, "password": TEST_PWD2})
    check("第 5 次后账号被锁定（423）", code == 423,
          f"got {code} {((r or {}).get('error') or {}).get('message','')}")

    code, r = c.call("POST", "/api/v1/auth/login", None,
                     {"phone": TEST_PHONE, "password": "definitely-wrong"})
    check("锁定期间即便密码错误也返回 423（不泄露密码对错）", code == 423, f"got {code}")

    code, r = c.call("POST", f"/api/v1/admin/users/{uid}/reset-password",
                     admin_tok, {"new_password": TEST_PWD},
                     label="POST reset-password(scrypt)")
    check("管理员重置密码成功（同时应解锁）", code == 200, f"got {code}")
    code, r = login(c, TEST_PHONE, TEST_PWD, "POST login(after-reset)")
    check("重置后能立即登录（锁定已解除）", code == 200, f"got {code}")
    tok3 = (r or {}).get("access_token") or ""
    if (r or {}).get("must_change_password"):
        code, r = c.call("POST", "/api/v1/auth/change-password", tok3,
                         {"old_password": TEST_PWD, "new_password": TEST_PWD2},
                         label="POST change-password")
        tok3 = (r or {}).get("access_token") or tok3
    check("重置后拿到可用 token", bool(tok3))

    # ---------------------------------------------------------------- [4]
    print(f"\n[4] 并发（{args.concurrency} 路）")
    login_tok = tok3

    # 4.1 串行基线：先测"一次登录本机要多久"，作为并发的对照
    serial_n = 4
    t0 = time.perf_counter()
    for _ in range(serial_n):
        login(c, TEST_PHONE, TEST_PWD2, "POST login(serial)")
    serial_avg = (time.perf_counter() - t0) * 1000 / serial_n
    print(f"        串行登录均值 {serial_avg:.0f} ms/次（含 scrypt）")

    # 4.2 并发：8 个同时打
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(login, c, TEST_PHONE, TEST_PWD2, "POST login(conc)")
                for _ in range(args.concurrency)]
        results = [f.result() for f in futs]
    batch_ms = (time.perf_counter() - t0) * 1000
    conc_avg = batch_ms / args.concurrency
    codes = [r[0] for r in results]
    check(f"{args.concurrency} 个并发登录全部 200",
          all(x == 200 for x in codes), f"codes={sorted(set(codes))}")
    # 这条是整套体检里最重要的回归断言：scrypt 一旦被写回同步调用，
    # 并发就会退化成排队，这个比值会立刻掉到 1.0 附近。
    check("并发登录是真的并行，不是排队",
          conc_avg < serial_avg * 0.7,
          f"串行 {serial_avg:.0f} ms/次 → 并发 {conc_avg:.0f} ms/次 "
          f"(整批 {batch_ms:.0f} ms)")

    # 4.3 登录风暴期间，无关请求不能被拖死（事件循环有没有被占住）
    storm: List[float] = []

    def probe_healthz() -> None:
        for _ in range(8):
            tt = time.perf_counter()
            try:
                c.call("GET", "/healthz", None, None, "GET healthz(storm)")
            except Exception:  # noqa: BLE001
                pass
            storm.append((time.perf_counter() - tt) * 1000)
            time.sleep(0.08)

    with ThreadPoolExecutor(max_workers=args.concurrency + 1) as pool:
        probe = pool.submit(probe_healthz)
        list(pool.map(
            lambda _: login(c, TEST_PHONE, TEST_PWD2, "POST login(storm)"),
            range(args.concurrency)))
        probe.result()
    check("登录风暴期间其它接口未被阻塞（最坏 < 1s）",
          max(storm) < 1000, f"最坏 {max(storm):.0f} ms / 中位 {statistics.median(storm):.0f} ms")

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(c.call, "GET", "/api/v1/auth/me", login_tok,
                            None, "GET me(conc)")
                for _ in range(args.concurrency)]
        results = [f.result() for f in futs]
    codes = [r[0] for r in results]
    check(f"{args.concurrency} 个并发 /me 全部 200",
          all(x == 200 for x in codes), f"codes={sorted(set(codes))}")

    # ---------------------------------------------------------------- [5]
    print("\n[5] 作业链路关键接口（并发读）")
    biz_paths = [
        ("/api/v1/projects", "GET projects"),
        ("/api/v1/tasks", "GET tasks"),
        ("/api/v1/settlements", "GET settlements"),
        ("/api/v1/reviews", "GET reviews"),
        ("/api/v1/performance/live", "GET performance"),
    ]
    # 测试账号是 annotator，大部分管理视图会被 404（越权一律 404）。
    # 所以作业链路用管理员身份压，才测得到真实查询路径。
    for path, label in biz_paths:
        with ThreadPoolExecutor(max_workers=min(8, args.concurrency)) as pool:
            futs = [pool.submit(c.call, "GET", path, admin_tok, None, label)
                    for _ in range(8)]
            results = [f.result() for f in futs]
        codes = [r[0] for r in results]
        latency = [t for (lbl, t) in TIMINGS if lbl == label]
        check(f"{path} 8 次并发无 5xx",
              all(x < 500 for x in codes), f"codes={sorted(set(codes))}")
        if latency:
            print(f"        {label}: 均值 {statistics.mean(latency):.0f} ms · "
                  f"p95 {p95(latency):.0f} ms")

    # ---------------------------------------------------------------- [6]
    print("\n[6] 凭证边界")
    code, _ = c.call("GET", "/api/v1/auth/me")
    check("无 token 访问 /me 返回 401", code == 401, f"got {code}")
    code, _ = c.call("GET", "/api/v1/auth/me", "not-a-jwt")
    check("非法 token 返回 401", code == 401, f"got {code}")
    tampered = login_tok[:-6] + ("aaaaaa" if not login_tok.endswith("aaaaaa") else "bbbbbb")
    code, _ = c.call("GET", "/api/v1/auth/me", tampered)
    check("被篡改的 token 返回 401", code == 401, f"got {code}")

    # ---------------------------------------------------------------- [7]
    print("\n[7] 连接池与延迟总览")
    code, r = c.call("GET", "/healthz?deep=1")
    pool = ((r or {}).get("db") or {}).get("pool") or {}
    check("连接池没有堆积（waiting=0）", pool.get("waiting", 0) == 0, str(pool))

    # 预算要按"接口里有没有 scrypt"分两档，否则测不出真问题：
    # 登录/改密各有 1~2 次 scrypt（本机 0.9s 起步，服务器上约 0.1s），
    # 把它们和普通查询混在一起算，p95 永远被拖爆，
    # 而真正该盯的"作业接口变慢"反而看不见。
    def _has_scrypt(lbl: str) -> bool:
        # 任何走 scrypt 的接口都要归到这一档：login / change-password /
        # register / reset-password。用路径子串匹配，避免"忘了打标签"的
        # 调用被错算进普通接口，把普通接口的 p95 拖爆。
        keys = ("/auth/login", "/auth/change-password", "/auth/register",
                "/reset-password", "POST login", "POST change-password")
        return "scrypt" in lbl or any(k in lbl for k in keys)

    pwd_t = [t for (lbl, t) in TIMINGS if _has_scrypt(lbl)]
    other_t = [t for (lbl, t) in TIMINGS if not _has_scrypt(lbl)]
    print(f"        含 scrypt 的接口 {len(pwd_t)} 次：中位 {statistics.median(pwd_t):.0f} ms · "
          f"p95 {p95(pwd_t):.0f} ms")
    print(f"        普通过滤接口 {len(other_t)} 次：中位 {statistics.median(other_t):.0f} ms · "
          f"p95 {p95(other_t):.0f} ms")

    check(f"普通接口 p95 在 {args.p95_budget_ms:.0f} ms 预算内",
          p95(other_t) <= args.p95_budget_ms, f"p95={p95(other_t):.0f} ms")
    check(f"含 scrypt 接口 p95 在 {args.login_p95_ms:.0f} ms 预算内",
          p95(pwd_t) <= args.login_p95_ms, f"p95={p95(pwd_t):.0f} ms")

    if other_t and p95(other_t) > args.p95_budget_ms:
        print("        最慢的普通请求（定位用）：")
        for lbl, t in sorted(
                [(l, t) for (l, t) in TIMINGS if not _has_scrypt(l)],
                key=lambda x: -x[1])[:6]:
            print(f"          {lbl}: {t:.0f} ms")

    check("全程零 5xx", not c.server_errors,
          "；".join(c.server_errors[:3]) if c.server_errors else "")

    # 恢复密码到 TEST_PWD2 并保持激活，方便人工复验
    return report(c, (uid, admin_tok), args)


def report(c: Client, cleanup: Any, args: argparse.Namespace) -> int:
    if cleanup:
        uid, admin_tok = cleanup
        # 停用而不是删除：账号留在库里可追溯，也不会占用手机号
        c.call("PATCH", f"/api/v1/admin/users/{uid}/status", admin_tok,
               {"status": "suspended"})
        print("\n        已停用体检账号（不删除，保留审计痕迹）")

    print()
    print(f"结果：{PASS} 通过 / {FAIL} 失败 / {WARN} 警告")
    if FAILURES:
        print("\n未通过项：")
        for f in FAILURES:
            print(f"  - {f}")
    c.http.close()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
