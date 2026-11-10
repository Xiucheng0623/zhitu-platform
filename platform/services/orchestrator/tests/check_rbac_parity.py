"""
FORGE · RBAC 规则表一致性校验
================================================================
`services/orchestrator/app/security.py` 的 ROLE_MATRIX 必须与
`web/src/lib/rbac.ts` 逐条一致。这两份表是同一个东西的两个副本，
一旦漂移，会出现「按钮可见但接口 404」或更糟的「按钮不可见但接口能调」——
后者是安全问题，前者是客服工单。

为什么用脚本解析 TS 源码而不是让前端导出 JSON 再比：
  需要在不启动前端、不装 node_modules 的情况下也能在 CI / 提交前跑。
  正则只匹配 `role: [ "action", ... ]` 这种固定形状，硬编码的枚举值
  不影响解析；真要改形状，这个脚本会直接失败提醒你更新它，而不是静默跳过。

之前文档里写了「tests/test_contract.py 会比对」，但那个文件从未存在。
这个脚本就是它。

用法：
  python tests/check_rbac_parity.py
  RBAC_ROOT=../.. python tests/check_rbac_parity.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RBAC_ROOT", HERE.parents[2])).resolve()
PY_APP = HERE.parent / "app"
TS_FILE = ROOT / "web" / "src" / "lib" / "rbac.ts"
TS_TYPES = ROOT / "web" / "src" / "lib" / "types.ts"

sys.path.insert(0, str(HERE.parent))


def load_python() -> tuple[dict[str, set[str]], set[str]]:
    from app.security import ALL_ACTIONS, ROLE_MATRIX  # noqa: PLC0415
    return {k: set(v) for k, v in ROLE_MATRIX.items()}, set(ALL_ACTIONS)


def load_typescript() -> tuple[dict[str, set[str]], set[str]]:
    src = TS_FILE.read_text(encoding="utf-8")
    marker = "const ROLE_MATRIX: Record<RoleCode, Action[]> = {"
    if marker not in src:
        raise SystemExit(f"未找到 ROLE_MATRIX 定义，rbac.ts 结构已变更：{TS_FILE}")
    body = src.split(marker, 1)[1].split("\n};", 1)[0]

    roles: dict[str, set[str]] = {}
    for m in re.finditer(r"^  (\w+):\s*\[(.*?)\],?\s*$", body, re.M | re.S):
        roles[m.group(1)] = set(re.findall(r'"([a-z]+:[a-z]+)"', m.group(2)))
    if not roles:
        raise SystemExit("ROLE_MATRIX 解析结果为空，正则与源码形状不匹配")

    # Action 联合类型里声明的动作全集
    action_block = src.split("export type Action =", 1)[1].split(";", 1)[0]
    declared = set(re.findall(r'"([a-z]+:[a-z]+)"', action_block))
    return roles, declared


def load_role_codes() -> set[str]:
    src = TS_TYPES.read_text(encoding="utf-8")
    block = src.split("export type RoleCode =", 1)[1].split(";", 1)[0]
    return set(re.findall(r'"(\w+)"', block))


def main() -> int:
    py_roles, py_actions = load_python()
    ts_roles, ts_declared = load_typescript()
    ts_codes = load_role_codes()

    failures: list[str] = []
    print(f"角色数：python={len(py_roles)} typescript={len(ts_roles)}")
    print(f"动作数：python={len(py_actions)} typescript={len(ts_declared)}")

    if ts_codes != set(ts_roles):
        failures.append(
            f"types.ts 的 RoleCode 与 rbac.ts 的角色集合不一致："
            f"types-only={sorted(ts_codes - set(ts_roles))} "
            f"rbac-only={sorted(set(ts_roles) - ts_codes)}")
    if set(py_roles) != set(ts_roles):
        failures.append(
            f"角色集合不一致：py-only={sorted(set(py_roles) - set(ts_roles))} "
            f"ts-only={sorted(set(ts_roles) - set(py_roles))}")

    for role in sorted(set(py_roles) & set(ts_roles)):
        a, b = py_roles[role], ts_roles[role]
        if a != b:
            failures.append(
                f"[{role}] py-only={sorted(a - b)} ts-only={sorted(b - a)}")

    used = set().union(*ts_roles.values()) if ts_roles else set()
    if used != ts_declared:
        failures.append(
            f"Action 联合类型与实际使用不一致："
            f"声明未用={sorted(ts_declared - used)} 用了未声明={sorted(used - ts_declared)}")
    if py_actions != used:
        failures.append(
            f"动作全集不一致：py-only={sorted(py_actions - used)} "
            f"ts-only={sorted(used - py_actions)}")

    if failures:
        print("\nPARITY FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1

    # 数量从两侧实际解析结果取，不写死：写死过一次"29"，加了 flow:write 后
    # 变成 30，脚本自己却还印 29 —— 数字对不上时最难查的就是这种"看着过了"。
    print(f"\nPARITY OK —— {len(py_roles)} 角色 × {len(py_actions)} 动作，两侧逐条一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
