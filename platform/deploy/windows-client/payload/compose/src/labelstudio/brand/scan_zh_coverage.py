#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FORGE · 中文覆盖度扫描
============================================================================
汉化做到哪儿了，不能凭截图目测。这个脚本登录后逐个页面遍历，把**仍然是英文的
可见文案**连同样式容器一起抓出来，按出现次数排序。

用法
----------------------------------------------------------------------------
    python scan_zh_coverage.py                          # 扫默认的 8 个页面
    python scan_zh_coverage.py --project 3              # 换项目
    python scan_zh_coverage.py --pages /projects/1/data # 只扫指定页面
    python scan_zh_coverage.py --json D:/zh-gaps.json   # 结果落盘，便于逐个补词条

输出怎么用
----------------------------------------------------------------------------
    top 列表里出现次数多、且明显是 UI 文案的（"Save" / "Cancel" / "Columns" 之类），
    直接补进 zh-cn-overrides.json 的 entries，再跑：

        python build_zh_dict.py && python apply_brand.py
        重启 Label Studio

    容器名带 lsf- 的是标注界面内部的**数据处理区**，按设计不翻译，别往里加词条。

判据
----------------------------------------------------------------------------
    · 只取可见节点（offsetParent 非空、且 rect 有面积）
    · 跳过与 zh-cn.template.js 里 SKIP_SELECTOR 一致的容器（任务正文/标签名/代码）
    · 只保留含 >=2 个连续拉丁字母的文本（过滤掉 "0/0" "12:31" "→" 这类）
    · 纯数字+单位、URL、任务 ID 之类不计入
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_brand import find_browser, login, browser_session, navigate  # noqa: E402

DEFAULT_PAGES = [
    "/projects/",
    "/projects/{p}/data",
    "/projects/{p}/settings/general",
    "/projects/{p}/settings/labeling",
    "/projects/{p}/settings/cloud-storage",
    "/projects/{p}/settings/webhooks",
    "/projects/{p}/settings/machine-learning",
    "/user/account",
]

# 与 zh-cn.template.js 的 SKIP_SELECTOR 保持一致。不一致就等于自欺欺人——
# 扫描器报「这里没翻译」，实际是设计上不翻译。
SKIP_SELECTOR = (
    "input,textarea,select,code,pre,script,style,svg,"
    "[contenteditable='true'],"
    ".lsf-html,.lsf-text,.lsf-richtext,.lsf-object,.lsf-labels,.lsf-region,"
    ".lsf-table__cell:not(.lsf-th),[data-forge-skip],"
    # 项目卡片上的项目名与描述是**用户数据**，不是 UI 文案
    ".lsf-project-card__title-text,.lsf-project-card__description,"
    ".lsf-breadcrumbs__label"   # 面包屑里的项目名，也是用户数据
)

# 标注配置里的标签类型名。上游补丁同样刻意保留英文（Text/Audio/Video 都是
# 原样保留），因为它们是 Label Studio 的 tag 名，翻成中文会让人对不上官方文档。
TAG_TYPE_NAMES = {
    "Text", "Audio", "Video", "Image", "HTML", "HyperText", "Choices", "Labels",
    "TextArea", "Rating", "Taxonomy", "Number", "DateTime", "Paragraphs",
    "KeyPoint", "Polygon", "Rectangle", "Ellipse", "Brush", "KeyPointLabels",
    "Pairwise", "Ranker", "TimeSeries", "Table", "List", "Vector",
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
INITIALS_RE = re.compile(r"[A-Z]{1,3}")
IDENT_RE = re.compile(r"[a-z][a-z0-9_]*")     # main_product / low_contrast / apparel
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")


def mostly_chinese(text: str) -> bool:
    """中文占到一定比例 → 这条已翻译，只是夹了 ctrl / Shift / Webhook 之类拉丁字母。

    初版判据是「含 >=2 个连续拉丁字母就算英文残留」，结果把
    「允许图像缩放（ctrl+滚轮）」「按 Shift + Enter 添加」全报成缺口。
    单纯比字数也不够：「按 Shift + Enter 添加」里拉丁 10 个、汉字 3 个，
    但它是翻好的。所以用比例阈值 + 下面「译文白名单」双保险。
    """
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    if cjk < 2:
        return False
    return cjk / (cjk + latin) >= 0.15


def load_translated_values() -> set[str]:
    """词库里所有「译文」构成的集合。

    如果网页上这段文字本身就在译文集合里，那它当然翻过了——这条判据比任何
    字符统计都可靠。「Webhook」（Webhooks 的译文）、「Redis 存储」、
    「Amazon S3」「Choices」（上游刻意保留的英文标识）都归这一类。
    """
    here = Path(__file__).resolve().parent
    vals: set[str] = set()
    for name in ("zh-cn-upstream.json", "zh-cn-overrides.json"):
        f = here / name
        if not f.exists():
            continue
        try:
            entries = json.loads(f.read_text(encoding="utf-8")).get("entries", {})
        except (ValueError, OSError):
            continue
        vals.update(entries.values())
    return vals


COLLECT_JS = """
(() => {
  const SKIP = %s;
  const LATIN = /[A-Za-z]{2,}/;
  const NOISE = /^(https?:\\/\\/|www\\.|\\/[\\w-]+\\/)/;
  const out = [];
  const seen = new Set();

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || cs.opacity === "0") return false;
    return true;
  }

  function container(el) {
    if (!el) return "";
    const cls = (el.className && typeof el.className === "string") ? el.className : "";
    return cls.split(/\\s+/).filter(Boolean).slice(0, 2).join(".");
  }

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const raw = n.nodeValue;
    if (!raw || !raw.trim()) continue;
    const t = raw.trim();
    if (t.length > 200) continue;
    if (!LATIN.test(t)) continue;
    if (NOISE.test(t)) continue;
    const p = n.parentElement;
    if (!p) continue;
    if (p.closest(SKIP)) continue;
    if (!visible(p)) continue;
    const key = t + "|" + container(p);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ text: t, where: container(p), tag: p.tagName.toLowerCase() });
  }

  // 属性文案单独算一类：它们经常是 tooltip/placeholder，看不见但用户会碰到
  for (const el of document.querySelectorAll("[placeholder],[title],[aria-label]")) {
    if (el.closest(SKIP)) continue;
    if (!visible(el)) continue;
    for (const a of ["placeholder", "title", "aria-label"]) {
      const v = el.getAttribute(a);
      if (!v || !v.trim() || !LATIN.test(v)) continue;
      const t = v.trim();
      if (t.length > 200) continue;
      const key = t + "|@" + a;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ text: t, where: "@" + a, tag: el.tagName.toLowerCase() });
    }
  }

  return out;
})()
""" % json.dumps(SKIP_SELECTOR)


TRANSLATED_VALUES: set[str] = set()


def classify(text: str, where: str) -> str:
    """把「看起来是英文」分成几类。

    scan 一开始报 52 条，其中大部分根本不是缺口：项目名、标签名、邮箱缩写，
    以及**我们自己翻好的中文**。不分类的话每次都得人工筛，等于没做自动化。
    """
    if text in TRANSLATED_VALUES:
        return "done"           # 就是词库里的译文
    if text in TAG_TYPE_NAMES:
        return "tag"            # 标签类型名，与上游一致，保留英文
    if mostly_chinese(text):
        return "done"           # 已中文化，只是夹了拉丁字母
    if EMAIL_RE.search(text):
        return "data"           # 邮箱
    if INITIALS_RE.fullmatch(text):
        return "data"           # 头像缩写 AD / FO
    if IDENT_RE.fullmatch(text):
        return "data"           # main_product / low_contrast 这类标识符
    return "gap"


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE 中文覆盖度扫描")
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--email", default="admin@forge.local")
    ap.add_argument("--password", default="ForgeAdmin2026!")
    ap.add_argument("--project", default="1")
    ap.add_argument("--pages", nargs="*", default=None, help="覆盖默认页面列表")
    ap.add_argument("--browser", default=None)
    ap.add_argument("--json", default=None, help="把明细写到这个文件")
    ap.add_argument("--settle", type=float, default=5.0)
    args = ap.parse_args()

    global TRANSLATED_VALUES
    TRANSLATED_VALUES = load_translated_values()

    cookies = login(args.base, args.email, args.password)
    browser = find_browser(args.browser)
    pages = args.pages or [u.format(p=args.project) for u in DEFAULT_PAGES]

    counter: Counter = Counter()
    details: dict[str, dict] = {}

    with browser_session(browser, cookies) as cdp:
        for path in pages:
            url = args.base + path
            try:
                navigate(cdp, url, args.settle)
                found = cdp.eval_js(COLLECT_JS) or []
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] {path} 扫描失败：{exc}")
                continue
            print(f"{path:<44} 残留英文 {len(found):>3} 条")
            for item in found:
                t = item["text"]
                counter[t] += 1
                details.setdefault(t, item)

    buckets: dict[str, list[tuple[str, int]]] = {"gap": [], "data": [], "tag": [], "done": []}
    for text, count in counter.most_common():
        buckets[classify(text, details[text].get("where", ""))].append((text, count))

    gaps = buckets["gap"]
    print("-" * 78)
    print(f"疑似 UI 缺口 {len(gaps)} 条 ｜ 已中文化 {len(buckets['done'])} 条"
          f" ｜ 业务数据 {len(buckets['data'])} 条 ｜ 标签类型名 {len(buckets['tag'])} 条"
          f"（后三类按设计或已达标，不算缺口）")

    if not gaps:
        print("\n没有 UI 缺口。")
    else:
        print("\nUI 缺口（补进 zh-cn-overrides.json → build_zh_dict.py → apply_brand.py）：")
        for text, count in gaps:
            where = details[text]["where"] or details[text]["tag"]
            print(f"  x{count:<2} {text!r:<62} [{where}]")

    if buckets["tag"]:
        print("\n保留英文的标签类型名：" + "、".join(t for t, _ in buckets["tag"]))

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            [{"text": t, "count": c, **details[t],
              "kind": classify(t, details[t].get("where", ""))}
             for t, c in counter.most_common()],
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明细已写入 {p}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
