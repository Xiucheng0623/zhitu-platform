"""
FORGE Orchestrator · 标注引擎定时对账（拉取兜底）
================================================================
一句话：**webhook 是加速路径，不是唯一路径。**

为什么必须有这个循环（真实事故）：
  平台 ↔ Label Studio 的进度同步原先只有一条路 —— LS 推 webhook 过来。
  那条路上有五个各自都能让流程静默断掉的地方：
    · LS 里没挂 webhook（新建项目时忘了）
    · LS 升版把事件名/字段名改了（actions vs event_actions、单复数）
    · 内网回调被 LS 的 SSRF 防护 403 拦掉
    · 编排层重启那几秒里发生的事件（LS 不重投）
    · 编排层挂了半天，回来时不会补历史
  每一条都不报错，平台只是安静地停在旧数字上。运营看到的就是"进度不动"。

  现在多了一条拉的路：即使一条回调都没收到，最多 `ls_sync_interval_s` 之后
  也会自己追上。两条路同时坏才会出现长期不一致，而这种情况下界面上
  「标注引擎对账」面板会把差异摆出来，人也能手动同步。

边界（为什么这么写）：
  · 只回写**进度与名称**，绝不碰 item_count（合同口径，见 repo.apply_ls_progress）；
  · 每个批次单独 try：一个项目 404 不该让整轮中断；
  · LS 不可达时**降频重试**而不是每 5 分钟刷一遍错误日志；
  · 全部走幂等写入，和手动同步/webhook 三路并发也安全（数字没变就不写库）；
  · 每轮顺带跑一次**标注员任务一致性自检**（assignment.build_assignment），
    复用本轮已取到的 LS 数据，不额外压 LS；结果写进 /healthz 的
    assignment 字段。集合漂移（批次没接 LS、项目被共用、票据白名单对不上）
    不会白屏也不会报错，只能靠这个自检暴露出来。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from . import assignment
from .assignment import build_assignment
from .clients import ls
from .config import settings
from .db import tenant_conn
from .repo import apply_ls_progress, diff_ls, tasks_with_ls
from .security import Principal

log = logging.getLogger("forge.autosync")

# 给 healthz / 对账接口看的运行状态。
STATUS: Dict[str, Any] = {
    "enabled": False,
    "interval_s": 0,
    "last_run_at": None,      # epoch 秒
    "last_ok_at": None,
    "last_changed": 0,        # 上一轮改了几个批次
    "last_error": "",
    "consecutive_errors": 0,
    "runs": 0,
}

_stop = asyncio.Event()


def service_principal() -> Principal:
    """服务主体：没有终端用户身份，但仍要过 RLS（按默认租户）。"""
    return Principal(sub="svc_autosync", tenant_id=settings.default_tenant,
                     roles=["platform_admin"], name="定时对账", is_service=True)


async def run_once() -> Dict[str, Any]:
    """执行一轮对账。返回本轮统计。"""
    svc = service_principal()
    async with tenant_conn(svc) as conn:
        tasks = await tasks_with_ls(conn, svc)

    checked = 0
    changed: list[str] = []
    errors: list[str] = []
    # 本轮已经取到的 LS 实时数据，交给一致性自检复用，避免同一项目问两遍。
    # with_rows=True 把同一份任务行带出来：项目数/进度统计与「标注员手里的
    # 数据行」从一次 list_tasks 派生，自检不需要再拉一遍、更不需要在平台
    # 库里另存一份（另存就是两份数据会不同步的开始）。
    remote: Dict[int, Dict[str, Any]] = {}
    for t in tasks:
        checked += 1
        pid = t.get("ls_project_id")
        try:
            summary = await ls.project_summary(int(pid), with_rows=True)
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
            errors.append(f"{t['id']}:#{pid} {str(exc)[:80]}")
            remote[int(pid)] = {"summary": None, "error": str(exc)[:160]}
            continue
        rows = summary.get("rows") or []
        title = summary.get("title") or ""
        # 下游 diff_ls / apply_ls_progress 只认统计字段，别把全量行带进去
        summary = {k: v for k, v in summary.items() if k != "rows"}
        remote[int(pid)] = {
            "summary": summary, "rows": rows, "title": title, "error": "",
        }
        # 只处理"该同步"的差异：条目数不符是合同口径问题，定时任务不动它。
        flags = diff_ls(t, summary)["flags"]
        if not [f for f in flags if f != "items"]:
            continue
        # 标题名以平台为准，顺手在 LS 侧对齐（失败不影响进度回写）
        if "title" in flags:
            try:
                await ls.update_project(int(pid), title=summary_title(t))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{t['id']} 改名失败 {str(exc)[:60]}")
        try:
            async with tenant_conn(svc) as conn:
                fresh = await _reload(conn, t["id"])
                if fresh is None:
                    continue
                out = await apply_ls_progress(conn, svc, fresh, summary, "auto")
            if out.get("changed"):
                changed.append(t["id"])
                log.info("定时对账修正 %s：%s→%s/%s", t["id"],
                         out.get("previous_status"), out.get("done_count"),
                         out.get("item_count"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{t['id']} 写入失败 {str(exc)[:80]}")

    # 标注员一致性自检：全量、管理视角、只读。
    # 两层分别写进 /healthz：
    #   assignment       平台分配集合 / 项目数 / 可核对性
    #   assignment_items 真实数据行的条数、指纹、不可达与截断
    # 这不是"再同步一次"，而是把漂移摆到监控能看见的地方。
    try:
        async with tenant_conn(svc) as conn:
            snap = await build_assignment(conn, remote)
            isnap = await assignment.build_items(conn, remote, compact=True)
        assignment.record(snap)
        assignment.record_items(isnap)
        issues = snap["summary"]["issues"] + isnap["summary"]["issues"]
        if issues:
            log.warning("标注员一致性异常 %d 项：%s",
                        len(issues), "；".join(issues[:3]))
    except Exception as exc:  # noqa: BLE001 - 自检失败不能拖垮对账循环
        assignment.record({}, f"{type(exc).__name__}: {exc}")
        assignment.record_items({}, f"{type(exc).__name__}: {exc}")
        errors.append(f"assignment {type(exc).__name__}: {str(exc)[:80]}")

    return {"checked": checked, "changed": changed, "errors": errors}


def summary_title(task: Dict[str, Any]) -> str:
    """和 repo.desired_ls_title 同一个口径（这里只是避免循环导入）。"""
    return f"{task.get('batch_no') or ''} · {task.get('name') or ''}".strip(" ·")


async def _reload(conn: Any, task_id: str) -> Optional[Dict[str, Any]]:
    """拿最新行：上一轮可能已经改过 done_count，用旧对象算差异会双重写。"""
    from .repo import get_task
    return await get_task(conn, task_id)


async def _loop() -> None:
    interval = settings.ls_sync_interval_s
    STATUS.update(enabled=interval > 0, interval_s=interval)
    if interval <= 0:
        log.info("定时对账已关闭（FORGE_LS_SYNC_INTERVAL_S=0）")
        return
    log.info("定时对账已启用：每 %s 秒一次（webhook 之外的兜底）", interval)
    # 启动后先等一会儿：让连接池、LS 的 token 刷新走完，别在最忙的时候插一脚。
    await asyncio.sleep(min(15, interval))
    while not _stop.is_set():
        t0 = time.time()
        try:
            res = await run_once()
            STATUS.update(runs=STATUS["runs"] + 1, last_run_at=round(time.time(), 1),
                          last_changed=len(res["changed"]),
                          last_error="; ".join(res["errors"][:3]))
            if res["errors"]:
                STATUS["consecutive_errors"] = STATUS["consecutive_errors"] + 1
            else:
                STATUS["consecutive_errors"] = 0
                STATUS["last_ok_at"] = round(time.time(), 1)
            if res["changed"]:
                log.info("定时对账本轮修正 %s 个批次：%s",
                         len(res["changed"]), "、".join(res["changed"][:8]))
        except Exception as exc:  # noqa: BLE001 - 循环绝不能因为一轮异常就退出
            STATUS.update(last_run_at=round(time.time(), 1),
                          last_error=f"{type(exc).__name__}: {exc}"[:200],
                          consecutive_errors=STATUS["consecutive_errors"] + 1)
            log.warning("定时对账本轮失败：%s", exc)

        # LS 长时间不可达时降频到最多 5 倍间隔，避免刷日志和压垮恢复中的服务。
        backoff = 1
        if STATUS["consecutive_errors"] >= 3:
            backoff = min(5, 1 + STATUS["consecutive_errors"] // 3)
        wait = max(1.0, interval * backoff - (time.time() - t0))
        try:
            await asyncio.wait_for(_stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def start() -> Optional[asyncio.Task]:
    """由 lifespan 调用。返回 Task 便于关闭时取消。"""
    _stop.clear()
    return asyncio.ensure_future(_loop())


async def stop(task: Optional[asyncio.Task]) -> None:
    _stop.set()
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


def snapshot() -> Dict[str, Any]:
    """给接口/健康检查的状态快照（不含内部字段）。"""
    return dict(STATUS)
