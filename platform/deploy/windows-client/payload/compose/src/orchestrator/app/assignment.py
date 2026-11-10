"""
标注员数据一致性（平台分配 ↔ 标注引擎实际可标 ↔ 标注员手里的数据）
============================================================================
为什么要单独做这一层（2026-11-07 用户明确要求「无论什么时候都一样」）
----------------------------------------------------------------------------
实测过两个真实账号，差异不是"同步慢了"，而是**平台在拿合同数冒充任务数**：

  张三：平台 3 个批次 / 910 条  →  LS 2 个项目 / 4 条
  李四：平台 2 个批次 / 810 条  →  LS 0 个项目 / 0 条

根因有两层：
  1. 集合：若干批次压根没绑定 LS 项目，平台仍算作「分给标注员的任务」；
  2. 条数：tasks.item_count 是合同/交付口径（客户买了 500 条），
     而 LS 里是该批次真正导入的任务数（可能只有 2 条）。

用户后来把要求说得更准：**「标注员手里面的数据、标注员平台看到的数据、
LS 看到的数据应该是一样的」**。所以只对齐「项目集合 + 条数」是不够的 ——
条数对了也可能平台展示的是另一份数据。这里再往上做一层：

  **LS 的任务行是唯一正本，平台不保存、不复制、不缓存任何一行任务数据。**
  `GET /me/assignment/items` 每次请求实时读 LS `/api/tasks?project=N`，
  把每一行原样带回来（id / data / meta / 标注状态 / 草稿 / 时间戳），
  并给出 data_sha256 与整份清单的 fingerprint。平台页面显示的「可标任务 N 条」
  直接由这 N 行派生，行的增删、标注状态变化会同步改变数字与指纹。
  「标注」按钮逐条换票进 LS 时，服务端先核实该 task 确实属于这个账号
  可见的项目，落地地址精确到 `?task=<id>`，即点平台第 5 行就打开 LS 第 5 行。

  这样三处是同一批行：标注员在 LS 里标的，就是平台列出来的，就是他手里的。
  合同量仍原样保留（contract_items）给结算/预算/商务，绝不被同步改写。

架构前提（写清楚，免得以后被误改）：
  批次 ↔ LS 项目 1:1，批次单 assignee，LS 侧按项目白名单隔离，
  所以项目白名单等价于「这个人能碰到的全部任务行」。一旦两个批次共用
  同一个 LS 项目，等价关系就破了 —— 这里显式检测重复绑定并报警。

「无论什么时候都一样」的工程边界（别吹成跨库事务强一致）：
  平台与 LS 是两个库、中间隔着网络，不可能有字面意义的零延迟一致。
  能兑现的是：**读时同源同次、不用旧数假装一致、漂移立刻可见**。
  平台不存第二份任务数据，就没有"两份数据不同步"这回事；两次刷新之间
  LS 若被改动，页面上的指纹会变、轮询/刷新即追上。做不到的是"LS 被改的
  那一瞬间，一个已经打开的浏览器页面自动变" —— 那需要推送，不是一致性。

状态与漂移：
  * ok               已接入且 LS 实时可读，actual_items 就是 LS 当下的任务数
  * not_connected    平台有这个批次，但没绑定 LS 项目 —— 不算「可标任务」
  * ls_missing       绑定了项目号，但 LS 里查不到该项目
  * ls_unreachable   LS 不可达 —— 绝不返回旧数冒充一致，明确标注不可信
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from . import db
from .clients import IntegrationError, ls
from .security import Principal, current_principal

log = logging.getLogger("forge.assignment")
router = APIRouter(prefix="/api/v1/me", tags=["assignment"])

# 给 healthz / 启动自检看的全局快照。定时对账每轮覆盖一次，
# 内容是「管理视角」的全量一致性，不针对某个登录人。
STATE: Dict[str, Any] = {
    "enabled": True,
    "last_check_at": None,
    "last_ok_at": None,
    "in_sync": None,
    "batches": 0,
    "assigned": 0,
    "connected": 0,
    "not_connected": 0,
    "actual_items": 0,
    "issues": [],
    "error": "",
}

# 任务**数据行**级的全局自检快照（healthz.assignment_items 读它）。
# 和上面的集合/数量自检分开存：一个是「有没有分错项目」，一个是
# 「平台列的每一行是不是 LS 当下的那一行」，坏法不同、处置也不同。
ITEMS_STATE: Dict[str, Any] = {
    "source": "labelstudio-live",
    "last_check_at": None,
    "last_ok_at": None,
    "in_sync": None,
    "items": 0,
    "labeled": 0,
    "drafts": 0,
    "ls_projects": 0,
    "unreachable": 0,
    "truncated": 0,
    "fingerprint": "",
    "issues": [],
    "error": "",
}

# 单个项目最多带回多少行。这是防御，不是业务限制：超过就把「无法完整核对」
# 明确写进 issues，绝不静默截断后还宣称两边一致。
MAX_ITEMS_PER_PROJECT = 2000


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


async def _summaries(pids: List[int]) -> Dict[int, Dict[str, Any]]:
    """并发取每个 LS 项目的真实任务数。单个失败不影响其他项目。

    tasks 取「任务列表实际条数」而不是 project.task_number（缓存字段，
    导入失败/删过任务会失真）；completed 是「已标注条目数」，
    不是标注记录数 —— 同一题被两人标过不能算两条进度。
    """
    async def one(pid: int):
        try:
            return pid, await ls.project_summary(pid), ""
        except IntegrationError as exc:
            return pid, None, str(exc)[:160]
        except Exception as exc:  # noqa: BLE001 - 单个项目失败不拖垮整份清单
            return pid, None, f"{type(exc).__name__}: {str(exc)[:120]}"

    pairs = await asyncio.gather(*(one(pid) for pid in sorted(set(pids))))
    return {pid: {"summary": s, "error": err} for pid, s, err in pairs}


async def build_assignment(
        conn: Any,
        remote: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
    """在**调用方的 RLS 上下文**里构造任务清单。纯读，不写任何表。

    传进来的 conn 已经由 db.tenant_conn(p) 建好，所以标注员只会拿到
    分配给自己的批次，管理员拿到全部 —— 同一份函数不用写两套。

    `remote` 供定时对账复用它本轮已经取过的 LS 数据，避免同一轮里
    把每个项目再问一遍；不传就由本函数实时并发取（接口默认行为）。
    格式：{ls_project_id: {"summary": {...} | None, "error": "..."}}。
    """
    cur = await conn.execute(
        "select t.id, t.batch_no, t.name, t.project_id, t.status, "
        "       t.item_count, t.done_count, t.ls_project_id, t.assignee_id, "
        "       sm.user_id as assignee_user_id, sm.name as assignee_name "
        "from forge.tasks t "
        "left join forge.supplier_members sm on sm.id = t.assignee_id "
        "order by t.id")
    tasks = [dict(r) for r in await cur.fetchall()]

    # 票据白名单用的就是这条 SQL（ls_bridge.py /ls/ticket），在同样的 RLS 下。
    # 显式取出来对比：若白名单里有这里查不到的项目，说明票据会把人送进
    # 一个平台里不存在的批次，或者某个项目被别人共享了。
    cur = await conn.execute(
        "select distinct ls_project_id from forge.tasks "
        "where ls_project_id is not null")
    ticket_projects = sorted({_int(r["ls_project_id"]) for r in await cur.fetchall()
                              if r["ls_project_id"]})

    pids = [_int(t["ls_project_id"]) for t in tasks if t["ls_project_id"]]
    if remote is None:
        remote = await _summaries(pids) if pids else {}

    # 重复绑定：两个批次指向同一个 LS 项目时，项目级隔离不再等价于
    # 任务级隔离，两个标注员会看到对方的数据 —— 必须报出来。
    seen: Dict[int, str] = {}
    dup: Dict[int, List[str]] = {}
    for t in tasks:
        pid = _int(t["ls_project_id"])
        if not pid:
            continue
        if pid in seen:
            dup.setdefault(pid, [seen[pid]]).append(t["id"])
        else:
            seen[pid] = t["id"]

    rows: List[Dict[str, Any]] = []
    for t in tasks:
        pid = _int(t["ls_project_id"])
        contract = _int(t["item_count"])
        info = remote.get(pid) or {}
        s = info.get("summary")
        if not pid:
            state, actual, actual_done = "not_connected", 0, 0
            ls_title, err = "", "该批次尚未绑定标注引擎项目"
        elif s is None:
            err = info.get("error") or "本轮未取得该项目的实时数据"
            # 404 / 不存在 与 网络不可达 分开报，处置方式不同
            state = "ls_missing" if "404" in err else "ls_unreachable"
            actual, actual_done, ls_title = 0, 0, ""
        else:
            state, err = "ok", ""
            actual = _int(s.get("tasks"))
            actual_done = _int(s.get("completed"))
            ls_title = s.get("title") or ""
        rows.append({
            "task_id": t["id"],
            "batch_no": t.get("batch_no"),
            "name": t.get("name"),
            "project_id": t.get("project_id"),
            "status": t.get("status"),
            "assignee_user_id": t.get("assignee_user_id"),
            "assignee_name": t.get("assignee_name"),
            # 合同口径：只给商务/结算看，永远不被同步改写
            "contract_items": contract,
            "contract_done": _int(t["done_count"]),
            # 实际口径：标注员能标的真实任务，来自 LS 实时
            "actual_items": actual,
            "actual_done": actual_done,
            "ls_project_id": pid or None,
            "ls_title": ls_title,
            "state": state,
            "countable": state == "ok",
            "error": err,
        })

    assigned = [r for r in rows if r["assignee_user_id"] or r["assignee_name"]]
    connected = [r for r in rows if r["state"] == "ok"]
    reachable = sorted({r["ls_project_id"] for r in connected})
    issues: List[str] = []
    for r in rows:
        if r["state"] == "not_connected" and (r["assignee_user_id"] or r["assignee_name"]):
            issues.append(f"{r['batch_no'] or r['task_id']} 已分配给标注员，但未接入标注引擎")
        elif r["state"] == "ls_missing":
            issues.append(f"{r['batch_no'] or r['task_id']} 绑定的 LS 项目 #{r['ls_project_id']} 不存在")
        elif r["state"] == "ls_unreachable":
            issues.append(f"{r['batch_no'] or r['task_id']} 标注引擎不可达，无法确认任务数")
    for pid, ids in dup.items():
        issues.append(f"LS 项目 #{pid} 被多个批次共用：{'、'.join(ids)}（标注员会互相看到数据）")
    ticket_only = [p for p in ticket_projects if p not in reachable]
    assignment_only = [p for p in reachable if p not in ticket_projects]
    if ticket_only:
        issues.append(f"票据白名单里有读不到的项目：{ticket_only}")
    if assignment_only:
        issues.append(f"任务清单与票据白名单不一致：{assignment_only}")

    checked_at = int(time.time())
    summary = {
        # in_sync 说的是「集合与可核对性」，不是「合同数 == LS 数」——
        # 那两个数语义不同，本来就不该相等。
        "in_sync": not issues,
        "checked_at": checked_at,
        "source": "labelstudio-live",
        "batches": len(rows),
        "assigned": len(assigned),
        "connected": len(connected),
        "not_connected": sum(1 for r in rows if r["state"] == "not_connected"),
        "unreachable": sum(1 for r in rows
                           if r["state"] in ("ls_unreachable", "ls_missing")),
        "contract_items": sum(r["contract_items"] for r in rows),
        "actual_items": sum(r["actual_items"] for r in rows),
        "actual_done": sum(r["actual_done"] for r in rows),
        "ls_projects": len(reachable),
        "ticket_projects": ticket_projects,
        "issues": issues,
    }
    return {"ok": True, "rows": rows, "summary": summary}


def record(snapshot: Dict[str, Any], error: str = "") -> None:
    """把一轮全量自检结果写进全局状态（healthz 会读）。"""
    s = snapshot.get("summary") or {}
    STATE.update(
        last_check_at=s.get("checked_at"),
        last_ok_at=s.get("checked_at") if s.get("in_sync") else STATE.get("last_ok_at"),
        in_sync=s.get("in_sync"),
        batches=s.get("batches", 0),
        assigned=s.get("assigned", 0),
        connected=s.get("connected", 0),
        not_connected=s.get("not_connected", 0),
        actual_items=s.get("actual_items", 0),
        issues=(s.get("issues") or [])[:12],
        error=error[:200],
    )


def snapshot() -> Dict[str, Any]:
    return dict(STATE)


@router.get("/assignment")
async def my_assignment(p: Principal = Depends(current_principal)):
    """当前账号的任务清单：平台分配关系 + LS 实时可标任务数。

    标注员进「我的任务」页读这个；管理员读到的就是全量自检结果。
    所有数字都来自本次请求对 LS 的实时读取，**没有缓存、没有兜底旧值**：
    LS 不可达时明确回 ls_unreachable 并让前端显示「无法核对」，
    绝不拿上一次的数字假装一致。

    注意：这一层只回答「分到哪些项目、每个项目多少条」。要拿**标注员手里
    一条条的真实数据**，读 /me/assignment/items —— 那才是页面主视图的正本。
    """
    async with db.tenant_conn(p) as conn:
        return await build_assignment(conn)


# ---------------------------------------------------------------------------
# 任务数据行：LS 的任务是唯一正本，平台只做读时代理
# ---------------------------------------------------------------------------
def _sha(obj: Any) -> str:
    """规范化 JSON 的 sha256。键排序 + 不转义中文，保证两边算出来一样。"""
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_item(raw: Dict[str, Any], pid: int, batch: Dict[str, Any],
                    title: str) -> Dict[str, Any]:
    """把 LS 原生 task 行摊平成平台要展示的字段。

    `data` 与 `meta` **原样透传**，不做映射、不挑字段、不生成"示例内容"：
    标注员在平台上看到的这条数据，就是要标的那条数据。data_sha256 让验证
    脚本可以逐行证明平台没有偷偷换过内容。
    """
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    drafts = raw.get("drafts") if isinstance(raw.get("drafts"), list) else []
    annotators = raw.get("annotators") if isinstance(raw.get("annotators"), list) else []
    tid = _int(raw.get("id"))
    return {
        "ls_task_id": tid,
        "ls_project_id": pid,
        "inner_id": _int(raw.get("inner_id")),
        # 批次只是合同/结算容器，用来分组，不替代 LS 任务行
        "batch_id": batch["id"],
        "batch_no": batch.get("batch_no"),
        "batch_name": batch.get("name"),
        "ls_title": title,
        "data": data,
        "meta": meta,
        "data_sha256": _sha(data),
        "is_labeled": bool(raw.get("is_labeled")),
        "total_annotations": _int(raw.get("total_annotations")),
        "cancelled_annotations": _int(raw.get("cancelled_annotations")),
        "annotators": annotators,
        "drafts": len(drafts),
        "has_draft": bool(drafts),
        "created_at": raw.get("created_at") or "",
        "updated_at": raw.get("updated_at") or "",
        # 逐条进 LS 的路径。前端拿它去换一张带 ls_task_id 的票据，
        # 服务端核实归属后才允许落地；这里绝不放完整 URL 让浏览器直跳
        # （那会绕过 SSO session）。
        #
        # 已标注任务不能用 labeling=1：LS 的标注流队列只排未标注任务，
        # 带 labeling=1&task=<已标注id> 会被静默跳到「下一条未标注」，
        # 于是平台点第 N 行、LS 打开的却是第 N+1 行。改为落到数据管理
        # 视图 /data?task=<id>，那里能精确选中这一条并显示已有标注结果。
        # 实测：project 13 的 task 5 已标注时，labeling=1&task=5 渲染的是 #6。
        "open_path": (
            f"/projects/{pid}/data?task={tid}"
            if bool(raw.get("is_labeled"))
            else f"/projects/{pid}/data?labeling=1&task={tid}"
        ),
    }


async def _fetch_rows(pids: List[int]) -> Dict[int, Dict[str, Any]]:
    """并发读每个白名单项目的**同一份** LS 任务行。单个项目失败不影响其他。"""
    async def title_of(pid: int) -> str:
        try:
            return (await ls.get_project(pid)).get("title") or ""
        except Exception:  # noqa: BLE001 - 标题只是展示，读不到不影响数据一致性
            return ""

    async def one(pid: int):
        try:
            rows, title = await asyncio.gather(ls.list_tasks(pid), title_of(pid))
            return pid, {"rows": rows, "title": title, "error": ""}
        except IntegrationError as exc:
            return pid, {"rows": None, "title": "", "error": str(exc)[:160]}
        except Exception as exc:  # noqa: BLE001
            return pid, {"rows": None, "title": "",
                         "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    pairs = await asyncio.gather(*(one(pid) for pid in sorted(set(pids))))
    return {pid: info for pid, info in pairs}


async def build_items(conn: Any,
                      remote: Optional[Dict[int, Dict[str, Any]]] = None,
                      compact: bool = False) -> Dict[str, Any]:
    """构造「标注员手里的真实数据行」。纯读，不写任何表、不缓存任何行。

    `conn` 必须已由 db.tenant_conn(p) 建好：项目集合来自 RLS 下该账号可见
    的批次，调用方无法指定任意 ls_project_id。`remote` 供定时对账/管理侧
    对账复用本轮已经取过的行，格式 {pid: {"rows": [...], "title": str,
    "error": str}}；不传就本次请求实时并发去 LS 取。

    `compact=True` 时只回汇总与指纹、不回全量行（healthz / 对账面板用），
    但指纹仍是对**同一批行**算出来的，自检不是另拉一份数据。
    """
    # 与 build_assignment / ls_bridge 的票据白名单是同一条 SQL。三处必须
    # 保持完全一致，否则「平台列出的行」和「SSO 放行的项目」会悄悄分叉。
    cur = await conn.execute(
        "select t.id, t.batch_no, t.name, t.item_count, t.ls_project_id, "
        "       t.assignee_id, sm.user_id as assignee_user_id "
        "from forge.tasks t "
        "left join forge.supplier_members sm on sm.id = t.assignee_id "
        "order by t.id")
    batches = [dict(r) for r in await cur.fetchall()]
    cur = await conn.execute(
        "select distinct ls_project_id from forge.tasks "
        "where ls_project_id is not null")
    ticket_projects = sorted({_int(r["ls_project_id"]) for r in await cur.fetchall()
                              if r["ls_project_id"]})

    pids = [_int(b["ls_project_id"]) for b in batches if b["ls_project_id"]]
    if remote is None:
        remote = await _fetch_rows(pids) if pids else {}

    # 重复绑定：两个批次共用一个 LS 项目 = 两个标注员会看到对方的数据行
    seen: Dict[int, str] = {}
    dup: Dict[int, List[str]] = {}
    for b in batches:
        pid = _int(b["ls_project_id"])
        if not pid:
            continue
        if pid in seen:
            dup.setdefault(pid, [seen[pid]]).append(b["id"])
        else:
            seen[pid] = b["id"]

    items: List[Dict[str, Any]] = []
    projects: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    unreachable: List[Dict[str, Any]] = []
    issues: List[str] = []

    for b in batches:
        pid = _int(b["ls_project_id"])
        group: Dict[str, Any] = {
            "batch_id": b["id"], "batch_no": b.get("batch_no"),
            "batch_name": b.get("name"), "ls_project_id": pid or None,
            "ls_title": "", "state": "not_connected", "error": "",
            "items": [], "item_count": 0,
            "assignee_user_id": b.get("assignee_user_id"),
            "contract_items": _int(b.get("item_count")),
        }
        if not pid:
            group["error"] = "该批次尚未绑定标注引擎项目"
            if b.get("assignee_user_id"):
                issues.append(f"{b.get('batch_no') or b['id']} 已分配给标注员，但未接入标注引擎")
            groups.append(group)
            continue

        info = remote.get(pid) or {}
        rows = info.get("rows")
        title = info.get("title") or ""
        err = info.get("error") or ""
        group["ls_title"] = title
        if not isinstance(rows, list):
            group["state"] = "ls_missing" if "404" in err else "ls_unreachable"
            group["error"] = err or "本轮未取得该项目的实时数据行"
            unreachable.append({"ls_project_id": pid, "error": group["error"]})
            issues.append(
                f"{b.get('batch_no') or b['id']} 的数据无法核对（{group['state']}），"
                f"页面不会显示旧数据")
            groups.append(group)
            projects.append({"ls_project_id": pid, "batch_id": b["id"],
                             "title": title, "total": 0, "truncated": False,
                             "reachable": False, "error": group["error"]})
            continue

        truncated = len(rows) > MAX_ITEMS_PER_PROJECT
        use = rows[:MAX_ITEMS_PER_PROJECT]
        group["state"] = "ok"
        group["items"] = [_normalize_item(r, pid, b, title) for r in use]
        group["item_count"] = len(group["items"])
        items.extend(group["items"])
        projects.append({"ls_project_id": pid, "batch_id": b["id"],
                         "title": title, "total": len(rows),
                         "truncated": truncated, "reachable": True, "error": ""})
        groups.append(group)
        if truncated:
            issues.append(
                f"项目 #{pid} 有 {len(rows)} 行，超过单次核对上限 "
                f"{MAX_ITEMS_PER_PROJECT}，只显示前 {MAX_ITEMS_PER_PROJECT} 行，无法声称完整一致")

    for oid, ids in dup.items():
        issues.append(
            f"LS 项目 #{oid} 被多个批次共用：{'、'.join(ids)}"
            f"（标注员会互相看到数据）")

    # 指纹覆盖「哪一行 + 内容 + 标注状态 + 草稿 + 最后修改时间」。
    # 任何一行被增删改标，指纹都会变；删掉一行同样会变。
    parts = [
        ":".join([str(i["ls_project_id"]), str(i["ls_task_id"]),
                  i["data_sha256"], "1" if i["is_labeled"] else "0",
                  str(i["total_annotations"]), str(i["drafts"]),
                  i["updated_at"]])
        for i in items
    ]
    fingerprint = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()

    # 用「项目可达」而不是「有数据行」：一个刚建好、还没导入的空项目
    # 是合法的，不该被误报成「票据里有读不到的项目」。
    reachable = sorted({p["ls_project_id"] for p in projects if p["reachable"]})
    ticket_only = [p for p in ticket_projects if p not in reachable]
    if ticket_only:
        issues.append(f"票据白名单里有读不到数据行的项目：{ticket_only}")
    items_only = [p for p in reachable if p not in ticket_projects]
    if items_only:
        issues.append(f"数据行所属项目不在票据白名单里：{items_only}")

    checked_at = int(time.time())
    summary = {
        "source": "labelstudio-live",
        "checked_at": checked_at,
        # 页面上的「可标任务 N 条」必须由这个数派生，不能另用平台库的
        # item_count，也不能用另一次请求取回的旧统计数。
        "items": len(items),
        "labeled": sum(1 for i in items if i["is_labeled"]),
        "drafts": sum(1 for i in items if i["has_draft"]),
        "batches": len(groups),
        "not_connected": sum(1 for g in groups if g["state"] == "not_connected"),
        "ls_projects": len(reachable),
        "ticket_projects": ticket_projects,
        "unreachable": len(unreachable),
        "truncated": sum(1 for p in projects if p["truncated"]),
        "contract_items": sum(_int(b.get("item_count")) for b in batches),
        "fingerprint": fingerprint,
        "in_sync": not issues,
        "issues": issues,
    }
    out: Dict[str, Any] = {
        "ok": not unreachable and not any(p["truncated"] for p in projects),
        "source": "labelstudio-live",
        "checked_at": checked_at,
        "fingerprint": fingerprint,
        "groups": groups,
        "projects": projects,
        "unreachable": unreachable,
        "summary": summary,
    }
    if compact:
        out["items"] = []
        for g in out["groups"]:
            g.pop("items", None)
    else:
        out["items"] = items
    return out


def record_items(snapshot: Dict[str, Any], error: str = "") -> None:
    """把一轮数据行自检写进全局状态（healthz 会读）。"""
    s = snapshot.get("summary") or {}
    ITEMS_STATE.update(
        source=s.get("source") or "labelstudio-live",
        last_check_at=s.get("checked_at"),
        last_ok_at=s.get("checked_at") if s.get("in_sync") else ITEMS_STATE.get("last_ok_at"),
        in_sync=s.get("in_sync"),
        items=s.get("items", 0),
        labeled=s.get("labeled", 0),
        drafts=s.get("drafts", 0),
        ls_projects=s.get("ls_projects", 0),
        unreachable=s.get("unreachable", 0),
        truncated=s.get("truncated", 0),
        fingerprint=s.get("fingerprint", ""),
        issues=(s.get("issues") or [])[:12],
        error=error[:200],
    )


def items_snapshot() -> Dict[str, Any]:
    return dict(ITEMS_STATE)


@router.get("/assignment/items")
async def my_assignment_items(p: Principal = Depends(current_principal)):
    """当前账号**手里的真实数据行**：本次请求实时代理 LS /api/tasks。

    平台不保存、不复制、不缓存任何一行：LS 里加一条、删一条、标一条，
    刷新本接口立刻反映在 items 与 fingerprint 上。LS 不可达时该项目
    一行都不返回（unreachable 明确列出），绝不用上一份数据假装一致。

    项目集合完全来自 RLS 下该账号可见的批次，接口不接受任何项目参数，
    所以标注员拿不到别人的数据行。
    """
    async with db.tenant_conn(p) as conn:
        return await build_items(conn)
