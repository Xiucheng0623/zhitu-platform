# -*- coding: utf-8 -*-
"""让 caddy 在离线包里变成可选（没预置就让第 09 步自己去下载）。"""
from pathlib import Path

NL = chr(10)
C = Path("C:/Users/G/WorkBuddy/2026-11-05-08-28-26/platform/deploy/windows-native/src/zn_common.py")

old = NL.join([
    "        if not s.exists():",
    '            return False, ("离线包不完整：缺 runtime/%s" + NL +',
    '                           "重新解压完整的 zip（不要只拷 exe 出来）。" % rel_src)',
])
new = NL.join([
    "        if not s.exists():",
    "            # caddy 允许缺席：它是单文件，第 09 步会自己下（45 MB）。",
    "            # 直接把是否预置 caddy 变成硬阻断，会让整个离线包因为一个可下载",
    "            # 的小组件而完全没法用。",
    '            if rel_src.startswith("caddy/"):',
    '                log("  caddy 未预置，第 09 步会自动下载（约 45 MB）")',
    "                continue",
    '            return False, ("离线包不完整：缺 runtime/%s" + NL +',
    '                           "重新解压完整的 zip（不要只拷 exe 出来）。" % rel_src)',
])
s = C.read_text(encoding="utf-8")
assert old in s, "锚点没找到"
C.write_text(s.replace(old, new, 1), encoding="utf-8", newline=NL)
print("[ok] caddy 改为可选")
