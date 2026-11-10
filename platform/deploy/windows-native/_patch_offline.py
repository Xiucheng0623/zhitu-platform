# -*- coding: utf-8 -*-
"""离线运行时补丁。规则：全文不用反斜杠（Git Bash 会把反斜杠吃掉）。"""
from pathlib import Path

NL = chr(10)
BASE = Path("C:/Users/G/WorkBuddy/2026-11-05-08-28-26/platform/deploy/windows-native/src")
done = []


def patch(path, old, new, label):
    s = path.read_text(encoding="utf-8")
    if old not in s:
        if new in s:
            print("  [skip] " + label + " 已是新内容")
            return
        raise SystemExit("锚点没找到：" + label)
    path.write_text(s.replace(old, new, 1), encoding="utf-8", newline=NL)
    done.append(label)
    print("  [ok] " + label)


C = BASE / "zn_common.py"

old1 = NL.join([
    "def venv_python(venv_dir: Path) -> Path:",
    '    return venv_dir / "Scripts" / "python.exe"',
    "",
])
new1 = NL.join([
    "def venv_python(venv_dir: Path) -> Path:",
    '    """venv 里的 python.exe，两种形态都要认。',
    "",
    "      * 标准 venv   -> <venv>/Scripts/python.exe",
    "      * 便携运行时  -> <venv>/python.exe（离线包把 Python embeddable 铺开，",
    "        python.exe 与 python313.dll 必须同层才互相找得到，塞进 Scripts 就废了）",
    '    """',
    '    cand = venv_dir / "Scripts" / "python.exe"',
    "    if cand.is_file():",
    "        return cand",
    '    root = venv_dir / "python.exe"',
    "    if root.is_file():",
    "        return root",
    "    return cand",
    "",
])
patch(C, old1, new1, "venv_python 支持便携运行时")

STAGE = NL.join([
    "# ---------------------------------------------------------------------------",
    "# 离线预置运行时（整包交付时用）",
    "# ---------------------------------------------------------------------------",
    "# 解压目录 runtime/ 与安装目录的对应关系。铺好之后，后面每一步都会因为",
    "# 「已就位」而自动跳过下载与 pip —— 服务器全程不需要联网。",
    "RUNTIME_MAP = (",
    '    ("pgsql", "pgsql"),                    # PostgreSQL 17.2 二进制（含 psql）',
    '    ("py-orch", "venv"),                   # 编排层运行时（Python + 依赖）',
    '    ("py-ls", "lsvenv"),                   # 标注引擎运行时（Python + LS 全依赖）',
    '    ("caddy/caddy.exe", "bin/caddy.exe"),  # 入口反代',
    ")",
    "",
    "",
    "def runtime_dir() -> Optional[Path]:",
    '    """离线包预置的运行时目录。没有就返回 None（走在线安装）。"""',
    "    cands = []",
    "    try:",
    "        cands.append(payload_dir() / 'runtime')",
    "    except Exception:  # noqa: BLE001",
    "        pass",
    "    cands.append(Path(sys.executable).parent / 'runtime')",
    "    cands.append(Path(sys.executable).parent / 'payload' / 'runtime')",
    "    for d in cands:",
    "        if (d / 'pgsql' / 'bin' / 'postgres.exe').is_file():",
    "            return d",
    "    return None",
    "",
    "",
    "def _dir_stats(p: Path) -> Tuple[int, int]:",
    "    n = size = 0",
    "    for f in p.rglob('*'):",
    "        if f.is_file():",
    "            n += 1",
    "            try:",
    "                size += f.stat().st_size",
    "            except OSError:",
    "                pass",
    "    return n, size",
    "",
    "",
    "def _stamp_ok(src: Path, dst: Path) -> bool:",
    '    """目标是否已经是这一份（文件数 + 总字节数比对）。',
    "",
    "    不比时间戳：解压/复制会改 mtime，拷完必然旧于源，永远判不过。",
    '    """',
    "    if not dst.is_dir():",
    "        return False",
    "    return _dir_stats(src) == _dir_stats(dst)",
    "",
    "",
    "def stage_runtime(app_dir: Path, log: Callable[[str], None]):",
    '    """把 runtime/ 铺到安装目录。没有预置运行时返回 None（走在线安装）。"""',
    "    src = runtime_dir()",
    "    if src is None:",
    "        return None",
    '    log("检测到离线运行时：%s" % src)',
    "    for rel_src, rel_dst in RUNTIME_MAP:",
    "        s = src / rel_src",
    "        d = app_dir / rel_dst",
    "        if not s.exists():",
    '            return False, ("离线包不完整：缺 runtime/%s" + NL +',
    '                           "重新解压完整的 zip（不要只拷 exe 出来）。" % rel_src)',
    "        if s.is_file():",
    "            if d.is_file() and d.stat().st_size == s.stat().st_size:",
    '                log("  %s 已就位，跳过" % rel_dst)',
    "                continue",
    "            d.parent.mkdir(parents=True, exist_ok=True)",
    "            shutil.copy2(s, d)",
    '            log("  %s -> %s" % (rel_src, rel_dst))',
    "            continue",
    "        if _stamp_ok(s, d):",
    '            log("  %s 已就位，跳过" % rel_dst)',
    "            continue",
    "        n, size = _dir_stats(s)",
    '        log("  铺开 %s（%d 个文件 / %.0f MB）…" % (rel_src, n, size / 1048576))',
    "        d.parent.mkdir(parents=True, exist_ok=True)",
    "        if d.exists():",
    "            shutil.rmtree(d, ignore_errors=True)",
    "        # 同盘直接 move（改目录项，秒级）；跨盘只能拷。装完这份就不需要了。",
    "        try:",
    "            same_drive = s.drive.lower() == app_dir.drive.lower()",
    "        except Exception:  # noqa: BLE001",
    "            same_drive = False",
    "        if same_drive:",
    "            shutil.move(str(s), str(d))",
    "        else:",
    "            shutil.copytree(s, d)",
    '        log("  %s 完成" % rel_dst)',
    '    return True, "离线运行时已铺开（全程不联网、不跑 pip）"',
    "",
    "",
    "def pg_installed(pg_root: Path) -> bool:",
    "",
])
patch(C, "def pg_installed(pg_root: Path) -> bool:" + NL, STAGE, "新增 stage_runtime")

print("改动 %d 处" % len(done))
