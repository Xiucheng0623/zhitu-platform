# 05 · Label Studio 接入运行手册

本文所有步骤均在 Windows 11 + Python 3.13 上**实测通过**（2026-11-05）。
每一步后面的「坑」都是实际踩到的，照抄本文可避免重复。

---

## 1. 安装（不用 Docker）

Label Studio CE 有官方 pip 包，不依赖 Docker。生产建议用容器（见 deploy/compose），
开发/演示用 pip 版足够，且能在 Windows 上跑。

```bash
# 磁盘紧张时把 venv 和 pip 缓存放非系统盘
mkdir -p D:/forge-ls && cd D:/forge-ls
python -m venv venv
venv/Scripts/python.exe -m pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
venv/Scripts/python.exe -m pip install label-studio \
    -i https://mirrors.aliyun.com/pypi/simple/ --cache-dir D:/forge-ls/pipcache
```

实测：Python 3.13.14 + label-studio 1.23.2 安装成功，耗时约 8 分钟，约 400MB。

---

## 2. 启动

```bash
cd D:/forge-ls
LABEL_STUDIO_USERNAME=forge@forge.local \
LABEL_STUDIO_PASSWORD='<强随机密码>' \
LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK=true \
USE_DEFAULT_BANNED_SUBNETS=false \
venv/Scripts/label-studio.exe start --port 8080 \
    --data-dir D:/forge-ls/data --no-browser --log-level WARNING
```

首次启动会跑 Django 迁移，约 40–60 秒；`curl http://127.0.0.1:8080/health`
返回 `{"status": "UP"}` 即就绪。

> **坑 1：SSRF 防护会拦住自己的回调**
> 1.23 起 LS 默认禁止 webhook 指向保留网段，注册时返回
> `403 URL resolves to a reserved network address (block: 127.0.0.0/8)`。
> 必须 `USE_DEFAULT_BANNED_SUBNETS=false`，并把 `USER_ADDITIONAL_BANNED_SUBNETS`
> 收窄到真正危险的范围（元数据地址 `169.254.0.0/16` 等），而不是全放开。

> **坑 2：数据库目录**
> `--data-dir` 决定 SQLite 与 media 位置。不指定会落到 `%LOCALAPPDATA%\label-studio`，
> 正是 C 盘最容易爆的位置。

---

## 3. 获取 API 凭证（关键）

**LS 1.23 已移除 legacy `Token <key>` 认证**，会返回
`401 Authentication token no longer valid: legacy token authentication has been removed`。
只能 `Authorization: Bearer <JWT>`。

JWT 的规则：**access 只有 5 分钟**,refresh 长期有效。所以集成方必须持有 refresh
并自动续期（orchestrator 的 `LabelStudioClient.access_token()` 已实现）。

获取 refresh 的完整流程（`/api/token/` 需要会话认证，不能只传账号密码）：

```python
import re, requests
base = "http://127.0.0.1:8080"
s = requests.Session(); s.trust_env = False      # 坑 3：别让系统代理插手
s.get(base + "/user/login/")
csrf = s.cookies.get("csrftoken")
s.post(base + "/user/login/",
       data={"email": "forge@forge.local", "password": "<密码>",
             "csrfmiddlewaretoken": csrf},
       headers={"Referer": base + "/user/login/"})

r = s.post(base + "/api/token/", json={},           # 201 -> {"token": "<refresh>"}
           headers={"X-CSRFToken": s.cookies.get("csrftoken"), "Referer": base})
refresh = r.json()["token"]

access = s.post(base + "/api/token/refresh/",
                json={"refresh": refresh}).json()["access"]
# access 用于 Authorization: Bearer <access>
```

把 `refresh` 写进 orchestrator 的 `FORGE_LS_REFRESH_TOKEN`。

> **坑 3：系统代理会把 localhost 请求改写成 404**
> 若机器设了 `HTTP_PROXY`，httpx / requests 默认 `trust_env=True` 会把发往
> 127.0.0.1 的请求交给代理，返回的是代理的 404/502 HTML 页，
> 表现得像「LS 路由写错」。所有内网调用必须 `trust_env=False`
> （orchestrator 用 `FORGE_HTTP_TRUST_ENV=false` 统一控制）。

> **坑 4：分页上限 100**
> `/api/projects/?page_size=500` 返回 **404**（不是 400），极易误判成路径错误。
> 固定 `page_size=100` 并跟随 `next` 翻页。

---

## 4. 创建项目（provisioning）

模板在 `platform/labelstudio/configs/`，共 5 个，分别对应 5 类生产场景：

| 文件 | LS 项目 | 模态 |
|---|---|---|
| 01-bbox-sku.xml | FORGE · 商品图 BBox + SKU 分类 | image |
| 02-polygon-dicom.xml | FORGE · 医学影像多边形分割 | image |
| 03-video-temporal.xml | FORGE · 视频时序边界与安全标签 | video |
| 04-audio-asr.xml | FORGE · 语音转写与说话人分离 | audio |
| 05-text-ner.xml | FORGE · 文本 NER 与意图分类 | text |

```bash
# 本地校验（不需要 LS）：XML 良构 + 控件 toName 指向 + 选项非空
python labelstudio/provision.py --dry-run

# 真实创建（幂等：按 title 匹配，已存在则只更新 label_config）
python labelstudio/provision.py \
    --ls-url http://127.0.0.1:8080 \
    --refresh "<REFRESH_TOKEN>" \
    --webhook-url http://127.0.0.1:8300/api/v1/webhooks/labelstudio
```

幂等性由 LS 自身的 `label_config` 校验兜底：非法 XML 会返回 400 并指出具体节点，
不需要我们另写 schema 校验器。

---

## 5. Webhook 回流

LS → orchestrator：`POST /api/v1/webhooks/labelstudio`

- 事件：`ANNOTATION_CREATED` / `ANNOTATION_UPDATED`
- 服务端按 `ls_project_id` 定位任务，`assigned|in_progress → submitted`，并写审计
  （`action = ls.annotation`）
- 校验头 `X-Forge-Secret`（对应 `FORGE_LS_WEBHOOK_SECRET`），不匹配返回 404
- LS 无终端用户身份，该路径使用服务主体 `svc_labelstudio`

端到端验证（真项目、真任务、真标注、真回调）：

```bash
cd services/orchestrator
python tests/e2e_labelstudio.py --refresh "<REFRESH_TOKEN>"
```

实测结果：**9/9 通过**——LS 内创建标注后，数据库任务状态在 1–2 秒内自动变为
`submitted`，审计表出现 `ls.annotation` 记录。

---

## 6. 前端嵌入

标注工作台（`web/src/pages/Annotate.tsx`）只嵌 LS 的**标注流**，其余交互全是我们自己的：

```
VITE_LS_EMBED=1
VITE_LS_BASE=http://127.0.0.1:8080
```

嵌入地址为 **`/projects/{id}/data`**。

> **坑 5：不是 `/label`**
> LS 1.23 的 React 路由是 `/projects/{id}/data`（标注流）。
> 写成 `/label` 会被 SPA 的 catch-all 接住并渲染 NotFound，看起来像 404 页面，
> 但服务端其实返回的是 200 的 index.html —— 极易误判成服务没起来。

未配置或任务没有 `ls_project_id` 时，页面显示「未连接 Label Studio」，
**不会用自绘假画布冒充**。

### 单点登录的限制（必须知情）

Label Studio **CE 版没有 OIDC/SAML**（那是 Enterprise 功能）。因此 iframe 内
首次访问会停在 LS 自己的登录页，登录一次后由长期 session cookie 维持。

三条可选路径：

| 方案 | 做法 | 代价 |
|---|---|---|
| A（当前） | 用户首次在 iframe 内登录一次，session 保持 | 有一次摩擦，零开发 |
| B | 反向代理 + 同源 Cookie 注入 | LS 不支持子路径托管（静态资源用绝对路径），要改前端 base |
| C | Fork LS 前端，注入我们的 JWT | 彻底无缝，但每次 LS 升级要重新合并 |

当前采用 A。若客户明确要求无缝，再评估 C。

---

## 7. 升级注意

| 项 | 影响 |
|---|---|
| `SECRET_KEY` | 轮换后所有 JWT 失效，需重新取 refresh 并更新 `FORGE_LS_REFRESH_TOKEN` |
| `label_config` 的对象名（`<Label value=...>`） | **只能追加不能重命名**，改名会让历史标注对不上 |
| SSRF 默认值 | 每个大版本都可能收紧，升级后务必重测 webhook 与媒体预览 |
| 分页上限 | 同上，重测 provisioning |

---

## 8. UI 品牌化：把 Label Studio 变成 FORGE

### 8.1 调用方式（回顾）

平台与 LS 之间只有一条通道：**官方 REST + JWT**。不碰 LS 的数据库、不复制标注结果。

| 动作 | 接口 | 备注 |
|---|---|---|
| 探活 | `GET /api/current-user/whoami` | 兼作 token 有效性检查 |
| 列项目 | `GET /api/projects/?page_size=100` | **上限 100**，翻页跟 `next` |
| 建项目 | `POST /api/projects/` | `label_config` 由 LS 官方校验 |
| 导入 | `POST /api/projects/{id}/import` | LS 原生 `[{"data":{...}}]` |
| 统计 | `GET /api/projects/{id}/stats` | |
| 注册回调 | `GET/POST /api/webhooks/` | 幂等：同 URL 复用 |
| 标注界面 | iframe → `/projects/{id}/data` | 相对路径 |

`clients.py` 里 `trust_env=False`（绕开系统代理）、`access_token()` 用 refresh 自动续期
（access 只有 5 分钟）、`_req()` 遇 401 强刷一次。这些是实现细节，见源码注释。

### 8.2 品牌补丁机制（重要）

pip 装的 LS，模板和静态资源都在 `venv/Lib/site-packages/label_studio/` 里：

```
users/templates/users/new-ui/user_base.html   ← 登录页（字标/宣传语/署名/样式）
users/templates/users/new-ui/user_login.html  ← 登录表单文案（Django 直出，见 8.10）
templates/simple.html                          ← 页面标题 + favicon
templates/base.html                            ← 标注工作台外壳（注入脚本、调色板、中文层）
core/static/images/login-bg.svg                ← 登录页装饰（含 static_build 哈希副本）
```

**手改这些文件，`pip install -U label-studio` 或重建 venv 时会静默丢失。**
所以真实交付物是「补丁脚本 + 资产」，不是被改过的 site-packages：

```
platform/labelstudio/brand/
├── apply_brand.py            幂等补丁器（--check / --revert），18 个补丁位
├── verify_brand.py           带登录态的无头浏览器自检 + 截图（标准库 CDP）
├── scan_zh_coverage.py       遍历 8 个页面，列出仍是英文的 UI 文案
├── build_zh_dict.py          合并两路词库 → 生成 zh-cn.js
├── zh-cn.template.js         中文层执行器（安全边界在这里）
├── zh-cn-upstream.json       社区补丁词库 897 条
├── zh-cn-overrides.json      本项目词条 177 条（覆盖优先）
├── forge-wordmark.svg        横版字标（采样框 + 负形 F，currentColor）
├── forge-mark.svg            方版标记（空态/菜单用）
├── login-ornament.svg        登录页右下角装饰
├── login.css.fragment        登录页品牌覆盖（内联进模板）
├── palette.css.fragment      主色阶覆盖（--color-grape-* → 铜）
├── runtime.css.fragment      运行时插入节点的样式
└── ls-runtime-brand.js       标注工作台的 DOM 层品牌替换
```

```bash
python build_zh_dict.py           # 改完词库先重新生成
python apply_brand.py --check     # 只报告锚点是否匹配得上
python apply_brand.py             # 应用（首次会把原文件存成 *.forge.orig）
python apply_brand.py --revert    # 回滚
python verify_brand.py --page login
python verify_brand.py --page project --project 1   # 顺带点开抽屉截第二张图
python scan_zh_coverage.py        # 还剩哪些英文
```

> **补丁器除了 sentinel 还有「原地刷新」**：注入型补丁（`<script>`/`<style>` 整块）
> 靠 sentinel 判断「已应用」是不够的 —— 改了词库或 CSS 之后 sentinel 还在，
> 就会被当成已完成跳过，线上仍是旧的。所以这类补丁带 `block_re`/`block`：
> 先按块正则定位，内容不一致就原地替换（而不是再走 pattern 插入，否则会出现两份）。

改完**必须重启 LS**：`DEBUG=False` 时 Django 用 cached template loader，模板在内存里。

### 8.3 为什么要内联样式而不是改 CSS 文件

Django 用 `ManifestStaticFilesStorage`，模板里 `{% static 'css/login.css' %}`
会被改写成 `login.f115716e9a7c.css`（带内容哈希）。改源文件不生效，
改哈希文件在重装后又消失。**模板是运行时读取的，内联 `<style>` 最稳。**

同理 favicon：不去替换 `images/favicon.ico` 的二进制，而是在模板里追加一条
`<link rel="icon" type="image/svg+xml" href="data:...">`，现代浏览器取最后一条。

### 8.4 标注工作台的品牌替换

React 前端是编译好的 bundle（13 个 chunk，压缩后 ~4MB），**不重编译**。
两条杠杆：

**① 主色阶（覆盖面最大）**
前端所有强调色都来自 `--color-grape-000…950`，语义变量再去引用它。
只替换 11 个值，按钮 / 链接 / 选中态 / 焦点环 / 进度条全部变铜色，零重编译。
见 `palette.css.fragment`。

**② 字标（DOM 替换）**
`ls-runtime-brand.js` 在 `base.html` 里注入，用 MutationObserver 盯
`svg[alt="Label Studio Logo"], svg.lsf-menu-header__logo`，命中就整体替换元素。
找不到就什么都不做 —— 版本升级后选择器失效只会退回原样，不会白屏。

**③ 品牌元素（脚本插入）**

| 位置 | 内容 | 选择器锚点 |
|---|---|---|
| 头部 | FORGE 横版字标（替换 LS 内联 SVG） | `svg.lsf-menu-header__logo` |
| 数据管理空态 | 方版标记（替换 LS 的上传箭头图标） | `div.bg-primary-emphasis.rounded-full` |
| 右侧区域面板空态 | 「FORGE 标注工作台」铜色署名 | `[data-testid="empty-state-header"]` |
| 左侧抽屉菜单底部 | 方版标记 + `FORGE` + `标注引擎 vX.Y.Z` | `li.lsf-main-menu__spacer` 之后 |
| 登录页 | 字标 + 「端到端 AI 数据标注工作台」+ FORGE 署名 | `user_base.html` 模板 |

菜单品牌块的版本号**从 DOM 里那条 `<a class="lsf-current-version">v1.23.2</a>` 读**，
不硬编码 —— 升级 LS 后这行不会说谎。该 `<a>` 本身用 CSS 隐藏掉，否则两行版本号并排重复。
它 `display:none` 但节点仍在，脚本才读得到；直接删节点会导致每次菜单重渲染后取不到版本。

> **坑 11：外链只按域名过滤会漏掉 GitHub**
> 早期 `EXT_RE = /labelstud\.io|humansignal\.com/`，侧栏里那条「GitHub」指向
> `github.com/HumanSignal/label-studio` —— 两个域名都不匹配，一直留着。
> 现在正则一起管 `github.com/(HumanSignal|heartexlabs)/label[- ]?studio`。

> **坑 12：菜单关闭时根本不渲染，所以「外链已清零」曾经是假绿灯**
> `ul.lsf-main-menu` 只在抽屉打开时存在。`verify_brand.py` 早期在关闭状态下统计
> `a[href*=labelstud.io]`，**永远得到 0** —— 断言通过，而菜单里那三条外链一直都在。
> 现在自检会先点开抽屉再统计 `extLinks`，并额外断言 `.forge-menu-brand` 存在。
> **教训：断言的对象必须真的在 DOM 里，否则测的是空集。**

### 8.5 三个必须知道的坑（登录页）

> **坑 6：头部 logo 是内联 `<svg>`，不是 `<img>`**
> 一开始按 `img[alt="Label Studio Logo"]` 找，命中数恒为 0：脚本没报错，
> 页面看着正常，其实什么也没换。必须整体替换元素（`parentNode.replaceChild`），
> 而不是改 `src`。

> **坑 7：`var MARK = "__PLACEHOLDER__"` 会被 `json.dumps` 打成双引号**
> Python 端用 `json.dumps(svg)` 注入时本身已带引号，JS 里再写一对就变成
> `var MARK = ""<svg ...`，整段脚本 SyntaxError。**页面看起来完全正常，只是没生效。**
> `apply_brand.py` 现在有 `= ""` 自检兜底。

### 8.5 三个必须知道的坑（登录页）

> **坑 8：`.ls-logo` 被写死成 414×64**
> 原字标是 194×30（比例 6.5:1），我们的字标是 176×44（4:1）。沿用固定宽高会留大片空白，
> 要改成 `width:auto; height:72px`。

> **坑 9：复选框改 `accent-color` 是打空靶**
> `<input type="checkbox">` 被 LS 设成 `display:none`，方框是
> `.form-group label::before` 用 `padding:10px` 画的，勾选态由
> `.form-group input:checked + label::before { background: var(--primary_link) }` 上色。
> 改 input 的任何属性都无效 —— 必须改 label 伪元素和 `--primary_link`。

> **坑 10：登录页藏了一条第三方数据出口**
> `user_tips.html` 会随机展示 4 条 HumanSignal 广告，并 `fetch('/heidi-tips')`
> 拉 GitHub 上的实时内容，把 `server_id` 拼进 `labelstud.io` / `humansignal.com` 的 URL。
> 补丁已把它整段摘掉。**升级后要重新确认它没回来。**

### 8.6 中文界面：没有，且没有可挂的 i18n 基础设施

实测结论（LS 1.23.2 CE）：

| 检查项 | 结果 |
|---|---|
| `label_studio/` 包内 `.po` / `.mo` | **0 个** |
| 前端 bundle 的 i18n 框架 | **0 处** `i18next` / `react-intl` / `useTranslation` |
| Django `LANGUAGE_CODE` / `USE_I18N` | `en-us` / `False` |
| `LANGUAGES` 列表 | 未定义（只有 en-us） |
| 界面里的语言切换入口 | 无 |
| `LOCALE` 类环境变量 | 无 |

所以不是"藏起来了要开个开关"，而是**上游根本没做国际化**。
要中文只有三条路：

| 方案 | 做法 | 代价 | 状态 |
|---|---|---|---|
| A 只汉化外壳 | iframe 外面套我们的中文页头/指引/状态，LS 内部保持英文 | 零风险 | 做过，已升级 |
| **B 关键文案 DOM 替换** | 词典 + MutationObserver，运行时替换按钮/空态/选项卡文案 | 中；LS 改文案就失配 | **当前采用，938 条** |
| C Fork 前端重编译 | 从源码接入 i18n 框架并全量翻译 | 高；每次升级重新合并 | 不推荐 |

> 反面参考：网上流传的「改 `web/apps/labelstudio/src/themes/default/variables.styl`
> 换主题」教程，走的是源码路线 —— pip 包里既没有 `src/` 也没有 node 工具链，
> 改不了。真正的杠杆是 8.4 的 CSS 变量覆盖。

### 8.7 中文层的词库从哪来：借词库，不借执行器

社区有一个中文补丁 [haoxiangdong/label-studio-zh-cn](https://github.com/haoxiangdong/label-studio-zh-cn)，
目标版本 LS 1.23.0，`zh-cn-ui.js` 84,889 字节，**897 条**整串词典 + 47 条正则。

> **取不到它的原因与解决办法**（记下来省下一次摸索）：
> `raw.githubusercontent.com` 在本机被阻断，`curl` 一律 `http=000`，换代理也不行。
> 但 **jsDelivr 通**：
> ```bash
> curl -sL -o zh-cn-ui.js \
>   "https://gcore.jsdelivr.net/gh/haoxiangdong/label-studio-zh-cn@main/zh-cn-ui.js"
> # 备用：https://ghproxy.net/https://raw.githubusercontent.com/<owner>/<repo>/main/<file>
> ```
> `cdn.jsdelivr.net` 反而 000，`gcore.jsdelivr.net` 200 —— 同一家 CDN 不同节点也要试。

**为什么只借词库、不借执行器**：它的 `shouldSkip` 只挡 `script/style/noscript/textarea/code/pre/svg`，
**不挡 `.lsf-richtext` / `.lsf-html` / `.lsf-object`**，而那是渲染出来的**任务正文**。
它还以 `applyFragmentReplacements` 作兜底，对每条未命中的文本跑
`Upload/Text/Tools/Docs/Info/History/Manual/Images…` 的子串包含替换。实测后果：

```
任务原文 "History of Rome"   →  "历史 of Rome"     ← 数据被改写
任务原文 "All of the above"  →  "全部 of the above"
```

在标注平台上这是**数据污染**，不是审美问题。另外它没有幂等标记，每轮 mutation 都
`querySelectorAll("*")` 重扫整棵 body，`replaceElementTextNodes` 还会把同元素内
多余文本节点清空（`/\S+/g → ""`）—— 在多 span 按钮上可能破坏布局。

所以我们保留自己的执行器（`zh-cn.template.js`：SKIP_SELECTOR + 按内容值幂等 + 60ms 节流），
只把它的**译文**搬过来。合并后 919 → 938 条。

### 8.8 词库流水线（数据 + 生成器，别手改成品）

```
zh-cn-upstream.json   897 条  上游词库（_meta 里记了出处、字节数、抓取方式）
zh-cn-overrides.json  177 条  本项目词条，覆盖优先级更高
         ↓  build_zh_dict.py（合并 + node --check）
zh-cn.template.js             执行器 + 48 条正则，词库位置留占位符
         ↓
zh-cn.js              938 条  产物
         ↓  apply_brand.py 内联进 templates/base.html
```

```bash
python build_zh_dict.py            # 生成
python build_zh_dict.py --check    # CI：产物与输入是否一致
python apply_brand.py              # 打补丁（注入块内容变了会原地刷新）
# 重启 Label Studio
```

三条维护约定：

1. **词条只写进 JSON，不手改 `zh-cn.js`** —— 它会被下一次生成覆盖。
2. **上游更新**只需替换 `zh-cn-upstream.json` 再跑一次生成器。
3. 覆盖层里优先放两类东西：
   - 我们更贴合标注场景的译法（`Info → 详情` 而非「信息」，`Auto → 跟随系统` 而非「自动」）
   - **品牌位改写**：上游把 "Label Studio" 译成「Label Studio 标注平台」，
     我们覆盖成「FORGE 标注平台」；版本号统称「FORGE 标注引擎版本」。

冲突处理（135 条重合里 27 条译文不同）不是一刀切：`Order by`、`Comments`、`Compare All`
这类上游更准的，我们**主动让步**（记在 `_meta.concede_to_upstream`）；
`Info`、`Auto`、`Relations`、`Webhooks` 这类上游译得不准或不适合窄标签页的，保留我们的。

### 8.9 覆盖度靠扫描，不靠目测

```bash
python scan_zh_coverage.py                 # 8 个页面：项目列表/数据管理/5 个设置页/账号
python scan_zh_coverage.py --json D:/gaps.json
```

它登录后逐页遍历，抓出**仍然是英文的可见文本**和 `placeholder/title/aria-label`，
按出现次数排序，并分四类：

| 分类 | 含义 | 处理 |
|---|---|---|
| `gap` | 真缺口 | 补进 `zh-cn-overrides.json` |
| `done` | 已中文化（含夹了 ctrl/Shift 的中文） | 忽略 |
| `data` | 业务数据：项目名、标签名、邮箱、头像缩写、标识符 | **绝不翻译** |
| `tag` | 标注配置的 tag 名（Text/Audio/Choices…） | 保留英文，与上游一致 |

判据演进过程值得记一笔：初版只判「含 ≥2 个连续拉丁字母」，报出 52 条；
加「译文白名单」（**这段文字本身就是词库里的某个 value → 当然翻过了**）+
「中文占比 ≥15%」两条之后收敛到 **1 条**。剩下那条是
`Use` / `image` / `from` 被 LS 拆成三个文本节点的一句话，跨节点无法整句匹配，属已知限制。

> 扫描器的 `SKIP_SELECTOR` 必须和 `zh-cn.template.js` 里的一致。
> 不一致就是自欺欺人：扫描器报「这里没翻译」，实际是设计上不翻译。

**已知限制（两条，都是主动取舍）**

1. **数据管理表格里的日期列仍是英文**（`Nov 05 2026. 11:19:40`）。
   日期和数据格同属 `.lsf-table__cell`，而任务正文字段列也是这个 class —— 见坑 13。
   为保住任务正文不被改写，数据格整体跳过，日期本地化只能放弃。
   硬要做的话得按列索引区分字段列，依赖表头与数据行的列对齐假设，不值得。
2. **跨文本节点的句子无法翻译**。LS 把
   `Use [image] from [...]` 拆成三个节点（中间夹着 `<code>`），整句正则匹配不到。
   逐片翻会得到「使用 图像 从」这种破碎语序，比留英文更糟。

> **坑 13：`.lsf-table__cell` 同时是表头和数据格**
> 最初把整个 class 加进 `SKIP_SELECTOR`（理由是「表格里的任务文本列」），
> 结果列头 `Completed` / `Annotated by` 全部没翻译 —— 而这两个词在上游词库里一直有。
> 正解是 `.lsf-table__cell:not(.lsf-th)`：表头（`.lsf-th`）放行，数据格继续保护。
> `verify_brand.py` 现在有 `enHeaders` 断言，列头一旦退回英文就会 FAIL。

### 8.10 登录页为什么要单独改模板

登录页是 **Django 模板直出**（`users/new-ui/user_login.html`），不是 React，
所以 `zh-cn.js`（注入在 `templates/base.html`）碰不到它 —— 实测该页 `zhEntry = 0`。
文案只有 7 处（标题 / 邮箱 / 密码 / 保持登录 / 按钮 / 还没有账号 / 注册），
直接改模板比为一个首屏多背 67KB 脚本划算。7 条补丁各自幂等，失配退回英文。
