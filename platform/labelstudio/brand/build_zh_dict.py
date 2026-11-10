#!/usr/bin/env python3
"""合并两路词库，生成 zh-cn.js（中文界面层）。

    python build_zh_dict.py           生成 zh-cn.js
    python build_zh_dict.py --check   只校验 zh-cn.js 是否与输入一致（CI 用）

输入（都在本目录）：
    zh-cn.template.js     执行器 + 正则表，词库位置留占位符 /*__EXACT_JSON__*/
    zh-cn-upstream.json   社区补丁词库（897 条，目标 LS 1.23.0）
    zh-cn-overrides.json  本项目自维护词条（覆盖优先级最高）

为什么要有这个脚本，而不是直接手改 zh-cn.js：
    词库是 1000 条量级的数据，手改必然漏、必然格式漂移。把它做成「数据 + 生成器」
    之后，上游更新只要替换 zh-cn-upstream.json 再跑一次；本项目的品牌改写集中在
    zh-cn-overrides.json 一处就能看见，不会被 900 条上游词条淹没。

输出是 zh-cn.js —— apply_brand.py 只读这个文件，所以生成物必须提交并保持可 node --check。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "zh-cn.template.js"
UPSTREAM = HERE / "zh-cn-upstream.json"
OVERRIDES = HERE / "zh-cn-overrides.json"
OUTPUT = HERE / "zh-cn.js"
PLACEHOLDER = "/*__EXACT_JSON__*/"


def load(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise SystemExit(f"{path.name} 缺少 entries 字典")
    return entries


def build() -> tuple[str, dict[str, int]]:
    if not TEMPLATE.exists():
        raise SystemExit(f"找不到模板：{TEMPLATE}")

    upstream = load(UPSTREAM)
    overrides = load(OVERRIDES)

    merged: dict[str, str] = {}
    merged.update(upstream)
    merged.update(overrides)          # 覆盖优先

    # 去掉空译文，避免把文案清成空白
    merged = {k: v for k, v in merged.items() if k and v}

    ordered = {k: merged[k] for k in sorted(merged)}

    body = "{\n" + ",\n".join(
        "    %s: %s" % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False))
        for k, v in ordered.items()
    ) + "\n  }"

    tpl = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        raise SystemExit(f"模板里找不到占位符 {PLACEHOLDER}")

    stats = {
        "upstream": len(upstream),
        "overrides": len(overrides),
        "total": len(ordered),
        "overridden": len(set(upstream) & set(overrides)),
    }
    return tpl.replace(PLACEHOLDER, body), stats


def js_syntax_error(code: str) -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    fd, tmp_name = tempfile.mkstemp(suffix=".js", prefix="forge-zh-")
    tmp = Path(tmp_name)
    try:
        tmp.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True, timeout=60)
        return (r.stderr or r.stdout).strip()[:800] if r.returncode else None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    code, stats = build()

    err = js_syntax_error(code)
    if err:
        print(f"[FAIL] 生成的 zh-cn.js 语法错误：\n{err}", file=sys.stderr)
        return 2

    summary = (
        f"上游 {stats['upstream']} + 覆盖 {stats['overrides']} → 去重后 {stats['total']} 条"
        f"（其中 {stats['overridden']} 条被本项目改写）"
    )

    if args.check:
        if not OUTPUT.exists():
            print("[FAIL] zh-cn.js 不存在，先跑一次不带 --check 的生成", file=sys.stderr)
            return 1
        if OUTPUT.read_text(encoding="utf-8") != code:
            print("[FAIL] zh-cn.js 与输入不一致，请重新生成", file=sys.stderr)
            return 1
        print(f"[ OK ] zh-cn.js 已是最新：{summary}")
        return 0

    old = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    if old == code:
        print(f"[SKIP] 无变化：{summary}")
        return 0

    OUTPUT.write_text(code, encoding="utf-8")
    print(f"[ OK ] 生成 zh-cn.js  {summary}")
    print(f"       字节数 {len(code.encode('utf-8')):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
