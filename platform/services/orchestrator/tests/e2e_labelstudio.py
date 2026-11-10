"""
FORGE × Label Studio 端到端验证
================================================================
证明的不是"接口能通"，而是完整链路真的成立：

  1. LS 里有真实项目（由 provision.py 建）
  2. 往项目导入真实任务
  3. 在 LS 里创建一条真实标注
  4. LS 通过 webhook 回调 orchestrator
  5. orchestrator 把对应任务的 status 从 assigned 改成 submitted，并写审计

全程无 mock。任何一步断了都会 FAIL，不会静默跳过。

前置：
  - Label Studio 运行中（python -m label_studio 或容器）
  - orchestrator 运行中，且已配置 FORGE_LS_BASE_URL / FORGE_LS_REFRESH_TOKEN
  - 已执行 labelstudio/provision.py 建好项目

用法：
  python tests/e2e_labelstudio.py --refresh <LS_REFRESH_TOKEN>
  python tests/e2e_labelstudio.py --refresh ... --project-title "FORGE · 文本 NER 与意图分类"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
import psycopg
from psycopg.rows import dict_row

PASS = 0
FAIL = 0
FAILURES: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}".strip())
        print(f"  FAIL  {name}  {detail}")


class LS:
    def __init__(self, base: str, refresh: str) -> None:
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=20, trust_env=False)
        r = self.c.post("/api/token/refresh/", json={"refresh": refresh})
        if r.status_code != 200:
            raise SystemExit(f"refresh 失败 {r.status_code}: {r.text[:200]}")
        self.c.headers["Authorization"] = f"Bearer {r.json()['access']}"

    def project_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        r = self.c.get("/api/projects/", params={"page_size": 100})
        r.raise_for_status()
        for p in r.json().get("results", []):
            if p["title"] == title:
                return p
        return None

    def import_tasks(self, pid: int, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        r = self.c.post(f"/api/projects/{pid}/import", json=tasks)
        if r.status_code not in (200, 201):
            raise SystemExit(f"导入失败 {r.status_code}: {r.text[:300]}")
        return r.json()

    def list_task_ids(self, pid: int) -> List[int]:
        r = self.c.get("/api/tasks/", params={"project": pid, "page_size": 100})
        r.raise_for_status()
        return [t["id"] for t in r.json().get("tasks", r.json().get("results", []))]

    def create_annotation(self, ls_task_id: int, result: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 注意：LS 没有裸 /api/annotations/ 的 POST（那个路径只挂子路由，会 404），
        # 创建标注必须走 /api/tasks/{id}/annotations/
        r = self.c.post(f"/api/tasks/{ls_task_id}/annotations/",
                        json={"task": ls_task_id, "result": result})
        if r.status_code not in (200, 201):
            raise SystemExit(f"创建标注失败 {r.status_code}: {r.text[:300]}")
        return r.json()


def db_task_status(task_id: str) -> Dict[str, Any]:
    dsn = os.environ.get(
        "FORGE_DB_DSN",
        "postgresql://forge_app:forge_dev_2026@localhost:5432/forge")
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("select set_config('forge.enterprise_id', %s, true)",
                     (os.environ.get("FORGE_DEFAULT_TENANT", "ent_forge"),))
        conn.execute("select set_config('forge.supplier_id', '', true)")
        conn.execute("select set_config('forge.assignee_id', '', true)")
        return conn.execute(
            "select id, status, ls_project_id from forge.tasks where id = %s",
            (task_id,)).fetchone() or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ls-url", default="http://127.0.0.1:8080")
    ap.add_argument("--refresh", required=True)
    ap.add_argument("--api", default="http://127.0.0.1:8300")
    ap.add_argument("--token", default="project_manager", help="orchestrator 演示 token")
    ap.add_argument("--project-title", default="FORGE · 文本 NER 与意图分类")
    args = ap.parse_args()

    print(f"\nFORGE × Label Studio 端到端验证  (LS={args.ls_url})\n")

    # 1. LS 项目存在
    print("[1] Label Studio 项目")
    ls = LS(args.ls_url, args.refresh)
    proj = ls.project_by_title(args.project_title)
    check(f"项目「{args.project_title}」存在", proj is not None)
    if not proj:
        return 1
    pid = proj["id"]
    check("项目有有效的 label config", bool(proj.get("label_config")))
    print(f"       ls_project_id = {pid}")

    # 2. 导入真实任务
    print("[2] 导入任务")
    before = ls.list_task_ids(pid)
    stamp = int(time.time())
    imported = ls.import_tasks(pid, [
        {"data": {"text": f"[{stamp}] 我上周买的耳机右声道没声音，要求退款。"}},
        {"data": {"text": f"[{stamp}] 订单 88231 什么时候能发货？"}},
    ])
    check("导入返回 task_count ≥ 2", imported.get("task_count", 0) >= 2, str(imported)[:120])
    after = ls.list_task_ids(pid)
    check("LS 任务数增加", len(after) > len(before), f"{len(before)} -> {len(after)}")
    new_ids = [i for i in after if i not in before]

    # 3. 选一条 DB 任务挂到这个 LS 项目上（模拟"派单已同步到 LS"）
    print("[3] 平台侧任务绑定")
    dsn_task = os.environ.get("FORGE_E2E_TASK", "tsk_a3")
    dsn = os.environ.get(
        "FORGE_DB_DSN",
        "postgresql://forge_app:forge_dev_2026@localhost:5432/forge")
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("select set_config('forge.enterprise_id', %s, true)", ("ent_forge",))
        conn.execute("select set_config('forge.supplier_id', '', true)")
        conn.execute("select set_config('forge.assignee_id', '', true)")
        row = conn.execute(
            "select id, status from forge.tasks where id = %s", (dsn_task,)).fetchone()
        if row is None:
            print(f"  跳过：库里没有任务 {dsn_task}")
            return 1
        # 回到 assigned 以便观察状态跃迁
        conn.execute(
            "update forge.tasks set status='assigned', ls_project_id=%s, updated_at=now()"
            " where id=%s", (pid, dsn_task))
    st = db_task_status(dsn_task)
    check("任务已绑定 ls_project_id", st.get("ls_project_id") == pid, str(st))
    check("任务初始状态为 assigned", st.get("status") == "assigned", str(st))

    # 4. 在 LS 创建真实标注
    print("[4] 在 Label Studio 创建标注")
    if not new_ids:
        print("  跳过：没有新任务可用于标注")
        return 1
    ann = ls.create_annotation(new_ids[0], [
        {"from_name": "intent", "to_name": "text", "type": "choices",
         "value": {"choices": ["refund_request"]}},
        {"from_name": "sentiment", "to_name": "text", "type": "choices",
         "value": {"choices": ["negative"]}},
    ])
    check("标注创建成功", bool(ann.get("id")), str(ann)[:120])

    # 5. 等 webhook 回流
    print("[5] 等待 webhook 回流（最多 15s）")
    final: Dict[str, Any] = {}
    for _ in range(15):
        time.sleep(1)
        final = db_task_status(dsn_task)
        if final.get("status") == "submitted":
            break
    check("任务状态被 LS 回调自动推进到 submitted",
          final.get("status") == "submitted",
          f"当前={final.get('status')}（若为 assigned，说明 webhook 未注册或回调失败）")

    # 6. 审计留痕
    print("[6] 审计")
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("select set_config('forge.enterprise_id', 'ent_forge', true)")
        rows = conn.execute(
            "select action, detail from forge.audit_log where target = %s"
            " and action = 'ls.annotation' order by at desc limit 3", (dsn_task,)).fetchall()
    check("存在 ls.annotation 审计记录", len(rows) > 0, str(rows)[:160])

    total = PASS + FAIL
    print(f"\n结果：{PASS}/{total} 通过，{FAIL} 失败")
    for f in FAILURES:
        print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
