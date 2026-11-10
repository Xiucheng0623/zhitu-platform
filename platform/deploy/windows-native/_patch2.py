# -*- coding: utf-8 -*-
"""补丁2：step_python / step_orchestrator / step_labelstudio / step_tasks 支持离线预置。"""
from pathlib import Path

NL = chr(10)
S = Path("C:/Users/G/WorkBuddy/2026-11-05-08-28-26/platform/deploy/windows-native/src/zn_setup.py")
done = []


def patch(old, new, label):
    s = S.read_text(encoding="utf-8")
    if old not in s:
        if new in s:
            print("  [skip] " + label)
            return
        raise SystemExit("锚点没找到：" + label)
    S.write_text(s.replace(old, new, 1), encoding="utf-8", newline=NL)
    done.append(label)
    print("  [ok] " + label)


# 02 Python：离线包直接铺开预置运行时
old = NL.join([
    "    def step_python(self) -> Tuple[bool, str]:",
    "        # 区间限定 3.11–3.13：LS 1.23 的依赖在新 Python 上常没有预编译 wheel",
    "        self.py, self.py_ver = C.find_python((3, 11), (3, 13))",
])
new = NL.join([
    "    def step_python(self) -> Tuple[bool, str]:",
    "        # 离线整包：runtime/ 里已经带好 Python 与全部依赖，铺开即可，",
    "        # 既不找系统 Python，也不联网。铺开之后后面的 PG / 依赖 / caddy",
    "        # 每一步都会因为「已就位」自动跳过下载与 pip。",
    "        staged = C.stage_runtime(self.app_dir, self.log)",
    "        if staged is not None:",
    "            ok, msg = staged",
    "            if not ok:",
    "                return False, msg",
    "            self.py = str(C.venv_python(self.venv_orch))",
    "            self.py_ver = '3.13'",
    "            self.log('Python：%s（离线预置，无需安装）' % self.py)",
    "            return True, msg",
    "",
    "        # 在线安装：找系统 Python，区间限定 3.11–3.13",
    "        self.py, self.py_ver = C.find_python((3, 11), (3, 13))",
])
patch(old, new, "step_python 优先用离线运行时")

# 04 编排层：预置依赖已就绪则跳过 pip
old = NL.join([
    "        if not req.is_file():",
    '            return False, "载荷里缺编排层源码：%s" % req',
    "        ok, msg = C.venv_create(self.py, self.venv_orch)",
])
new = NL.join([
    "        if not req.is_file():",
    '            return False, "载荷里缺编排层源码：%s" % req',
    "        # 离线包把依赖预装进了运行时；服务器可能根本没网，别再 pip 一遍",
    '        if (self.venv_orch / "Lib" / "site-packages" / "fastapi").is_dir():',
    "            self.log('编排层依赖已在预置运行时里，跳过 pip')",
    '            return True, "编排层运行时已就绪（离线预置）"',
    "        ok, msg = C.venv_create(self.py, self.venv_orch)",
])
patch(old, new, "step_orchestrator 跳过 pip")

# 06 LS：预置依赖已就绪则跳过 pip（判 site-packages，不判 Scripts/label-studio.exe）
old = NL.join([
    "    def step_labelstudio(self) -> Tuple[bool, str]:",
    "        ok, msg = C.venv_create(self.py, self.venv_ls)",
])
new = NL.join([
    "    def step_labelstudio(self) -> Tuple[bool, str]:",
    "        # 离线包里 LS 的全套依赖已经躺在运行时中。判 site-packages 而不是",
    "        # Scripts/label-studio.exe —— 便携运行时没有 console_scripts 的 exe。",
    '        if (self.venv_ls / "Lib" / "site-packages" / "label_studio").is_dir():',
    "            self.log('Label Studio %s 已在预置运行时里，跳过 pip' % LS_VERSION)",
    '            return True, "Label Studio %s 已就绪（离线预置）" % LS_VERSION',
    "        ok, msg = C.venv_create(self.py, self.venv_ls)",
])
patch(old, new, "step_labelstudio 跳过 pip")

# 10 计划任务：LS 启动命令两种形态
old = NL.join([
    '        ls_exe = self.venv_ls / "Scripts" / "label-studio.exe"',
    "        ls_cmd = self._launcher(",
    '            "labelstudio", ls_env, ls_exe.parent,',
    '            \'"%s" start --port %d --data-dir "%s" --no-browser\'',
    "            % (ls_exe, C.DEFAULT_LS_PORT, self.ls_data))",
    '        ok, msg = C.task_register(C.TASKS["ls"], ls_cmd, ls_exe.parent, "智途标注引擎")',
])
new = NL.join([
    "        # 标注入口：标准 venv 用 Scripts/label-studio.exe；离线便携运行时没有",
    "        # console_scripts 生成的 exe，用 start_ls.py 转发同一个 main()，参数",
    "        # 结构完全一致（argv[0] 是脚本名，后面还是 start --port …）。",
    '        ls_exe = self.venv_ls / "Scripts" / "label-studio.exe"',
    "        if ls_exe.is_file():",
    "            ls_home = ls_exe.parent",
    '            ls_head = \'"%s"\' % ls_exe',
    "        else:",
    "            ls_home = self.venv_ls",
    '            ls_head = \'"%s" "%s"\' % (C.venv_python(self.venv_ls),',
    '                                       self.venv_ls / "start_ls.py")',
    "        ls_cmd = self._launcher(",
    '            "labelstudio", ls_env, ls_home,',
    '            \'%s start --port %d --data-dir "%s" --no-browser\'',
    "            % (ls_head, C.DEFAULT_LS_PORT, self.ls_data))",
    '        ok, msg = C.task_register(C.TASKS["ls"], ls_cmd, ls_home, "智途标注引擎")',
])
patch(old, new, "step_tasks 支持便携运行时启动 LS")

print("改动 %d 处" % len(done))
