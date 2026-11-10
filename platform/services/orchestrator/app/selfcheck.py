"""
FORGE Orchestrator · 启动自检
================================================================
这里只放「错了不会报错、只会静默变坏」的那类检查。

为什么需要它（真实事故）：
  本项目的越权语义是**返回 404 而不是 403**（与前端「看不见即为不存在」一致）。
  这带来一个副作用：**权限名写错**和**资源真的不存在**在客户端看起来一模一样。
  写同步接口时用了矩阵里根本不存在的 `task:write`，点按钮只得到
  「任务不存在」，排查方向完全被带偏 —— 花了半小时才想到去看权限矩阵。

  这类错误的共同点：编译器不管、运行时不报、测试不覆盖，只有人肉点一遍
  才可能发现，而且大概率被误判成业务问题。所以放到启动时扫一遍。

设计取舍：
  · 用 **AST** 扫源码，不用正则。第一版是正则，启动就报了 4 条"未知权限"，
    其中 3 条来自**注释和文档字符串**里写的 `require("x")` 示例 —— 注释被当成
    真实调用。AST 只认 Call 节点，注释天然不算，误报归零。
  · 扫源码而不是遍历路由依赖：路由依赖藏在闭包里，要读 `__closure__` 才能拿到
    参数，既脆又只能看到已挂载的路由；AST 扫描顺带覆盖「写了 require 但没挂上」。
  · 只告警不致命。自检自身有 bug 把服务弄挂，比它要防的问题更糟。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Dict, List

from .security import ALL_ACTIONS

log = logging.getLogger("forge.selfcheck")

# require("x") / require_any("a", "b") 两类调用点
_GUARDS = ("require", "require_any")

# 第一次自检结果缓存在这里，healthz 直接读，避免每次请求都扫盘。
CACHE: Dict[str, object] = {}


def _scan_file(path: Path) -> Dict[str, List[str]]:
    """解析单个文件，收集其中的 require/require_any 常量参数。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    found: Dict[str, List[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name not in _GUARDS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.setdefault(arg.value, []).append(path.name)
    return found


def _scan_sources() -> Dict[str, List[str]]:
    """扫本包内所有 .py，收集「哪里用了哪个权限名」。"""
    here = Path(__file__).resolve().parent
    used: Dict[str, List[str]] = {}
    for f in sorted(here.glob("*.py")):
        for name, where in _scan_file(f).items():
            used.setdefault(name, []).extend(where)
    return used


def _mentioned_sources() -> set:
    """本包里所有出现过的字符串常量（含字典键/值）。

    为什么要单独统计"提及"：有些权限不是通过 require() 用的，而是放在查表里
    （例如 fsm.py 的状态迁移表 `("draft","submitted"): "settlement:write"`）。
    只统计 require 会把这类误判成"没人用"，进而给出误导性的结论。
    """
    here = Path(__file__).resolve().parent
    seen: set = set()
    for f in sorted(here.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                seen.add(node.value)
    return seen


def audit_permissions() -> Dict[str, object]:
    """检查代码里用到的权限名是否都在 ROLE_MATRIX 里。

    返回：
      unknown —— 矩阵里没有的权限名（写错了，或矩阵忘了加）
      unused  —— 矩阵里有但没人用（可能是历史遗留，仅提示）
      ok      —— unknown 为空
    """
    declared = set(ALL_ACTIONS)
    used = _scan_sources()
    mentioned = _mentioned_sources()
    unknown = sorted(k for k in used if k not in declared)
    # 只提示"全包都没提到"的：这类通常是前端在用、后端还没实现接口，
    # 属于功能缺口而不是越权洞（接口不存在就没人能调）。
    unused = sorted(a for a in declared if a not in mentioned)
    report: Dict[str, object] = {
        "ok": not unknown,
        "unknown": {k: sorted(set(v)) for k, v in used.items() if k not in declared},
        "unknown_count": len(unknown),
        "used_count": len(used),
        "declared_count": len(declared),
        "unused": unused,
    }
    return report


def run_startup_checks() -> Dict[str, object]:
    """启动时跑一遍并把结果放进 CACHE / 日志。"""
    perm = audit_permissions()
    CACHE["permissions"] = perm
    if not perm["ok"]:
        # 逐条打出来，带上出处文件 —— 只报"有未知权限"等于没报。
        for name, files in (perm["unknown"] or {}).items():  # type: ignore[union-attr]
            log.error("权限名 '%s' 在 ROLE_MATRIX 里不存在（出现在 %s）。"
                      "该接口会对所有人返回 404 —— 看起来像资源不存在，实际是拼错了。",
                      name, "、".join(files))
    else:
        log.info("权限自检通过：代码用到 %s 个动作，矩阵声明 %s 个", 
                 perm["used_count"], perm["declared_count"])
        if perm["unused"]:
            log.info("矩阵里后端尚未实现的权限（前端可能已按它隐藏入口）：%s",
                     "、".join(perm["unused"]))  # type: ignore[arg-type]
    return perm


def checks_summary() -> Dict[str, object]:
    """给 healthz 用的精简视图（不含 unused 这种长列表）。"""
    perm = CACHE.get("permissions")
    if perm is None:
        return {"permissions": {"ok": None, "reason": "未执行"}}
    return {"permissions": {"ok": perm["ok"],
                            "unknown": perm["unknown"],
                            "used": perm["used_count"],
                            "declared": perm["declared_count"]}}
