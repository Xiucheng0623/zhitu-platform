# -*- coding: utf-8 -*-
"""标注流程（甲方可编辑的"员工怎么标"）→ Label Studio label_config。

为什么单独一个模块、且只做纯函数
============================================================================
这段逻辑有三个使用方：平台接口（保存）、应用接口（写进 LS）、以及测试。
如果把 XML 生成塞在路由里，改一个字段就要连数据库才能验证，出错时也分不清
是"生成错了"还是"写库错了"。所以这里只做两件纯事：

  1. `build_config(payload)`  结构化步骤 → LS 认识的 XML（含条件分支）
  2. `validate(payload)`      校验步骤本身是否自相矛盾

设计约定（对外就是"甲方的编辑能力"）
----------------------------------------------------------------------------
一个项目的标注过程 = 一个**数据对象** + 一串**步骤**：

    {"object": "image",
     "steps": [
       {"kind": "rectanglelabels", "name": "bbox", "title": "框出主体",
        "required": true, "options": ["主体", "配件"]},
       {"kind": "choices", "name": "has_defect", "title": "是否有缺陷",
        "required": true, "options": ["有缺陷", "无缺陷"]},
       {"kind": "choices", "name": "defect_type", "title": "缺陷类型",
        "required": true, "options": ["划痕", "破损"],
        "depends_on": "has_defect", "depends_value": "有缺陷"}
     ]}

`depends_on` / `depends_value` 生成 `<View visibleWhen="choice-selected">`：
只有选了「有缺陷」，「缺陷类型」这一步才出现。这是"两步标注"最常用的写法。

刻意不支持的东西（说清楚比假装支持好）
----------------------------------------------------------------------------
· 任意 LSF 控件组合：只覆盖 8 种常用控件（见 KINDS）。要冷门控件就走
  Label Studio 后台手写 XML，平台这边照样能存（`label_config_raw` 直通）。
· 多级依赖（A 依赖 B、B 又依赖 C）：只允许一级。LS 支持嵌套，但一级之外
  的语义没法在这个界面上表达清楚，与其做半个还不如不做。
· 脚本类/动态控件：LSF 的 `visibleWhen` 之外还有 `whenLabelValue` 等条件，
  这里只实现 choice-selected —— 其余写法留给手写 XML。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

# 对象（数据载体）→ LSF 标签。value 固定按模态来，界面不让改：
# 改了 value（$image/$audio…）数据就挂不上，对甲方没有任何好处。
OBJECTS: Dict[str, Tuple[str, str]] = {
    "image": ("Image", '$image'),
    "audio": ("Audio", '$audio'),
    "video": ("Video", '$video'),
    "text": ("Text", '$text'),
}

# kind → (LSF 标签, 是否需要标签值, 是否天然按区域)
KINDS: Dict[str, Tuple[str, bool, bool]] = {
    "rectanglelabels": ("RectangleLabels", True, True),
    "polygonlabels": ("PolygonLabels", True, True),
    "keypointlabels": ("KeyPointLabels", True, True),
    "labels": ("Labels", True, True),          # 文本 span
    "choices": ("Choices", True, False),
    "textarea": ("TextArea", False, False),
    "rating": ("Rating", False, False),
    "number": ("Number", False, False),
}

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
MAX_STEPS = 24
MAX_OPTIONS = 40
MAX_TEXT = 120


def _esc(s: str) -> str:
    """XML 文本/属性转义。

    必须自己转：甲方填的标签值里出现 & < > " 是常事（"R&D"、"<5mm"），
    直接拼进 XML 会让整段配置语法错误，而 LS 只会回一句 "XML 不合法"，
    甲方根本看不出是自己那个 & 造成的。
    """
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _clean(s: Any, limit: int = MAX_TEXT) -> str:
    return str(s if s is not None else "").strip().replace("\x00", "")[:limit]


def normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把前端传来的结构收敛成规范形态（缺字段补默认、多余字段丢掉）。

    为什么要收敛：前端是"设计器"，字段会随界面演化增删；直接透传就会把
    界面临时状态（选中项、展开态）一起存进库，下次读出来还要过滤。
    """
    obj = _clean((payload or {}).get("object"), 20) or "image"
    raw_steps = (payload or {}).get("steps") or []
    steps: List[Dict[str, Any]] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        kind = _clean(raw.get("kind"), 24)
        opts = raw.get("options") or []
        if isinstance(opts, str):
            opts = [x for x in re.split(r"[\n,]", opts)]
        steps.append({
            "id": _clean(raw.get("id"), 24) or f"s{i + 1}",
            "kind": kind,
            "name": _clean(raw.get("name"), 40),
            "title": _clean(raw.get("title"), 60),
            "required": bool(raw.get("required")),
            "per_region": bool(raw.get("per_region")),
            "options": [_clean(o, MAX_TEXT) for o in opts if _clean(o, MAX_TEXT)],
            "depends_on": _clean(raw.get("depends_on"), 40),
            "depends_value": _clean(raw.get("depends_value"), MAX_TEXT),
        })
    return {"object": obj, "steps": steps[:MAX_STEPS]}


def validate(payload: Dict[str, Any]) -> List[str]:
    """返回人类可读的错误列表；空列表 = 通过。

    为什么不分字段返回结构化的错误：这些错误最终都会原样显示在甲方眼前，
    与其让前端再翻译一遍，不如在后端拼好一句人话（"第 2 步「缺陷类型」
    依赖的选项不在「是否有缺陷」里"）。
    """
    errs: List[str] = []
    obj = payload.get("object")
    if obj not in OBJECTS:
        errs.append(f"数据对象只能是 {', '.join(OBJECTS)} 之一")
    steps = payload.get("steps") or []
    if not steps:
        return errs + ["至少要有一步标注，否则标注员打开是空白的"]

    seen: Dict[str, Dict[str, Any]] = {}
    for i, st in enumerate(steps, 1):
        label = st.get("title") or st.get("name") or f"第 {i} 步"
        kind = st.get("kind")
        if kind not in KINDS:
            errs.append(f"{label}：不支持的控件类型 {kind!r}")
            continue
        name = st.get("name") or ""
        if not NAME_RE.match(name):
            errs.append(f"{label}：字段名只能用字母开头的字母/数字/下划线（当前 {name!r}）")
        elif name in seen:
            errs.append(f"{label}：字段名 {name!r} 与上一步重复")
        elif name == obj:
            errs.append(f"{label}：字段名不能和数据对象同名（{obj}）")
        seen[name] = st

        need_opts = KINDS[kind][1]
        opts = st.get("options") or []
        if need_opts:
            if not opts:
                errs.append(f"{label}：至少要有一个可选值")
            if len(opts) > MAX_OPTIONS:
                errs.append(f"{label}：可选值最多 {MAX_OPTIONS} 个")
            dup = {o for o in opts if opts.count(o) > 1}
            if dup:
                errs.append(f"{label}：可选值重复（{'、'.join(sorted(dup))}）")

    # 条件分支：依赖必须指向前面某一步的某个选项，且只允许一级
    for i, st in enumerate(steps, 1):
        dep = st.get("depends_on")
        if not dep:
            continue
        label = st.get("title") or st.get("name") or f"第 {i} 步"
        target = seen.get(dep)
        if target is None:
            errs.append(f"{label}：它依赖的步骤 {dep!r} 不存在")
            continue
        if target.get("kind") != "choices":
            errs.append(f"{label}：只能依赖「单选/多选」步骤，{dep!r} 不是")
            continue
        if dep == st.get("name"):
            errs.append(f"{label}：不能依赖自己")
            continue
        if st.get("depends_value") not in (target.get("options") or []):
            errs.append(f"{label}：依赖的选项 {st.get('depends_value')!r} "
                        f"不在「{target.get('title') or dep}」的可选值里")
        if target.get("depends_on"):
            errs.append(f"{label}：暂不支持两级以上的条件（{dep!r} 本身已带条件）")
    return errs


def build_config(payload: Dict[str, Any]) -> str:
    """结构化步骤 → label_config XML 字符串。

    缩进按 LSF 习惯两空格，方便甲方切到代码视图时自己改。
    """
    obj = payload.get("object") or "image"
    tag, value = OBJECTS[obj]
    lines: List[str] = ["<View>"]
    extra = ' zoom="true" zoomControl="true"' if obj in ("image", "audio", "video") else ""
    lines.append(f'  <{tag} name="{_esc(obj)}" value="{value}"{extra}/>')

    for st in payload.get("steps") or []:
        lsf_tag, need_opts, natural_region = KINDS[st["kind"]]
        attrs = [f'name="{_esc(st["name"])}"', f'toName="{_esc(obj)}"']
        if st.get("required"):
            attrs.append('required="true"')
        # perRegion 只对"分类"类控件有意义：区域类控件本来就是每个区域一份
        if st.get("per_region") and not natural_region:
            attrs.append('perRegion="true"')
        attr_s = " ".join(attrs)

        body: List[str] = []
        if need_opts:
            for o in st.get("options") or []:
                body.append(f'    <Label value="{_esc(o)}"/>' if lsf_tag.endswith("Labels")
                            else f'    <Choice value="{_esc(o)}"/>')

        inner: List[str] = []
        # 标题必须作为 <Header> 写进去：Label Studio 对 Choices / RectangleLabels
        # 这些控件**不会**把 name 当标题显示。没有 Header 时，标注员在页面上
        # 只看到一排光秃秃的选项（实测截图：只有「有缺陷 / 无缺陷」两个框），
        # 根本不知道这一题在问什么。甲方在设计器里填的标题如果只存进库、
        # 不落到 XML，等于白填。
        title = (st.get("title") or "").strip()
        if title and title != st["name"]:
            # title == name 时不加：从手写 XML 反解出来的步骤标题就是 name，
            # 再加一个 Header 会在页面上重复显示一遍同样的字。
            inner.append(f'  <Header value="{_esc(title)}"/>')

        control = [f'  <{lsf_tag} {attr_s}>'] + body + [f'  </{lsf_tag}>']
        if not body:
            control = [f'  <{lsf_tag} {attr_s}/>']
        inner.extend(control)

        dep = st.get("depends_on")
        if dep:
            lines.append(
                '  <View visibleWhen="choice-selected" '
                f'whenTagName="{_esc(dep)}" whenChoiceValue="{_esc(st.get("depends_value"))}">'
            )
            lines.extend("  " + ln for ln in inner)
            lines.append("  </View>")
        else:
            lines.extend(inner)
    lines.append("</View>")
    return "\n".join(lines)


def parse_config(xml: str) -> Dict[str, Any]:
    """把已有 XML 反向解析成步骤（用于"从现有配置开始编辑"）。

    只认得本模块自己会生成的形态：遇到不认识的控件就跳过，并在
    `unsupported` 里记下标签名 —— 前端会提示"有 N 个控件只能在代码视图改"，
    而不是假装它不存在然后一保存就把它删了。
    """
    out: Dict[str, Any] = {"object": "image", "steps": [], "unsupported": []}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {"object": "image", "steps": [], "unsupported": [], "parse_error": True}

    tag_to_kind = {v[0]: k for k, v in KINDS.items()}
    obj_from_tag = {v[0]: k for k, v in OBJECTS.items()}

    def walk(node: ET.Element, dep: str = "", dep_val: str = "") -> None:
        # 同一层里最近一个 <Header> 就是下一个控件的标题。
        # 这样 parse(build(x)) 能保住标题，而不是把中文标题丢成字段名。
        pending_title = ""
        for child in list(node):
            if child.tag == "Header":
                pending_title = child.get("value", "") or ""
                continue
            if child.tag == "View":
                when = child.get("visibleWhen")
                walk(child,
                     child.get("whenTagName", "") if when else dep,
                     child.get("whenChoiceValue", "") if when else dep_val)
                pending_title = ""
                continue
            if child.tag in obj_from_tag:
                out["object"] = obj_from_tag[child.tag]
                pending_title = ""
                continue
            kind = tag_to_kind.get(child.tag)
            if not kind:
                out["unsupported"].append(child.tag)
                pending_title = ""
                continue
            opts = [c.get("value", "") for c in list(child) if c.get("value") is not None]
            out["steps"].append({
                "id": f"s{len(out['steps']) + 1}",
                "kind": kind,
                "name": child.get("name", ""),
                "title": pending_title or child.get("name", ""),
                "required": child.get("required") == "true",
                "per_region": child.get("perRegion") == "true",
                "options": opts,
                "depends_on": dep,
                "depends_value": dep_val,
            })
            pending_title = ""

    walk(root)
    return out


# 内置步骤模板：甲方点一下就有个能用的起点，不用从空白开始设计。
# 与 labelstudio/configs/*.xml 是两套东西 —— 那边是 CLI provisioning 用的
# 完整成品，这里是"设计器里的起手式"，结构更小、可在此基础上改。
STEP_TEMPLATES: List[Dict[str, Any]] = [
    {
        "key": "bbox_category",
        "name": "图像框选 + 分类",
        "object": "image",
        "desc": "先框出目标，再选类目；适合商品图、工业质检",
        "steps": [
            {"kind": "rectanglelabels", "name": "bbox", "title": "框出目标", "required": True,
             "options": ["主体", "配件", "包装", "水印"]},
            {"kind": "choices", "name": "category", "title": "类目", "required": True,
             "options": ["服饰", "电子", "美妆", "家居"]},
        ],
    },
    {
        "key": "audio_verdict",
        "name": "语音判定",
        "object": "audio",
        "desc": "听音频给结论；适合客服质检、车载语音",
        "steps": [
            {"kind": "choices", "name": "verdict", "title": "判定", "required": True,
             "options": ["合格", "不合格"]},
            {"kind": "textarea", "name": "reason", "title": "不合格原因", "required": True,
             "depends_on": "verdict", "depends_value": "不合格"},
        ],
    },
    {
        "key": "text_ner",
        "name": "文本实体 + 意图",
        "object": "text",
        "desc": "划出实体片段并判断意图；适合工单、评论",
        "steps": [
            {"kind": "labels", "name": "entity", "title": "实体片段",
             "options": ["人名", "地名", "机构", "产品"]},
            {"kind": "choices", "name": "intent", "title": "意图", "required": True,
             "options": ["咨询", "投诉", "售后", "其他"]},
        ],
    },
    {
        "key": "two_stage_defect",
        "name": "两步缺陷判定（条件分支）",
        "object": "image",
        "desc": "先判有无缺陷，有缺陷才追问类型与位置",
        "steps": [
            {"kind": "choices", "name": "has_defect", "title": "是否有缺陷", "required": True,
             "options": ["有缺陷", "无缺陷"]},
            {"kind": "choices", "name": "defect_type", "title": "缺陷类型", "required": True,
             "options": ["划痕", "破损", "污渍", "变形"],
             "depends_on": "has_defect", "depends_value": "有缺陷"},
            {"kind": "rectanglelabels", "name": "defect_box", "title": "框出缺陷位置",
             "options": ["缺陷"],
             "depends_on": "has_defect", "depends_value": "有缺陷"},
        ],
    },
]
