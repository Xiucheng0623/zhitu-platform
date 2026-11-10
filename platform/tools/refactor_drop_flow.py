# -*- coding: utf-8 -*-
"""一次性重构：撤下平台自建标注流程设计器，只保留「绑定 LS 项目」能力。

方向依据：甲方应该进 Label Studio 用原生 Visual / Code 编辑器设计标注流程，
平台不再维护第二套表单设计器（8 种控件 + 一级条件的半成品，永远追不上上游）。

本脚本幂等：重复执行不会二次修改。跑完请执行
  python -m compileall services/orchestrator/app
  cd web && npm run typecheck && npm run build
"""
from __future__ import annotations

from pathlib import Path

P = Path(__file__).resolve().parents[1]  # platform/


def edit(rel: str, fn) -> None:
    p = P / rel
    s = p.read_text(encoding="utf-8")
    s2 = fn(s)
    if s2 != s:
        p.write_text(s2, encoding="utf-8")
        print("  changed  ", rel)
    else:
        print("  unchanged", rel)


def splice(s: str, start_mark: str, end_mark: str, replacement: str) -> str:
    """把 [start_mark, end_mark) 换成 replacement；两端标记本身按参数处理。"""
    i = s.index(start_mark)
    j = s.index(end_mark, i)
    return s[:i] + replacement + s[j:]


# ---------------------------------------------------------------------------
NEW_BINDING_BLOCK = '''class LsBindingBody(BaseModel):
    """把平台项目绑定到一个**已经存在**的 Label Studio 项目。"""
    ls_project_id: int = Field(gt=0)
    config_name: Optional[str] = Field(default=None, max_length=120)


@app.get("/api/v1/projects/{project_id}/ls-binding")
async def project_ls_binding(project_id: str,
                             p: Principal = Depends(require("project:read"))):
    """只读：这个项目绑没绑标注引擎、绑的是哪个 LS 项目。

    项目详情里的「在标注引擎里设计」按钮靠它判断能不能进。这不是设计器
    接口 —— 平台不存表单结构，`label_config` 的正本始终在 LS 里。

    `labeling_config_id` 身兼两职：新建项目时写占位串 `lsc_default`
    （意思是「用标准模板」），真正绑定 LS 之后才是数字项目号。只认数字，
    否则界面会显示「已绑定 #lsc_default」这种看起来像真的假话。
    """
    async with db.tenant_conn(p) as conn:
        proj = await repo.project_brief(conn, project_id)
    if not proj:
        raise HTTPException(404, "not found")
    raw = str(proj.get("labeling_config_id") or "").strip()
    ls_id = int(raw) if raw.isdigit() else None
    return {
        "bound": ls_id is not None,
        "ls_project_id": ls_id,
        "ls_config_name": proj.get("labeling_config_name") if ls_id else None,
    }


@app.post("/api/v1/projects/{project_id}/ls-binding")
async def project_ls_binding_save(project_id: str, body: LsBindingBody,
                                  request: Request,
                                  p: Principal = Depends(require("project:write"))):
    """管理员把平台项目绑到已有 LS 项目 —— 甲方进 LS 设计的前置条件。

    为什么不让平台顺手「新建」LS 项目：一个可用的 LS 项目还要挂数据源、
    云存储、成员与导入任务，这些都在 LS 里做。平台建个空壳只会让两边对不上，
    甲方点进去一条数据都没有。这里只做绑定，且先向 LS 核实项目真的存在，
    否则「在标注引擎里设计」会把人送进一个 404。

    权限刻意给 `project:write` 而不是 `labeling:write`：甲方能改表单，
    但不能改「这个项目连的是哪个 LS 项目」—— 那等于把所有数据换了个地方。
    """
    async with db.tenant_conn(p) as conn:
        proj = await repo.project_brief(conn, project_id)
    if not proj:
        raise HTTPException(404, "not found")
    try:
        remote = await ls.get_project(body.ls_project_id)
    except IntegrationError as exc:
        raise HTTPException(502, f"标注引擎里没有这个项目（#{body.ls_project_id}）：{exc}")
    name = (body.config_name or "").strip() or str(remote.get("title") or "标注界面")
    async with db.tenant_conn(p) as conn:
        await repo.set_project_ls(conn, p, project_id, body.ls_project_id,
                                  name, client_ip(request))
    return {"ok": True, "bound": True, "ls_project_id": body.ls_project_id,
            "ls_config_name": name}


'''


def fix_main(s: str) -> str:
    old_imp = ("from . import (auth, autosync, collection, db, finance, flow, ls_bridge, ops,\n"
               "               repo, selfcheck, storage)")
    new_imp = ("from . import (auth, autosync, collection, db, finance, ls_bridge, ops,\n"
               "               repo, selfcheck, storage)")
    if old_imp in s:
        s = s.replace(old_imp, new_imp)
    if "class FlowStep(BaseModel):" in s:
        s = splice(s, "class FlowStep(BaseModel):",
                   '@app.get("/api/v1/tasks/{task_id}/efficiency")',
                   NEW_BINDING_BLOCK)
    return s


def fix_repo(s: str) -> str:
    start = "# ---------------------------------------------------------------------------\n# 标注流程（甲方编辑的"
    if start in s:
        s = splice(s, start, "async def project_brief",
                   "# ---------------------------------------------------------------------------\n"
                   "# 项目 ↔ 标注引擎绑定\n"
                   "# ---------------------------------------------------------------------------\n"
                   "# 表单结构（label_config）不在这里，也不在平台库里：正本在 Label Studio，\n"
                   "# 甲方通过一次性票据进 LS 原生编辑器改。平台只记「这个项目连到哪个 LS 项目」。\n\n\n")
    s = s.replace(
        '    """项目的最小信息（编号/名称/LS 绑定）。设计器与"应用到引擎"都要用。\n\n'
        '    单独抽出来是因为 apply 需要 code+name 拼 LS 项目标题，而 get_flow 只返回\n'
        '    流程本身；顺带把 labeling_config_id 带着，避免调用方再查一次。\n'
        '    """',
        '    """项目最小信息（编号/名称/LS 绑定）。\n\n'
        '    绑定状态读取、设计票据签发都用它；顺带把 labeling_config_id 带出来，\n'
        '    避免调用方再查一次。\n'
        '    """')
    s = s.replace('    """把「平台项目 ↔ LS 项目」的绑定写回 projects。',
                  '    """把「平台项目 ↔ LS 项目」的绑定写回 projects。')
    s = s.replace('await write_audit(conn, p, "project.flow_apply", project_id,\n'
                  '                      f"绑定标注引擎项目 #{ls_project_id}（{config_name}）", ip)',
                  'await write_audit(conn, p, "project.ls_bind", project_id,\n'
                  '                      f"绑定标注引擎项目 #{ls_project_id}（{config_name}）", ip)')
    if "json.loads(steps)" not in s and "json.dumps(steps" not in s:
        s = s.replace("import json\n", "", 1)
    return s


def fix_clients(s: str) -> str:
    return s.replace(
        '        """读单个项目，主要为了拿到它当前的 label_config。\n\n'
        '        用途只有一个：甲方第一次在平台里打开设计器时，如果这个项目早就在\n'
        '        标注引擎里配过表单，得先把现状读回来当作起点 —— 否则他面对的是一张\n'
        '        空白画布，"保存并应用"会把他原来的配置整段覆盖掉，而且没有任何提示。\n'
        '        """',
        '        """读单个 LS 项目，主要用来核实「要绑定的项目号真的存在」。\n\n'
        '        绑定接口在写库之前先调它：绑到不存在的 LS 项目，甲方点「在标注引擎\n'
        '        里设计」只会进 404，而且界面上还显示「已绑定」—— 属于骗人。\n'
        '        顺带把 label_config 带回来，供对账/诊断读现状。\n'
        '        """')


def fix_types(s: str) -> str:
    mark = "/**\n * 标注流程（甲方可编辑的「员工怎么标」）"
    if mark in s:
        s = s[:s.index(mark)].rstrip() + "\n"
    return s


def fix_http(s: str) -> str:
    mark = "/**\n * 整体替换语义的写接口（标注流程、标注规范这类"
    if mark in s:
        i = s.index(mark)
        j = s.index("export function clearToken", i)
        s = s[:i] + s[j:]
    return s


def fix_entry(s: str) -> str:
    s = s.replace(
        "interface FlowBrief {\n"
        "  steps?: { object?: string; steps?: unknown[] };\n"
        "  ls_project_id?: string | number | null;\n"
        "  ls_config_name?: string | null;\n"
        "}",
        "interface LsBinding {\n"
        "  bound: boolean;\n"
        "  ls_project_id: number | null;\n"
        "  ls_config_name?: string | null;\n"
        "}")
    s = s.replace("useState<FlowBrief | null>", "useState<LsBinding | null>")
    s = s.replace("get<FlowBrief>(`/api/v1/projects/${projectId}/flow`)",
                  "get<LsBinding>(`/api/v1/projects/${projectId}/ls-binding`)")
    s = s.replace("\n  * 单项目白名单", "\n * 单项目白名单")
    return s


def main() -> int:
    print("撤下自建设计器，保留 LS 绑定能力：")
    edit("services/orchestrator/app/main.py", fix_main)
    edit("services/orchestrator/app/repo.py", fix_repo)
    edit("services/orchestrator/app/clients.py", fix_clients)
    edit("web/src/lib/types.ts", fix_types)
    edit("web/src/lib/http.ts", fix_http)
    edit("web/src/design/LsDesignEntry.tsx", fix_entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
