#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FORGE · Label Studio UI 品牌化补丁（可重放 / 幂等 / 可回滚）
============================================================================

为什么需要这个脚本
----------------------------------------------------------------------------
Label Studio CE 是 pip 装在 `D:/forge-ls/venv` 里的，我们要改的两个东西都在
site-packages 内部：

  - Django 模板      venv/Lib/site-packages/label_studio/users/templates/...
  - 登录页静态资源   venv/Lib/site-packages/label_studio/core/static*/...

**直接手工改会在 `pip install -U label-studio` 或重建 venv 时无声丢失。**
所以真正的交付物是「这个脚本 + brand/ 下的资产」，不是被改过的 site-packages。
升级 LS 之后重跑一次 `python apply_brand.py` 就恢复品牌。

改哪几处（都验证过实际渲染）
----------------------------------------------------------------------------
1. 登录页左侧字标    new-ui/user_base.html 内联 SVG，class="ls-logo"
2. 登录页宣传语      "A full-fledged open source solution for data labeling"
3. 登录页 HumanSignal 署名块   <div class="by">Brought to you by ...</div>
4. 登录页品牌样式    <style id="forge-brand">（铜色渐变 / 背景装饰 / 按钮色）
5. 页面标题与 favicon   templates/simple.html + templates/base.html
6. 标注工作台运行时替换   templates/base.html 注入 ls-runtime-brand.js
7. 登录装饰图          core/static/images/login-bg.svg 及 static_build 哈希副本

不做什么
----------------------------------------------------------------------------
不改 Label Studio 的 React bundle（web/dist/apps/labelstudio/*.js，压缩后 4MB、
13 个 chunk）。要改编译产物只能从源码重建，代价远高于收益；第 6 项用 DOM 层
替换覆盖了可见品牌位，且选择器失效时会自动退化为原样，不会破坏界面。

用法
----------------------------------------------------------------------------
    python apply_brand.py                 # 应用（默认自动探测 venv）
    python apply_brand.py --check         # 只检查，不写文件
    python apply_brand.py --revert        # 从 .forge.orig 回滚
    python apply_brand.py --ls-root D:/forge-ls/venv/Lib/site-packages/label_studio

幂等：已打过补丁的段落带哨兵字符串，重复执行不会叠加或报错。
备份：首次修改前把原文件存为 `<文件名>.forge.orig`，之后不再覆盖备份。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BRAND_DIR = Path(__file__).resolve().parent


def zh_counts() -> tuple[int, int]:
    """读两路词库的条数，好让补丁名里的数字不会随词库增长而过时。"""
    def n(name: str) -> int:
        try:
            data = json.loads((BRAND_DIR / name).read_text(encoding="utf-8"))
            return len(data.get("entries", {}))
        except (OSError, ValueError):
            return 0
    return n("zh-cn-upstream.json"), n("zh-cn-overrides.json")
SENTINEL = "forge-brand"
CSS_SENTINEL = 'id="forge-brand"'

# 每个补丁：(名字, 文件相对路径, 匹配模式, 替换内容, 幂等哨兵)
# 文件相对路径相对于 label_studio 包目录。


def _b64(name: str) -> str:
    return base64.b64encode((BRAND_DIR / name).read_bytes()).decode("ascii")


def js_syntax_error(code: str, label: str) -> str | None:
    """用 node --check 真正解析一遍生成的脚本。

    为什么需要：曾因为 `var MARK = ""<svg ...`（占位符多了一对引号）让整段脚本
    SyntaxError。页面照常渲染、控制台不看就发现不了，表现只是「品牌没替换」——
    这种静默失败靠肉眼看模板是查不出来的。有 node 就硬校验，没有就跳过。
    """
    node = shutil.which("node")
    if not node:
        return None
    tmp = Path(tempfile.mkstemp(suffix=".js", prefix=f"forge-{label}-")[1])
    try:
        tmp.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(tmp)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip()[:600]
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def build_patches() -> list[dict]:
    wordmark = (BRAND_DIR / "forge-wordmark.svg").read_text(encoding="utf-8")
    # 取出 <svg ...> 与 </svg> 之间的内容，用我们自己的开标签包一层（补上 class="ls-logo"）
    inner = re.sub(r"^\s*<svg[^>]*>", "", wordmark, count=1)
    inner = re.sub(r"</svg>\s*$", "", inner).strip()
    logo_html = (
        '<svg role="img" aria-label="智途" class="ls-logo" width="176" height="44" '
        'viewBox="0 0 176 44" fill="none" xmlns="http://www.w3.org/2000/svg">\n'
        + inner
        + "\n    </svg>"
    )

    css = (BRAND_DIR / "login.css.fragment").read_text(encoding="utf-8").replace(
        "__FORGE_ORNAMENT__", "data:image/svg+xml;base64," + _b64("login-ornament.svg")
    )
    style_block = (
        '    <!-- FORGE brand overrides，见 platform/labelstudio/brand/login.css.fragment -->\n'
        '    <style id="forge-brand">\n' + css + "\n    </style>\n"
    )

    # 运行时脚本要两份形态：
    #   URI —— data URI，给 <img> 分支兜底
    #   SVG —— 内联 SVG 源码（JSON 编码成 JS 字符串），给 <svg> 分支整体替换元素
    # LS 1.23 实际渲染的是内联 <svg class="lsf-menu-header__logo">，所以 SVG 这份才是主力。
    mark_svg = wordmark.strip()
    if 'class="forge-logo"' not in mark_svg:
        mark_svg = mark_svg.replace("<svg ", '<svg class="forge-logo" ', 1)
    js = (BRAND_DIR / "ls-runtime-brand.js").read_text(encoding="utf-8")
    js = js.replace(
        "__FORGE_WORDMARK_URI__",
        "data:image/svg+xml;base64," + _b64("forge-wordmark.svg"),
    ).replace("__FORGE_WORDMARK_SVG__", json.dumps(mark_svg))
    # 方版标记（空态图标用），同样走 json.dumps 注入
    mark_badge = (BRAND_DIR / "forge-mark.svg").read_text(encoding="utf-8").strip()
    js = js.replace("__FORGE_MARK_SVG__", json.dumps(mark_badge))
    # 曾经踩过：JS 里写成 var MARK = "__PLACEHOLDER__"，而 json.dumps 已经带了引号，
    # 结果渲染成 `var MARK = ""<svg ...` → 整段脚本 SyntaxError，页面看着正常但什么都没换。
    # 这里做一次廉价自检，别让这种错误静默通过。
    if '= ""' in js or "__FORGE_WORDMARK" in js or "__FORGE_MARK_SVG" in js:
        raise SystemExit(
            "运行时脚本占位符替换异常：出现空字符串或仍有占位符残留。"
            "检查 ls-runtime-brand.js 里 MARK / BADGE 是否多余地加了双引号。"
        )
    for _code, _label in ((js, "brand"),):
        _err = js_syntax_error(_code, _label)
        if _err:
            raise SystemExit(f"ls-runtime-brand.js 语法检查失败：\n{_err}")
    # 整页标注工作台外壳：品牌启动态 + 覆盖 LS 原版橙色方块 spinner + 返回平台入口。
    # 平台侧已经改成「点击标注工作台 → 整页导航」，所以 LS 必须自己承担首屏视觉，
    # 不能再露出 Label Studio 的加载画面。
    _mark_png = BRAND_DIR.parents[1] / "web" / "public" / "brand" / "sy-mark-dark.png"
    wb_shell = (BRAND_DIR / "workbench-shell.html.fragment").read_text(encoding="utf-8")
    wb_shell = wb_shell.replace(
        "__FORGE_MARK_B64__",
        base64.b64encode(_mark_png.read_bytes()).decode("ascii"),
    ).replace(
        "__FORGE_PLATFORM_URL__",
        "{{ settings.FORGE_PLATFORM_URL|default:'http://127.0.0.1:9121/' }}",
    )
    # 只查真正的占位符，不要用 `"__FORGE_" in text` —— 片段里还有
    # window.__FORGE_LS_RESTRICTED__ 这种普通全局名，一刀切会误报成未替换。
    _leftover = re.findall(r"__FORGE_(?:MARK_B64|PLATFORM_URL|MARK_SVG|WORDMARK_[A-Z_]+|ORNAMENT)__", wb_shell)
    if _leftover:
        raise SystemExit(f"工作台外壳片段仍有未替换的占位符：{sorted(set(_leftover))}")

    shim = (
        "\n  {# FORGE 运行时品牌替换：见 platform/labelstudio/brand/ls-runtime-brand.js #}\n"
        '  <script nonce="{{request.csp_nonce}}" id="forge-runtime-brand">\n'
        + js
        + "\n  </script>\n"
    )

    # 中文界面层。与品牌脚本分开成两个 <script>，谁坏了都不影响另一个，
    # 也方便单独回滚（删掉这一个 patch 即可）。
    zh = (BRAND_DIR / "zh-cn.js").read_text(encoding="utf-8")
    _err = js_syntax_error(zh, "zh-cn")
    if _err:
        raise SystemExit(f"zh-cn.js 语法检查失败：\n{_err}")
    zh_block = (
        "\n  {# FORGE 中文界面层：见 platform/labelstudio/brand/zh-cn.js #}\n"
        '  <script nonce="{{request.csp_nonce}}" id="forge-zh-cn">\n'
        + zh
        + "\n  </script>\n"
    )

    # 调色板覆盖：只换 11 个 CSS 变量，整个 React 前端的强调色就变铜色，
    # 不需要重编译 bundle。插在 </head> 之前，确保晚于 LS 自己的样式表。
    palette = (BRAND_DIR / "palette.css.fragment").read_text(encoding="utf-8")
    runtime_css = (BRAND_DIR / "runtime.css.fragment").read_text(encoding="utf-8")
    palette_block = (
        '\n  {# FORGE 主色阶覆盖，见 platform/labelstudio/brand/palette.css.fragment #}\n'
        '  <style id="forge-palette">\n' + palette + "\n  </style>\n"
        '\n  {# FORGE 运行时插入元素的样式，见 runtime.css.fragment #}\n'
        '  <style id="forge-runtime-css">\n' + runtime_css + "\n  </style>\n"
    )

    # ---- 标注页 UX 增强层 -------------------------------------------------
    # Label Studio CE 没有插件机制（官方 Plugins 是企业版独占），所以「国内
    # 标注平台那种顺手感」只能靠这一层注入：顶部快捷条、快捷键面板、
    # 提交前校验、复用上一题标签、右侧规范侧栏、单题耗时打点。
    # 它自己判断「是不是标注页」，其它页面不挂载任何东西。
    ux_js = (BRAND_DIR / "ls-ux-enhance.js").read_text(encoding="utf-8")
    _err = js_syntax_error(ux_js, "ux-enhance")
    if _err:
        raise SystemExit(f"ls-ux-enhance.js 语法检查失败：\n{_err}")
    ux_block = (
        "\n  {# FORGE 标注页 UX 增强层：见 platform/labelstudio/brand/ls-ux-enhance.js #}\n"
        '  <script nonce="{{request.csp_nonce}}" id="forge-ux-enhance">\n'
        + ux_js
        + "\n  </script>\n"
    )

    favicon_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#14161A"/>'
        '<image x="4" y="4" width="56" height="56" href="data:image/png;base64,'
        + "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"
        + '"/></svg>'
    )
    favicon_uri = "data:image/svg+xml;base64," + base64.b64encode(
        favicon_svg.encode("utf-8")).decode("ascii")

    title_block_old = (
        "    <title>Label Studio</title>\n"
        "    <link href=\"{{settings.HOSTNAME}}{% static 'images/favicon.ico' %}\" rel=\"shortcut icon\"/>"
    )
    title_block_new = (
        "    <title>智途 · 标注工作台</title>\n"
        "    <link href=\"{{settings.HOSTNAME}}{% static 'images/favicon.ico' %}\" rel=\"shortcut icon\"/>\n"
        f'    <link href="{favicon_uri}" rel="icon" type="image/svg+xml"/>'
    )

    # LS 侧单点登录中间件：把 brand/forge_sso.py 复制进包目录，并把
    # MIDDLEWARE 里挂上。两处都必须能重放，否则升级 LS 之后「免二次登录」会
    # 无声失效，而且症状只是"又弹出登录页"，很难第一时间往补丁上想。
    sso_middleware_patch = (
        "    'jwt_auth.middleware.JWTAuthenticationMiddleware',\n"
        "    # FORGE: 平台账号 → LS 会话（见 core/forge_sso.py）\n"
        "    'core.forge_sso.ForgeSsoMiddleware',  # forge-sso-middleware\n"
        "]"
    )
    no_signup_page = """{% extends 'users/new-ui/user_base.html' %}
<!-- forge-no-signup -->
{% block user_content %}
  <div class="form-wrapper" style="text-align:center;line-height:2">
    <h2>账号由智途平台统一开通</h2>
    <p>标注账号与平台手机号账号同步，无需在这里单独注册。</p>
    <p>请从智途平台的「标注工作台」进入。</p>
  </div>
{% endblock %}
"""

    return [
        # ---- 0a. 关掉自助注册入口 -------------------------------------------
        # 智途平台才是唯一账号源。LS 自带注册页有 Heidi / 原版营销内容，
        # 员工点进去就会以为需要再开一个账号。入口和整页都换成统一开通说明。
        dict(
            name="登录页整块去掉自助注册入口（new-ui）",
            path="users/templates/users/new-ui/user_login.html",
            # 连同 {% if %} 一起换掉：只删链接、留着 if，会在
            # DISABLE_SIGNUP_WITHOUT_LINK=true 时把「账号由平台统一开通」
            # 这句提示也一起藏起来，员工反而看不到任何说明。
            pattern=r'\{% if not settings\.DISABLE_SIGNUP_WITHOUT_LINK %\}\s*'
                    r'<div class="text-wrapper">.*?\{% endif %\}',
            repl="""<!-- forge-no-signup -->
  <div class="text-wrapper">
    <p class="">账号由智途平台统一开通，无需在此注册。</p>
    <p class="">请从平台的「标注工作台」进入。</p>
  </div>""",
            sentinel="forge-no-signup",
        ),
        dict(
            name="注册页 → 平台统一开通说明（new-ui，实际渲染的那套）",
            path="users/templates/users/new-ui/user_signup.html",
            # views.user_signup 在 feature flag 打开时渲染 new-ui 版本；
            # 只补旧模板会留下 Heidi / "How did you hear about Label Studio"
            # 等原版营销字段，用户点注册就看见两套系统没打通。
            pattern=r".*",
            repl=no_signup_page,
            sentinel="forge-no-signup",
        ),
        dict(
            name="注册页 → 平台统一开通说明（legacy 兜底）",
            path="users/templates/users/user_signup.html",
            pattern=r".*",
            repl="""{% extends 'users/new-ui/user_base.html' %}
<!-- forge-no-signup -->
{% block content %}
  {{ block.super }}
  <div style="max-width:420px;margin:48px auto;text-align:center;line-height:1.9">
    <h2>账号由智途平台统一开通</h2>
    <p>标注账号与平台手机号账号同步，无需在这里单独注册。</p>
    <p>请从智途平台进入标注工作台。</p>
  </div>
{% endblock %}
""",
            sentinel="forge-no-signup",
        ),
        # ---- 0b. 单点登录桥 -------------------------------------------------
        dict(
            name="MIDDLEWARE 挂载平台 → LS 单点登录中间件",
            path="core/settings/base.py",
            pattern=r"    'jwt_auth\.middleware\.JWTAuthenticationMiddleware',\s*\]",
            repl=lambda m: sso_middleware_patch,
            sentinel="forge-sso-middleware",
        ),
        # ---- 1. 登录页字标 ----

        dict(
            name="登录页字标 → 智途（内联 SVG）",
            path="users/templates/users/new-ui/user_base.html",
            pattern=r'<svg[^>]*aria-label="Label Studio Logo".*?</svg>',
            repl=logo_html,
            sentinel='aria-label="智途" class="ls-logo"',
        ),
        # ---- 2. 宣传语 ----
        dict(
            name="登录页宣传语 → 中文定位",
            path="users/templates/users/new-ui/user_base.html",
            pattern=r"<h3>A full-fledged open source solution for data labeling</h3>",
            repl="<h3>端到端 AI 数据标注工作台</h3>",
            sentinel="<h3>端到端 AI 数据标注工作台</h3>",
        ),
        # ---- 3. HumanSignal 署名块 ----
        dict(
            name="登录页 HumanSignal 署名 → FORGE 署名",
            path="users/templates/users/new-ui/user_base.html",
            pattern=r'<div class="by">.*?</div>',
            repl='<div class="by forge-by">智途 · AI 数据生产平台</div>',
            sentinel='class="by forge-by"',
        ),
        # ---- 3b. 第三方推广位 ----
        # user_tips.html 是 LS 的营销位：随机展示 4 条 HumanSignal 广告，并且
        # 会 fetch('/heidi-tips') 拉 GitHub 上的实时内容、把 request.server_id
        # 拼进 labelstud.io / humansignal.com 的 URL。这在自有产品里既不该出现，
        # 也是一条对外数据出口，直接摘掉。
        dict(
            name="移除 Label Studio 第三方推广位（heidi tips）",
            path="users/templates/users/new-ui/user_base.html",
            pattern=r"\{% include 'users/new-ui/user_tips\.html' %\}",
            repl=(
                "{# FORGE: 已移除 LS 的第三方推广位 user_tips.html。"
                "它会 fetch /heidi-tips 拉实时营销内容，并把 server_id 带到 labelstud.io / humansignal.com 的 URL 上。 #}"
            ),
            sentinel="FORGE: 已移除 LS 的第三方推广位",
        ),
        # ---- 4. 品牌样式内联 ----
        dict(
            name="登录页品牌样式（铜色渐变 / 装饰图 / 按钮色）",
            path="users/templates/users/new-ui/user_base.html",
            pattern=r"(<link rel=\"stylesheet\" href=\"\{\{ settings\.HOSTNAME \}\}\{% static 'css/login\.css' %\}\"/>\n)",
            repl=lambda m, _s=style_block: m.group(1) + _s,
            sentinel=CSS_SENTINEL,
        ),
        # ---- 5. 页面标题 + favicon（两个模板各一处）----
        dict(
            name="simple.html 标题与 favicon",
            path="templates/simple.html",
            pattern=re.escape(title_block_old),
            repl=title_block_new,
            sentinel="<title>智途 · 标注工作台</title>",
        ),
        dict(
            name="base.html 标题与 favicon（标注工作台）",
            path="templates/base.html",
            pattern=re.escape(title_block_old),
            repl=title_block_new,
            sentinel="<title>智途 · 标注工作台</title>",
        ),
        # ---- 6. 运行时品牌替换脚本 ----
        dict(
            name="base.html 注入 React 运行时品牌替换",
            path="templates/base.html",
            pattern=r"(\{% get_available_languages as LANGUAGES %\}\n)",
            repl=lambda m: m.group(1) + shim,
            sentinel='id="forge-runtime-brand"',
            block=shim,
            block_re=r'[ \t]*\{# FORGE 运行时品牌替换[^\n]*#\}\s*<script[^>]*id="forge-runtime-brand"[^>]*>.*?</script>',
        ),
        # ---- 6b. 主色阶覆盖 ----
        dict(
            name="主色阶 → 工业铜（覆盖 --color-grape-*）",
            path="templates/base.html",
            pattern=r"</head>",
            repl=lambda m, _p=palette_block: _p + m.group(0),
            sentinel='id="forge-palette"',
            block=palette_block,
            block_re=(
                r'[ \t]*\{# FORGE 主色阶覆盖[^\n]*#\}\s*<style[^>]*id="forge-palette"[^>]*>.*?</style>'
                r'\s*\{# FORGE 运行时插入元素的样式[^\n]*#\}\s*<style[^>]*id="forge-runtime-css"[^>]*>.*?</style>'
            ),
        ),
        # ---- 6a. 整页标注工作台外壳 ----
        dict(
            name="标注工作台外壳（品牌启动态 / 加载动画 / 返回平台）",
            path="templates/base.html",
            pattern=r"(<body[^>]*>\n)",
            repl=lambda m, _b=wb_shell: m.group(1) + _b + "\n",
            sentinel='id="forge-wb-shell-css"',
            block=wb_shell,
            block_re=(
                r'[ \t]*\{# FORGE 标注工作台外壳[^\n]*#\}.*?'
                r'<script[^>]*id="forge-wb-shell-js"[^>]*>.*?</script>'
            ),
        ),
        # ---- 6a2. 平台回跳地址 ----
        dict(
            name="平台回跳地址 FORGE_PLATFORM_URL",
            path="core/settings/base.py",
            pattern=r"(FRONTEND_HOSTNAME = get_env\([^\n]*\)\n)",
            repl=lambda m: m.group(1)
            + "\n# FORGE: 整页进入标注工作台后「返回平台」按钮的目标\n"
            + "FORGE_PLATFORM_URL = get_env('FORGE_PLATFORM_URL', 'http://127.0.0.1:9121/')\n",
            sentinel="FORGE_PLATFORM_URL = get_env(",
        ),
        # ---- 6c. 中文界面层 ----
        dict(
            name=f"中文界面层（上游 {zh_counts()[0]} 条 + 本项目 {zh_counts()[1]} 条）",
            path="templates/base.html",
            pattern=r"</head>",
            repl=lambda m, _z=zh_block: _z + m.group(0),
            sentinel='id="forge-zh-cn"',
            block=zh_block,
            block_re=r'[ \t]*\{# FORGE 中文界面层[^\n]*#\}\s*<script[^>]*id="forge-zh-cn"[^>]*>.*?</script>',
        ),
        # ---- 6d. 标注页 UX 增强层 ----
        # 放在中文界面层之后注入：它自己会建 UI，不依赖中文层；
        # 但一个人关掉中文层调试时，这两个功能应该还在。
        dict(
            name="标注页 UX 增强层（快捷条 / 快捷键 / 校验 / 复用标签 / 规范侧栏 / 耗时）",
            path="templates/base.html",
            pattern=r"</head>",
            repl=lambda m, _u=ux_block: _u + m.group(0),
            sentinel='id="forge-ux-enhance"',
            block=ux_block,
            block_re=r'[ \t]*\{# FORGE 标注页 UX 增强层[^\n]*#\}\s*<script[^>]*id="forge-ux-enhance"[^>]*>.*?</script>',
        ),
        # ---- 7. legacy 登录页文案 ----
        dict(
            name="legacy 登录页标题文案",
            path="users/templates/users/user_base.html",
            pattern=r"<h1>Welcome to Label Studio Community Edition \{\{versions\.package\.short_version\}\}</h1>",
            repl="<h1>Welcome to 智途 {{versions.package.short_version}}</h1>",
            sentinel="Welcome to 智途",
        ),
    ] + [
        # ---- 8. 登录表单文案 ----
        # 登录页是 Django 模板直出，不是 React，所以中文界面层（zh-cn.js）覆盖不到它。
        # 这里直接改模板：文案只有 7 处，为它多背 67KB 脚本不划算。
        # 每条都独立幂等（sentinel = 改后的文本），失配就退回英文，不会白屏。
        dict(
            name=f"登录表单中文文案：{new[:34]}",
            path="users/templates/users/new-ui/user_login.html",
            pattern=re.escape(old),
            repl=new,
            sentinel=new,
        )
        for old, new in [
            ("<h2>Log in</h2>", "<h2>登录</h2>"),
            ("<label>Email Address</label>", "<label>邮箱地址</label>"),
            ("<label>Password</label>", "<label>密码</label>"),
            (
                '<label for="persist_session">Keep me logged in this browser</label>',
                '<label for="persist_session">在此浏览器保持登录</label>',
            ),
            (
                '<button type="submit" aria-label="Log In" class="login-button">Log in</button>',
                '<button type="submit" aria-label="登录" class="login-button">登录</button>',
            ),
        ]
    ]


# 静态资源：直接覆盖内容（login-bg.svg 的哈希副本也要覆盖，因为哈希 CSS 引用的是它）
def static_targets(ls_root: Path) -> list[Path]:
    core = ls_root / "core"
    out = []
    for p in [
        core / "static" / "images" / "login-bg.svg",
        core / "static_build" / "images" / "login-bg.svg",
    ]:
        out.append(p)
    sb = core / "static_build" / "images"
    if sb.is_dir():
        out.extend(sorted(sb.glob("login-bg.*.svg")))
    return out


def favicon_targets(ls_root: Path) -> list[Path]:
    """LS 自己的站点图标位图，含 collectstatic 出来的哈希名副本。

    为什么必须连 .ico 一起换：模板里同时给了 <link rel="shortcut icon" .ico>
    和 data: 的 svg，实测 Chrome 只认前者 —— 于是页面上的字全换成"智途"了，
    标签页左上角还挂着 Label Studio 的橙色方块，最扎眼。只改 svg 那份没用。
    """
    out: list[Path] = []
    for base in (ls_root / "core" / "static", ls_root / "core" / "static_build",
                 ls_root / "static"):
        if not base.is_dir():
            continue
        for pat in ("images/favicon*.ico", "favicon*.ico",
                    "images/favicon*.png", "favicon*.png",
                    "images/apple-touch-icon*.png"):
            out.extend(sorted(base.glob(pat)))
    # 去重但保序
    seen, uniq = set(), []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def backup(p: Path) -> Path:
    b = p.with_suffix(p.suffix + ".forge.orig")
    if not b.exists():
        b.write_bytes(p.read_bytes())
    return b


def find_ls_root() -> Path:
    """优先用本机已装 LS 的 venv，其次用当前解释器里的 label_studio。"""
    candidates = [
        Path("D:/forge-ls/venv/Lib/site-packages/label_studio"),
    ]
    try:
        import label_studio  # type: ignore

        candidates.insert(0, Path(label_studio.__file__).resolve().parent)
    except Exception:  # noqa: BLE001
        pass
    for c in candidates:
        if c.is_dir() and (c / "templates" / "simple.html").is_file():
            return c
    raise SystemExit(
        "找不到 Label Studio 安装目录。请用 --ls-root 指定 "
        "<venv>/Lib/site-packages/label_studio"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE Label Studio 品牌补丁")
    ap.add_argument("--ls-root", type=Path, default=None)
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    ap.add_argument("--revert", action="store_true", help="从 .forge.orig 回滚")
    args = ap.parse_args()

    ls_root = args.ls_root or find_ls_root()
    if not (ls_root / "templates" / "simple.html").is_file():
        raise SystemExit(f"--ls-root 不像是 label_studio 包目录: {ls_root}")
    print(f"label_studio = {ls_root}")
    print(f"brand assets = {BRAND_DIR}")
    print("-" * 78)

    if args.revert:
        n = 0
        for p in list(ls_root.rglob("*.forge.orig")):
            target = Path(str(p)[: -len(".forge.orig")])
            target.write_bytes(p.read_bytes())
            print(f"[revert] {target.relative_to(ls_root)}  ← {p.name}")
            n += 1
        # forge_sso.py 是新增文件、没有 .orig 备份，必须单独删掉，
        # 否则回滚后 MIDDLEWARE 也被还原了，这个文件只是变成一堆死代码。
        sso_dst = ls_root / "core" / "forge_sso.py"
        if sso_dst.is_file():
            sso_dst.unlink()
            print(f"[revert] 删除 {sso_dst.relative_to(ls_root)}")
            n += 1
        print(f"\n已回滚 {n} 个文件。重启 Label Studio 生效。")
        return 0

    patches = build_patches()
    changed = skipped = failed = 0

    per_file: dict[Path, str] = {}
    for p in patches:
        f = ls_root / p["path"]
        if not f.is_file():
            print(f"[skip] {p['name']}: 文件不存在 {p['path']}")
            skipped += 1
            continue
        text = per_file.get(f)
        if text is None:
            text = f.read_text(encoding="utf-8")

        # 注入型补丁（<script>/<style> 整块）要能「内容变了就原地换掉」。
        # 只靠 sentinel 存在与否判断是不够的：改词库、调 CSS 之后 sentinel 还在，
        # 就会被当成「已应用」跳过，线上还是旧的。这里先按块正则定位，
        # 内容不一致就原地替换（而不是再走 pattern 插入，否则会出现两份）。
        if p.get("block_re"):
            m = re.search(p["block_re"], text, re.S)
            if m:
                expected = p["block"].strip("\n")
                if m.group(0).strip("\n") == expected:
                    print(f"[ ok ] {p['name']}（已应用，跳过）")
                    skipped += 1
                else:
                    text = text[: m.start()] + expected + text[m.end():]
                    print(f"[sync] {p['name']}：内容已更新，原地刷新")
                    changed += 1
                per_file[f] = text
                continue

        if p["sentinel"] in text:
            print(f"[ ok ] {p['name']}（已应用，跳过）")
            skipped += 1
            per_file[f] = text
            continue

        pat = p["pattern"]
        repl = p["repl"]
        new_text, n = re.subn(
            pat,
            repl if callable(repl) else (lambda m, _r=repl: _r),
            text,
            count=1,
            flags=re.S,
        )
        if n == 0:
            print(f"[FAIL] {p['name']}: 锚点未匹配（LS 版本可能变了）")
            failed += 1
            per_file[f] = text
            continue

        print(f"[match] {p['name']}  ({p['path']})")
        changed += 1
        per_file[f] = new_text

    for f, text in per_file.items():
        cur = f.read_text(encoding="utf-8")
        if text == cur:
            continue
        if args.check:
            print(f"[check] 需要修改: {f.relative_to(ls_root)}")
            continue
        backup(f)
        f.write_text(text, encoding="utf-8")
        print(f"       写入 {f.relative_to(ls_root)}  {sha(cur.encode())} → {sha(text.encode())}")

    # ---- 单点登录中间件源码（整文件复制，不是文本补丁）--------------------
    # 放在 MIDDLEWARE 补丁之后：先保证引用存在，再保证被引用的模块存在。
    # 反过来做的话，两者之间一旦重启 LS 就会 ModuleNotFoundError 起不来。
    sso_src = (BRAND_DIR / "forge_sso.py").read_bytes()
    sso_dst = ls_root / "core" / "forge_sso.py"
    if not sso_dst.is_file():
        print("[new ] core/forge_sso.py（单点登录中间件）")
        if not args.check:
            sso_dst.write_bytes(sso_src)
        changed += 1
    elif sso_dst.read_bytes() != sso_src:
        print(f"[sync] core/forge_sso.py  {sha(sso_dst.read_bytes())} → {sha(sso_src)}")
        if not args.check:
            sso_dst.write_bytes(sso_src)
        changed += 1
    else:
        print("[ ok ] core/forge_sso.py（已是最新）")
        skipped += 1

    # ---- 静态装饰图 ----
    ornament = (BRAND_DIR / "login-ornament.svg").read_bytes()
    for p in static_targets(ls_root):
        if not p.is_file():
            continue
        cur = p.read_bytes()
        if cur == ornament:
            print(f"[ ok ] 装饰图已替换 {p.relative_to(ls_root)}")
            continue
        print(f"[edit] 装饰图 {p.relative_to(ls_root)}  {sha(cur)} → {sha(ornament)}")
        if not args.check:
            backup(p)
            p.write_bytes(ornament)
        changed += 1

    # ---- 站点图标（浏览器标签页上那个）------------------------------------
    ico = (BRAND_DIR / "zhitu-favicon.ico").read_bytes()
    png = (BRAND_DIR / "zhitu-favicon.png").read_bytes()
    targets = favicon_targets(ls_root)
    if not targets:
        print("[warn] 没找到 LS 的 favicon 文件，标签页图标可能还是旧的")
    for f in targets:
        # .ico 就写 ico、.png 就写 png。张冠李戴的话浏览器解码失败，
        # 表现是"图标变成空白"，比留着旧的更难判断。
        blob = ico if f.suffix.lower() == ".ico" else png
        cur = f.read_bytes()
        if cur == blob:
            print(f"[ ok ] 站点图标已替换 {f.relative_to(ls_root)}")
            continue
        print(f"[edit] 站点图标 {f.relative_to(ls_root)}  {sha(cur)} → {sha(blob)}")
        if not args.check:
            backup(f)
            f.write_bytes(blob)
        changed += 1

    print("-" * 78)
    print(f"changed={changed}  skipped={skipped}  failed={failed}  check={args.check}")
    if failed:
        print("\n有补丁锚点未匹配。请对照新的模板文件调整 build_patches() 里的正则，")
        print("不要直接把文件手改掉——那样下次升级还是会丢。")
        return 2
    if changed and not args.check:
        print("\n模板是 Django cached loader 加载的，必须重启 Label Studio 才生效：")
        print("  label-studio start --port 8080 --data-dir D:/forge-ls/data --no-browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
