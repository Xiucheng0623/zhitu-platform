/* ============================================================================
   FORGE · Label Studio 中文界面层           【模板 · 由 build_zh_dict.py 生成 zh-cn.js】
   ----------------------------------------------------------------------------
   词库来源两路，合并后写进 EXACT：
     1. zh-cn-upstream.json  ← 社区补丁 haoxiangdong/label-studio-zh-cn 的 897 条
        （目标版本 1.23.0，与我们的 1.23.2 只差补丁版本号，语言包不随小版本变）
     2. zh-cn-overrides.json ← 我们自己手调的 158 条，覆盖优先级更高
   为什么要合并而不是直接用上游：
     上游的执行器对**标注平台是危险的**——
       a. shouldSkip 只挡 script/style/textarea/code/pre/svg，
          **不挡 .lsf-richtext / .lsf-html / .lsf-object**，任务正文会被翻译。
          实测语境：任务原文是 "History of Rome" 会被改成「历史 of Rome」。
       b. 它的 applyFragmentReplacements 是**兜底子串替换**，任何未命中词条的文本
          都会跑一遍 Upload/Text/Tools/Docs/Info/History/Manual/Images… 的包含替换。
       c. 无幂等标记，每轮 mutation 都重扫整棵 body 并 querySelectorAll("*")，
          replaceElementTextNodes 还会把同元素内多余文本节点清空（/\S+/g → ""）。
    所以本层【只借词库，不用它的执行器】。

   ★ 安全边界（最重要）★
   这是数据标注平台，**绝不能翻译被标注的数据**。任务原文里出现 "Save" 很正常。
   以下容器一律跳过：
       input / textarea / select / [contenteditable]
       .lsf-html .lsf-text .lsf-richtext        ← 渲染出来的任务正文
       .lsf-object .lsf-labels .lsf-region      ← 标注框与标签名（我们的分类法）
       .lsf-table__cell                         ← 数据管理表格里的任务文本列
       code / pre / script / style / svg
   只翻译 UI 文案，且只做**整串精确匹配 + 少量全串正则**，不做子串替换。

   维护成本：LS 升级改文案后，失配条目静默退回英文（不报错、不白屏）。
   `verify_brand.py` 会断言 zhEntries 非 0 与若干关键文案，改完跑一次即可。
   ============================================================================ */
(function () {
  "use strict";

  // ---- 整串精确匹配（897 条上游 + 158 条本项目，合并而成）------------------
  var EXACT = /*__EXACT_JSON__*/;

  // 大小写不敏感兜底：上游同一句会以 "Create project" / "Create Project" 两种形态出现
  var LOWER = (function () {
    var m = {};
    for (var k in EXACT) {
      if (!Object.prototype.hasOwnProperty.call(EXACT, k)) continue;
      var lk = k.toLowerCase();
      if (m[lk] === undefined) m[lk] = EXACT[k];
    }
    return m;
  })();

  // ---- 带变量的动态文案 ----------------------------------------------------
  // 顺序要紧：先具体后笼统。函数式替换 String.replace 原生支持。
  var MONTHS = {
    Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6,
    Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12,
    January: 1, February: 2, March: 3, April: 4, May: 5, June: 6,
    July: 7, August: 8, September: 9, October: 10, November: 11, December: 12
  };

  var PATTERNS = [
    // 日期时间（先于一切通用数字规则）
    [/^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4}),\s*(\d{1,2}:\d{2})(?::(\d{2}))?$/,
      function (_, d, m, y, hm, s) { return y + "年" + MONTHS[m] + "月" + Number(d) + "日 " + (s ? hm + ":" + s : hm); }],
    [/^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})\s*[∙·-]\s*(\d{1,2}:\d{2}:\d{2})$/,
      function (_, m, d, y, hms) { return y + "年" + MONTHS[m] + "月" + Number(d) + "日 " + hms; }],
    [/^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})[.,]?\s+(\d{1,2}:\d{2})(?::(\d{2}))?$/,
      function (_, m, d, y, hm, s) { return y + "年" + MONTHS[m] + "月" + Number(d) + "日 " + (s ? hm + ":" + s : hm); }],
    [/^(\d{1,2}:\d{2})\s*AM$/i, "上午 $1"],
    [/^(\d{1,2}:\d{2})\s*PM$/i, "下午 $1"],

    // 相对时间
    [/^just now$/i, "刚刚"],
    [/^less than a minute ago$/i, "不到一分钟前"],
    [/^a few seconds ago$/i, "几秒前"],
    [/^a minute ago$/i, "1 分钟前"],
    [/^an hour ago$/i, "1 小时前"],
    [/^a day ago$/i, "1 天前"],
    [/^yesterday$/i, "昨天"],
    [/^about\s+(\d+)\s+seconds? ago$/i, "约 $1 秒前"],
    [/^about\s+(\d+)\s+minutes? ago$/i, "约 $1 分钟前"],
    [/^about\s+(\d+)\s+hours? ago$/i, "约 $1 小时前"],
    [/^(\d+)\s+seconds? ago$/i, "$1 秒前"],
    [/^(\d+)\s+minutes? ago$/i, "$1 分钟前"],
    [/^(\d+)\s+hours? ago$/i, "$1 小时前"],
    [/^(\d+)\s+days? ago$/i, "$1 天前"],
    [/^(\d+)\s+weeks? ago$/i, "$1 周前"],
    [/^(\d+)\s+months? ago$/i, "$1 个月前"],
    [/^(\d+)\s+years? ago$/i, "$1 年前"],
    [/^Last prediction created\s+(\d+)\s+seconds? ago$/i, "上次预测创建于 $1 秒前"],
    [/^Last prediction created\s+(\d+)\s+minutes? ago$/i, "上次预测创建于 $1 分钟前"],
    [/^Last prediction created\s+(\d+)\s+hours? ago$/i, "上次预测创建于 $1 小时前"],
    [/^Created\s+(\d+)\s+seconds? ago$/i, "已创建 $1 秒前"],
    [/^Created\s+(\d+)\s+minutes? ago$/i, "已创建 $1 分钟前"],
    [/^Created\s+(\d+)\s+hours? ago$/i, "已创建 $1 小时前"],

    // 计数与进度
    // 标注流左下角的「1 of 1」：LS 里是 current of total，上游词库没有这条，
    // 不补的话每个标注员作业时都会看到一截英文。
    [/^(\d+)\s+of\s+(\d+)$/i, "第 $1 / $2 条"],
    [/^(\d+)\s+of\s+(\d+)\s+Tasks?\s+\((\d+)%\)$/i, "$1 / $2 个任务（$3%）"],
    [/^(\d+)\s+Tasks?$/i, "$1 个任务"],
    [/^(\d+)\s+annotations?$/i, "$1 条标注"],
    [/^(\d+)\s+predictions?$/i, "$1 条预测"],
    [/^(\d+)\s+characters?$/i, "$1 个字符"],
    [/^(\d+)\s+submissions?$/i, "$1 次提交"],
    [/^(\d+)\s*\/\s*(\d+)\s+submissions?$/i, "$1 / $2 次提交"],
    [/^(\d+)\s+files? uploaded$/i, "已上传 $1 个文件"],
    [/^(\d+)\s*\((\d+)\s+new\)$/i, "$1（新增 $2）"],
    [/^Labels\s*\((\d+)\)$/i, "标签（$1）"],
    [/^Choices\s*\((\d+)\)$/i, "选项（$1）"],
    [/^Columns:\s*(\d+)$/i, "列数：$1"],

    // 带参数的业务文案
    [/^Tasks:\s*(.+)$/i, "任务：$1"],
    [/^Submitted annotations:\s*(.+)$/i, "已提交标注：$1"],
    [/^Predictions:\s*(.+)$/i, "预测：$1"],
    [/^New Project #(\d+)$/i, "新项目 #$1"],
    [/^Source for task (\d+)$/i, "任务 $1 的源数据"],
    [/^Registered (.+), user ID (\d+)$/i, "注册于 $1，用户 ID $2"],
    [/^Copied "([^"]+)" for Task (\d+) to clipboard$/i, '已将任务 $2 的 "$1" 复制到剪贴板'],
    [/^Welcome\s*👋$/i, "欢迎 👋"],

    // 品牌位：LS 版本号一律以 FORGE 标注引擎名义出现
    [/^Label Studio Version:\s*(.+)$/i, "智途 标注引擎版本：$1"]
  ];

  // ---- 需要替换的属性 ------------------------------------------------------
  var ATTRS = ["title", "placeholder", "aria-label", "data-tooltip", "alt"];

  // ---- 元素级合并规则 ------------------------------------------------------
  // 有些文案在 DOM 里被拆成多个文本节点（"Label" + "all" + "Tasks"），逐节点
  // 翻译只能得到「标注 全部 Tasks」这种半截结果 —— 实测数据管理页的
  // 「标注全部 Tasks」就是这么来的。这类只能按元素整体改写。
  var LEAF_FIX = [
    [/^标注\s*(?:全部|所有)\s*Tasks?$/i, "标注全部任务"],
    [/^标注\s*Tasks?$/i, "标注任务"]
  ];

  function fixLeafLabels(root) {
    if (!root.querySelectorAll) return;
    var els = root.querySelectorAll("span, div, button, a, label, h1, h2, h3, h4");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.children.length) continue;          // 只处理纯文本叶子，别动容器
      if (shouldSkip(el)) continue;
      var t = (el.textContent || "").trim();
      if (!t || t.length > 24) continue;
      for (var j = 0; j < LEAF_FIX.length; j++) {
        var re = LEAF_FIX[j][0];
        re.lastIndex = 0;
        if (re.test(t)) {
          re.lastIndex = 0;
          var out = t.replace(re, LEAF_FIX[j][1]);
          if (out !== el.textContent) el.textContent = out;
          break;
        }
      }
    }
  }

  // ---- 绝不进入的容器 ------------------------------------------------------
  // ★ .lsf-table__cell 要排除表头（.lsf-th）：数据管理表格里**表头和数据格用的是
  //   同一个 class**，整类跳过会把 "Completed"/"Annotated by" 这些列头也挡掉。
  //   只跳非表头单元格 → 任务正文仍然受保护，列头能翻。
  var SKIP_SELECTOR = [
    "input", "textarea", "select", "code", "pre", "script", "style", "svg",
    "[contenteditable='true']",
    ".lsf-html", ".lsf-text", ".lsf-richtext",
    ".lsf-object", ".lsf-labels", ".lsf-region",
    ".lsf-table__cell:not(.lsf-th)",   // 数据单元格（任务正文列在这里）
    "[data-forge-skip]"
  ].join(",");

  function shouldSkip(node) {
    var el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return true;
    if (typeof el.closest !== "function") return true;
    return !!el.closest(SKIP_SELECTOR);
  }

  function tr(text) {
    if (!text) return null;
    var key = text.trim();
    if (!key || key.length > 400) return null;
    if (EXACT[key] !== undefined) return EXACT[key];
    var lk = key.toLowerCase();
    if (LOWER[lk] !== undefined) return LOWER[lk];
    for (var i = 0; i < PATTERNS.length; i++) {
      var re = PATTERNS[i][0];
      re.lastIndex = 0;
      if (re.test(key)) {
        re.lastIndex = 0;
        return key.replace(re, PATTERNS[i][1]);
      }
    }
    return null;
  }

  // 已经处理过的文本节点打标记，避免反复翻译和死循环。
  // ★ 记的是「处理时的内容」而不是布尔值：React 复用同一个 text 节点时，
  //   内容会从 A 变成 B。如果只记「处理过」，B 就永远不会被翻译
  //   （实测「1 minute ago」这类相对时间就是这么漏掉的）。
  var MARK = "forgeT";

  function walkText(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        if (shouldSkip(n)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var batch = [];
    var n;
    while ((n = walker.nextNode())) batch.push(n);
    for (var i = 0; i < batch.length; i++) {
      var node = batch[i];
      var cur = node.nodeValue;
      if (node[MARK] === cur) continue;   // 内容没变，跳过
      var out = tr(cur);
      if (out !== null && out !== cur) {
        node.nodeValue = out;
        node[MARK] = out;                 // 记的是替换后的内容
      } else {
        node[MARK] = cur;                 // 无对应词条，记下原文，下次内容变了还能再试
      }
    }
  }

  function walkAttrs(root) {
    if (!root.querySelectorAll) return;
    var els = root.querySelectorAll("[title], [placeholder], [aria-label], [data-tooltip], [alt]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (shouldSkip(el)) continue;
      for (var j = 0; j < ATTRS.length; j++) {
        var name = ATTRS[j];
        var v = el.getAttribute(name);
        if (!v) continue;
        var markKey = "data-forge-attr-" + name;
        if (el.getAttribute(markKey) === v) continue;   // 该属性内容没变
        var out = tr(v);
        if (out !== null && out !== v) {
          el.setAttribute(name, out);
          el.setAttribute(markKey, out);
        } else {
          el.setAttribute(markKey, v);
        }
      }
    }
  }

  var last = 0, queued = false;

  function sync() {
    var now = Date.now();
    if (queued) return;
    if (now - last < 60) {
      queued = true;
      window.setTimeout(function () { queued = false; sync(); }, 60);
      return;
    }
    last = now;
    try {
      walkText(document.body);
      walkAttrs(document.body);
      fixLeafLabels(document.body);
    } catch (e) {
      /* 汉化失败绝不影响标注功能 */
    }
  }

  function boot() {
    sync();
    // React 首屏是分批渲染的，MutationObserver 有时会漏掉「同一节点被改回英文」
    // 这种非 childList 的场景。补几次定点扫描，让首屏文案尽量一次到位。
    [300, 900, 2000, 4000].forEach(function (ms) {
      window.setTimeout(sync, ms);
    });
    try {
      var mo = new MutationObserver(sync);
      mo.observe(document.body, { childList: true, subtree: true, characterData: true });
    } catch (e) {
      /* 无 MutationObserver 时只跑一次 */
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 供自检脚本断言用。tr 一并暴露，让 verify_brand.py 能直接单测词条，
  // 不必为了验证一条词而构造整个 DOM。
  window.__FORGE_ZH__ = { entries: Object.keys(EXACT).length, patterns: PATTERNS.length, tr: tr };
})();
