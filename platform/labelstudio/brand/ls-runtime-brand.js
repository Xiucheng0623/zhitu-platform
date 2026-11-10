/* ============================================================================
   FORGE · Label Studio React 前端运行时品牌替换
   ----------------------------------------------------------------------------
   标注工作台（/projects/{id}/data）是 Label Studio 编译好的 React bundle
   （web/dist/apps/labelstudio/*.js，13 个 chunk）。我们不重编译它，只做四件事：

     1. 换字标            头部 <svg class="lsf-menu-header__logo">
     2. 清外链            菜单与页面里指向 labelstud.io / humansignal.com / 官方 GitHub 仓库的入口
     3. 补品牌元素        数据管理空态图标、右侧面板署名、左侧抽屉菜单底部品牌块
     4. 换标题            把 <title> 里的 Label Studio 换掉

   真实 DOM（2026-11 实测 LS 1.23.2，别凭猜）
   ----------------------------------------------------------------------------
   · 头部字标是**内联 SVG 不是 <img>**：
       <div class="lsf-menu-header__trigger main-menu-trigger">
         <svg width="194" height="30" class="lsf-menu-header__logo"
              alt="Label Studio Logo"> … </svg>
         <button …>汉堡</button>
       </div>
     之前按 img[alt=...] 找，命中数恒为 0 —— 脚本不报错、页面正常，
     其实什么都没换。所以 svg 分支必须整体替换元素。

   · 外链出现在三处：
       ul.lsf-main-menu a.lsf-main-menu__item[href*=labelstud.io]  (API / Docs / Slack)
       ul.lsf-main-menu a[href*=github.com/HumanSignal]            ("GitHub" 菜单项)
       右侧面板与空态里的 a[href*=labelstud.io]                    ("Learn more")
     第三处最早漏了：只按 labelstud.io / humansignal.com 过滤域名，
     github.com/HumanSignal/label-studio 两条都不匹配，菜单里就留着一条
     「GitHub」指向 LS 官方仓库。现在 EXT_RE 一起管。

   · 空态结构：
       数据管理  div.bg-primary-emphasis.rounded-full > svg(40×40 上传箭头)
       右侧面板  [data-testid="empty-state-header"]

   设计原则：找不到就什么都不做。版本升级后选择器失效只会退回 Label Studio 原样，
   不会白屏、不会抛错、不会挡住任何操作。

   占位符（由 apply_brand.py 替换，MARK 不带引号——json.dumps 已带）：
     __FORGE_WORDMARK_SVG__  横版字标全文
     __FORGE_MARK_SVG__      方版标记全文
     __FORGE_WORDMARK_URI__  横版字标的 base64 data URI（给 <img> 兜底）
   ============================================================================ */
(function () {
  "use strict";

  var BRAND = "智途";
  var SRC_TEXT = "Label Studio";
  var MARK = __FORGE_WORDMARK_SVG__;
  var BADGE = __FORGE_MARK_SVG__;
  var URI = "__FORGE_WORDMARK_URI__";

  var LOGO_SEL = [
    'img[alt="' + SRC_TEXT + ' Logo"]',
    'svg[alt="' + SRC_TEXT + ' Logo"]',
    "svg.lsf-menu-header__logo"
  ].join(",");
  // 官方站 + 官方仓库。GitHub 那条是侧栏里 "GitHub" 菜单项，
  // 指向 github.com/HumanSignal/label-studio —— 它不前缀 labelstud.io，
  // 早期只按域名过滤时漏掉了，菜单里还留着一条指向 LS 官方仓库的入口。
  var EXT_RE = /labelstud\.io|humansignal\.com|github\.com\/(HumanSignal|heartexlabs)\/label[\s-]?studio/i;

  var lastRun = 0;
  var queued = false;

  // ---- 1. 字标 -------------------------------------------------------------
  function replaceSvgLogo(el) {
    var holder = document.createElement("span");
    holder.className = "forge-brand-logo";
    holder.setAttribute("data-forge-skip", "1");
    holder.innerHTML = MARK;
    var svg = holder.firstElementChild;
    if (!svg) return;
    svg.style.setProperty("height", "26px", "important");
    svg.style.setProperty("width", "auto", "important");
    svg.style.setProperty("display", "block", "important");
    el.parentNode.replaceChild(holder, el);
  }

  function replaceImgLogo(el) {
    el.setAttribute("src", URI);
    el.setAttribute("alt", BRAND);
    el.style.setProperty("height", "26px", "important");
    el.style.setProperty("width", "auto", "important");
  }

  function swapLogos() {
    var nodes = document.querySelectorAll(LOGO_SEL);
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.getAttribute("data-forge") === "1") continue;
      el.setAttribute("data-forge", "1");
      try {
        if (el.tagName.toLowerCase() === "img") replaceImgLogo(el);
        else replaceSvgLogo(el);
      } catch (e) { /* 单点失败不影响其它 */ }
    }
  }

  // ---- 2. 清掉指向 Label Studio 官网的外链 ---------------------------------
  // 菜单里是 <li><a class="lsf-main-menu__item" href="https://labelstud.io/...">
  // 整个 <li> 连图标一起摘掉；页面内的 "Learn more" 只摘链接本身。
  function dropExternalLinks() {
    var links = document.querySelectorAll(
      'a[href*="labelstud.io"], a[href*="humansignal"], a[href*="github.com/HumanSignal"]'
    );
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (a.getAttribute("data-forge") === "1") continue;
      if (!EXT_RE.test(a.getAttribute("href") || "")) continue;
      a.setAttribute("data-forge", "1");
      var menuItem = a.closest("li");
      if (menuItem && menuItem.parentElement &&
          /lsf-main-menu/.test(menuItem.parentElement.className || "")) {
        menuItem.parentElement.removeChild(menuItem);
      } else if (a.parentElement) {
        // 行内的 "Learn more"：直接摘掉节点。父容器是 flex 列，少一个子节点不会塌陷。
        a.parentElement.removeChild(a);
      }
    }
  }

  // ---- 2b. 「个人访问令牌」：不是我们的功能，摘掉入口 ----------------------
  // LS 原生的 Personal Access Token 是一个长期 API 密钥，拿到之后可以在脚本里
  // `Authorization: Bearer <token>` 直连 LS，**绕过平台的 SSO 和项目授权**。
  // 服务端已经封死签发面（forge_sso.API_TOKEN_RE：标注员 403、甲方设计会话 404），
  // 但侧栏入口还留着 —— 点进去永远停在 "loading..."，「创建新令牌」是灰的，
  // 看起来像功能坏了。所以前端把入口摘掉；真正的边界仍然是服务端中间件。
  //
  // 为什么不放 #forge-ls-role-js：那段只对 restricted 会话生效，而这个入口
  // 对**所有**角色都不可用（甲方同样签不出令牌），跟角色无关。
  var PAT_HREF = "/user/account/personal-access-token";
  var PAT_TEXT = /^(个人访问令牌|Personal Access Tokens?|API Tokens?)$/;

  function dropPatEntry() {
    function removeLi(el) {
      var li = el.closest && el.closest("li");
      var host = li && li.parentElement ? li : el;
      if (host && host.parentNode) host.parentNode.removeChild(host);
    }
    // 首选 href 精确匹配（LS 升级改文案也不影响）
    var links = document.querySelectorAll('a[href*="' + PAT_HREF + '"]');
    for (var i = 0; i < links.length; i++) removeLi(links[i]);
    // 兜底：href 结构变了时按文案精确匹配。只认多字词，不收裸词，
    // 避免把标注界面里恰好叫「令牌」的选项按钮摘掉。
    var items = document.querySelectorAll("a.lsf-main-menu__item");
    for (var j = 0; j < items.length; j++) {
      var t = (items[j].textContent || "").replace(/\s+/g, " ").trim();
      if (t.length <= 12 && PAT_TEXT.test(t)) removeLi(items[j]);
    }
  }

  // 为什么不做「直连 URL 时弹说明条」：实测 LS 对 `/user/account/personal-access-token`
  // 的直连会落到「个人信息」页（URL 不变、内容换了），固定定位的说明条会盖住
  // 顶部标题栏和主题开关 —— 比不提示更糟。入口已经摘掉，这条路径实际上走不到，
  // 所以这里坚持"找不到就什么都不做"。

  // ---- 3. 补品牌元素 -------------------------------------------------------
  function brandEmptyStates() {
    // 3a. 数据管理空态：把圆形上传图标换成 FORGE 方版标记。
    //
    // ★ 这里踩过一个会持续膨胀的坑：最初用 `...rounded-full svg`（后代选择器）找图标，
    //   替换后我们的标记里又有一个 <svg>，下一轮 150ms 调度又把它当成目标，
    //   于是每轮往里面套一层 div，DOM 层级无限增长（实测 60 层还在涨）。
    //   修法：定位容器 + 幂等守卫，只处理「容器里还没有 forge-empty-mark」的情况。
    var circle = document.querySelector("div.bg-primary-emphasis.rounded-full");
    if (circle && !circle.querySelector(".forge-empty-mark")) {
      var svg = circle.querySelector("svg");
      if (svg) {
        var m = document.createElement("div");
        m.className = "forge-empty-mark";
        m.setAttribute("data-forge-skip", "1");
        m.innerHTML = BADGE;
        var svgEl = m.firstElementChild;
        if (svgEl) {
          svgEl.setAttribute("width", "40");
          svgEl.setAttribute("height", "40");
          svgEl.style.setProperty("display", "block");
        }
        circle.replaceChild(m, svg);
      }
    }

    // 3b. 右侧面板空态：在说明文案下方加一行品牌署名（同样带幂等守卫）
    var header = document.querySelector('[data-testid="empty-state-header"]');
    if (header && !document.querySelector(".forge-panel-sign")) {
      var host = header.parentElement;
      if (host) {
        var sign = document.createElement("div");
        sign.className = "forge-panel-sign";
        sign.setAttribute("data-forge-skip", "1");
        sign.textContent = "智途 标注工作台";
        host.appendChild(sign);
      }
    }

    // 3c. 左侧抽屉菜单底部：FORGE 方版标记 + 引擎版本。
    //
    // 菜单是「打开才渲染」的（关闭时 ul.lsf-main-menu 根本不在 DOM 里），
    // 所以这里每轮 sync 都要检查；一旦找到 spacer 就插一块，幂等靠 ul 内
    // 是否已有 .forge-menu-brand 判断。
    // spacer 是 flex-grow，插在它后面就贴在菜单底部。
    var ul = document.querySelector("ul.lsf-main-menu");
    if (ul && !ul.querySelector(".forge-menu-brand")) {
      var spacer = ul.querySelector(".lsf-main-menu__spacer");
      if (spacer && spacer.parentElement) {
        var box = document.createElement("li");
        box.className = "forge-menu-brand";
        box.setAttribute("data-forge-skip", "1");

        var mark = document.createElement("span");
        mark.innerHTML = BADGE;
        var svgNode = mark.firstElementChild;
        if (svgNode) box.appendChild(svgNode);

        var text = document.createElement("span");
        text.className = "forge-menu-brand__text";
        var name = document.createElement("span");
        name.className = "forge-menu-brand__name";
        name.textContent = BRAND;
        var meta = document.createElement("span");
        meta.className = "forge-menu-brand__meta";
        // 版本号取自页面上真实渲染的 vX.Y.Z，取不到就只写引擎名，
        // 不硬编码版本 —— 升级 LS 后这行不会说谎。
        meta.textContent = "标注引擎 " + (engineVersion() || "");
        text.appendChild(name);
        text.appendChild(meta);
        box.appendChild(text);

        spacer.parentElement.insertBefore(box, spacer.nextSibling);
      }
    }
  }

  // 从侧栏里那条 <a class="lsf-current-version">v1.23.2</a> 抓版本号。
  // 它在 CSS 里被 display:none 藏掉了（我们品牌块里已经显示版本，避免两行重复），
  // 但节点还在，所以这里仍然读得到 —— 不硬编码版本，升级 LS 后这行不会说谎。
  function engineVersion() {
    var el = document.querySelector("ul.lsf-main-menu .lsf-current-version");
    if (el) {
      var t = (el.textContent || "").trim();
      if (/^v\d+\.\d+/.test(t)) return t;
    }
    var ul = document.querySelector("ul.lsf-main-menu");
    if (!ul) return "";
    var m = (ul.innerText || "").match(/v\d+\.\d+\.\d+/);
    return m ? m[0] : "";
  }

  // ---- 4. 标题 -------------------------------------------------------------
  function swapTitle() {
    if (document.title.indexOf(SRC_TEXT) === -1) return;
    document.title = document.title.split(SRC_TEXT).join(BRAND);
  }

  // ---- 5. 删除导入页的第三方云存储品牌位 -----------------------------------
  // 「导入数据，开始你的第一个项目」页里 Amazon S3 / Google / Azure / Redis
  // 四个 logo 是一个整体容器。CSS 已隐藏，这里把节点摘掉，保证 DOM 里也不剩。
  function dropProviderIcons() {
    var box = document.querySelector('[data-testid="dm-storage-provider-icons"]');
    if (box && box.parentNode) box.parentNode.removeChild(box);
  }

  // ---- 6. 受限账号的入口隐藏不在这里 --------------------------------------
  // 相关逻辑统一放在 base.html 的 #forge-ls-role-js 里（那段脚本能直接读到
  // Django session 渲染出来的 can_import 标记），避免两处实现各自漂移。
  // 真正的安全边界仍然是 LS 服务端 core/forge_sso.py 的 ForgeSsoMiddleware。

  // ---- 5b. 标注流左下角的「1 of 1」计数 --------------------------------
  // 中文层是「逐文本节点 + 整串精确匹配」，这个计数器节点在首屏被扫到时
  // 内容还是英文、翻译结果为空，于是被记成「已处理」，之后即使文案规则
  // 命中也不会再翻（实测就是它一直显示 1 of 1）。这里做一次定点改写：
  // 只认 .lsf-current-task__task-count 这个类，不会碰到任务正文。
  function fixTaskCounter() {
    var nodes = document.querySelectorAll(".lsf-current-task__task-count");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = (el.textContent || "").trim();
      var m = /^(\d+)\s+of\s+(\d+)$/.exec(t);
      if (m) el.textContent = "第 " + m[1] + " / " + m[2] + " 条";
    }
  }

  // ---- 调度 ---------------------------------------------------------------
  function sync() {
    var now = Date.now();
    if (queued) return;
    // 标注过程中 DOM 高频变动（每画一个框都会改），150ms 节流足够
    if (now - lastRun < 150) {
      queued = true;
      window.setTimeout(function () { queued = false; sync(); }, 150);
      return;
    }
    lastRun = now;
    try {
      swapLogos();
      dropExternalLinks();
      dropPatEntry();
      brandEmptyStates();
      swapTitle();
      dropProviderIcons();
      fixTaskCounter();
    } catch (e) {
      /* 品牌替换永不影响业务功能 */
    }
  }

  function boot() {
    sync();
    try {
      var mo = new MutationObserver(sync);
      mo.observe(document.documentElement, { childList: true, subtree: true });
      var titleEl = document.querySelector("title");
      if (titleEl) mo.observe(titleEl, { childList: true, characterData: true, subtree: true });
    } catch (e) { /* 无 MutationObserver 时退化为只跑一次 */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 供 verify_brand.py 断言
  window.__FORGE_BRAND__ = { version: 4, brand: BRAND };
})();
