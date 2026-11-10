/*!
 * 智途 · 标注工作台 UX 增强层（forge-ux）
 * ---------------------------------------------------------------------------
 * 为什么存在：Label Studio 社区版没有插件机制（官方 Plugins 是 Enterprise 独占），
 * 但它的标注前端（LSF）把运行时 store 挂在 window.Htx 上，且底部工具栏的按钮
 * 都有稳定的 data-testid。所以「国内标注平台那种顺手感」只能靠这一层注入来做：
 * 我们只加 UI 与校验，**所有业务动作都复用 LSF 原生实现**（点原生按钮 / 调原生
 * store 方法），不自己实现跳题与提交，避免和 LS 的状态机打架。
 *
 * 设计原则（踩过的坑都写在这里）：
 *   1. 幂等：重复注入不会重复建 DOM（SPA 里脚本可能被执行多次）。
 *   2. 只在标注页生效：找不到 .lsf-editor / window.Htx 就什么都不做。
 *   3. 不抢原生逻辑：上一题/下一题/跳过 = click 原生 testid 按钮；
 *      保存草稿 = 调 annotation.autosave()；提交 = click 原生提交按钮。
 *   4. 快捷键在输入框里不生效（除 Esc），否则标注员打标签会被吞键。
 *   5. 提交拦截只在「校验不通过」时 preventDefault，通过就完全放行。
 *   6. 所有自检信息暴露在 window.__FORGE_UX__.diag()，自动化测试据此断言，
 *      不靠截图猜。
 */
(function () {
  'use strict';

  if (window.__FORGE_UX__ && window.__FORGE_UX__.version) return; // 幂等

  var VERSION = '1.0.0';
  var Z = 2147482500;                       // 低于品牌启动遮罩，高于标注界面
  var C = {
    ink: '#1A1B1E', ink2: '#5B5F66', line: '#E4E2EE',
    primary: '#543C9E', primary2: '#7A5CD6', primarySoft: '#F2EEFB',
    danger: '#B23B3B', dangerSoft: '#FCEFEF', ok: '#2F7A5A'
  };
  var FONT = '-apple-system,BlinkMacSystemFont,"Microsoft YaHei","PingFang SC",sans-serif';

  var S = {
    lastLabels: null,       // 上一题提交的标签（复用用）
    lastRegionOnly: false,  // 上一题只有区域、没有可复用的分类标签
    snapRegionHit: false,   // 上一次快照里出现过区域类结果（提交时置位）
    taskKey: null,          // 当前 LS task id
    startedAt: 0,           // 当前题开始时间
    helpOpen: false,
    guideOpen: false,
    submitting: false,
    lastValidate: null,
    requiredMissing: [],   // 按 XML 算出来的缺项（含条件块收起的，仅供参考）
    diagCount: 0
  };

  // -------------------------------------------------------------------------
  // 小工具
  // -------------------------------------------------------------------------
  /**
   * 取 LSF 的应用 store（换题、提交、保存、校验全靠它）。
   *
   * 两代 LS 的挂法不一样，这里踩过一次很贵的坑：
   *   ≤1.22  window.Htx 直接就是 store
   *   1.23+  window.Htx 没了，实例改挂在 window.LabelStudio.instances（Set）上，
   *          store 在 实例.store
   * 只认 Htx 的后果不是报错，而是**界面全在、按钮全在、按下去什么都不发生**
   * （换题找不到原生按钮、保存拿不到 annotation）—— 最难查的那种失败。
   */
  function H() {
    if (window.Htx) return window.Htx;
    try {
      var inst = window.LabelStudio && window.LabelStudio.instances;
      if (inst && inst.size) {
        var it = inst.values().next().value;
        if (it && it.store) return it.store;
      }
    } catch (e) {}
    return null;
  }
  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function on(node, ev, fn, opts) { node.addEventListener(ev, fn, opts || false); }
  function log() {
    try { console.log.apply(console, ['[forge-ux]'].concat([].slice.call(arguments))); } catch (e) {}
  }

  /** 原生按钮：全部走 data-testid，LS 升级后若改名这里会第一时间暴露（diag 里有数量）。 */
  function nativeBtn(testid) { return $('[data-testid="' + testid + '"]'); }

  /**
   * 底栏那个"提交/更新"按钮。
   *
   * testid 会随**这题有没有已有标注**变化（实测）：
   *   全新任务 → data-testid="bottombar-submit-button"，文案「提交」
   *   有草稿/已有标注 → data-testid="bottombar-update-button"，文案「更新」
   * 只认前者，标注员做到一半（草稿已自动保存）再按「提交并下一题」就会得到
   * "找不到提交按钮，可能是 LS 版本变化" —— 而其实按钮好好在那儿。
   * 而"有草稿"恰恰是绝大多数时间所处的状态。
   */
  function nativeSubmitBtn() {
    return nativeBtn('bottombar-submit-button') || nativeBtn('bottombar-update-button');
  }
  function nativeSubmitBlocked() {
    var b = nativeSubmitBtn();
    if (!b) return true;      // 找不到就按"拦着"处理，宁可提示也不误提交
    return !!(b.disabled || b.getAttribute('aria-disabled') === 'true');
  }

  /**
   * LSF 校验没通过时弹的那个框（antd Modal，文案形如
   *   Warning / Checkbox "defect_type" is required. / [OK]
   * ）。
   *
   * 为什么必须认它（踩过的坑 · 假报成功）：LSF 的校验失败**不会**进
   * isSubmitting，也不会换任务，它只是弹个框。而增强层的提交判定原来只看
   * "isSubmitting 回落 / 任务变了"，于是"被拦住"被判成"已提交" ——
   * 标注员看到「已提交，进入下一题」，实际一条记录都没写
   * （实测：该 task 的 total_annotations 仍是 0）。这是最坏的一类 bug：
   * 数据没落库而人以为落了。
   */
  function nativeValidationDialog() {
    var nodes = document.querySelectorAll('.ant-modal, [class*="modal"]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var t = n.textContent || '';
      if (!/is required|are required/i.test(t)) continue;
      var r = n.getBoundingClientRect();
      if (!r.height) continue;          // 收起的旧弹窗不算
      return n;
    }
    return null;
  }

  /** 从弹窗文案里抠出缺哪个控件：Checkbox "defect_type" is required. */
  function dialogRequiredNames(node) {
    var t = (node && node.textContent) || '';
    var out = [], m;
    var re = /["\u201c\u2018']([^"\u201d\u2019']+)["\u201d\u2019']\s*(?:is|are)\s+required/gi;
    while ((m = re.exec(t))) {
      if (out.indexOf(m[1]) < 0) out.push(m[1]);
    }
    return out;
  }

  /** 关掉弹窗，免得它继续挡着下一步操作。 */
  function dismissDialog(node) {
    var scope = node || document;
    var btns = scope.querySelectorAll('.ant-modal button, .ant-btn, button');
    for (var i = 0; i < btns.length; i++) {
      if (/^(OK|确定|知道了)$/i.test((btns[i].textContent || '').trim())) {
        btns[i].click();
        return true;
      }
    }
    return false;
  }

  function clickNative(testid) {
    var b = nativeBtn(testid);
    if (!b) return false;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
    b.click();
    return true;
  }

  // -------------------------------------------------------------------------
  // 样式（一次性注入；品牌色与平台一致）
  // -------------------------------------------------------------------------
  function injectCss() {
    if ($('#fg-ux-css')) return;
    var css = ''
      + '#fg-ux-bar{position:fixed;top:6px;left:50%;transform:translateX(-50%);z-index:' + Z + ';'
      + 'display:flex;align-items:center;gap:2px;height:32px;padding:0 6px;'
      + 'background:#fff;border:1px solid ' + C.line + ';border-radius:16px;'
      + 'box-shadow:0 2px 10px rgba(26,27,30,.10);font:500 12px/1 ' + FONT + ';color:' + C.ink + ';}'
      + '#fg-ux-bar .fg-sep{width:1px;height:16px;background:' + C.line + ';margin:0 4px;}'
      // 窄屏（<=1200px）时顶部面包屑会一直排到页面中间，快捷条再贴着 top:6px
      // 就会压住项目名 —— 实测在 1080 宽下「项目 / PROBE · 条件分支」被整个盖住。
      // 这种宽度在笔记本外接屏、投屏演示时很常见，所以往下让一行。
      // 紧凑模式：只留图标 + 条数，文字整体隐掉。
      // 为什么需要：实测这条栏 681px 宽、居中后左边缘在 x=365，
      // 而顶部面包屑「项目 / <项目名>」会一直排到 400 多，长项目名直接被盖住。
      // 靠媒体查询挪位置没用 —— 下面还有任务标签行，挪下去照样压住别的东西。
      + '#fg-ux-bar.fg-compact .lb{display:none;}'
      + '#fg-ux-bar.fg-compact .fg-sep{display:none;}'
      + '#fg-ux-bar.fg-compact{padding:0 4px;}'
      + '#fg-ux-bar .fg-count{padding:0 8px;color:' + C.ink2 + ';font-variant-numeric:tabular-nums;white-space:nowrap;}'
      + '#fg-ux-bar .fg-count b{color:' + C.ink + ';font-weight:600;}'
      + '.fg-btn{display:inline-flex;align-items:center;gap:4px;height:24px;padding:0 9px;'
      + 'border:0;background:transparent;border-radius:12px;cursor:pointer;color:' + C.ink2 + ';'
      + 'font:500 12px/1 ' + FONT + ';white-space:nowrap;}'
      + '.fg-btn:hover{background:' + C.primarySoft + ';color:' + C.primary + ';}'
      + '.fg-btn:disabled{opacity:.4;cursor:not-allowed;background:transparent;color:' + C.ink2 + ';}'
      + '.fg-btn.fg-primary{background:' + C.primary + ';color:#fff;}'
      + '.fg-btn.fg-primary:hover{background:#6648BE;}'
      + '.fg-btn svg{width:13px;height:13px;flex:none;}'

      // 帮助面板
      + '#fg-ux-help{position:fixed;inset:0;z-index:' + Z + '1;background:rgba(20,22,26,.42);'
      + 'display:none;align-items:center;justify-content:center;}'
      + '#fg-ux-help.on{display:flex;}'
      + '#fg-ux-help .box{width:520px;max-width:92vw;max-height:82vh;overflow:auto;background:#fff;'
      + 'border-radius:14px;padding:22px 24px;box-shadow:0 18px 60px rgba(20,22,26,.28);'
      + 'font:400 13px/1.7 ' + FONT + ';color:' + C.ink + ';}'
      + '#fg-ux-help h3{margin:0 0 4px;font-size:15px;font-weight:600;}'
      + '#fg-ux-help .sub{color:' + C.ink2 + ';font-size:12px;margin-bottom:16px;}'
      + '#fg-ux-help table{width:100%;border-collapse:collapse;}'
      + '#fg-ux-help td{padding:7px 0;border-bottom:1px solid #F1EFF7;vertical-align:top;}'
      + '#fg-ux-help td:first-child{width:150px;}'
      + '#fg-ux-help kbd{display:inline-block;min-width:18px;padding:2px 6px;border:1px solid ' + C.line + ';'
      + 'border-bottom-width:2px;border-radius:5px;background:#FAF9FD;font:600 11px/1.4 ui-monospace,monospace;'
      + 'color:' + C.ink + ';text-align:center;}'
      + '#fg-ux-help .close{position:absolute;top:14px;right:16px;}'

      // 提示浮层（校验失败 / 操作反馈）
      + '#fg-ux-toast{position:fixed;left:50%;top:56px;transform:translateX(-50%) translateY(-6px);'
      + 'z-index:' + Z + '2;max-width:520px;padding:9px 14px;border-radius:10px;'
      + 'font:500 12.5px/1.5 ' + FONT + ';box-shadow:0 8px 26px rgba(20,22,26,.16);'
      + 'opacity:0;pointer-events:none;transition:opacity .16s,transform .16s;}'
      + '#fg-ux-toast.on{opacity:1;transform:translateX(-50%) translateY(0);}'
      + '#fg-ux-toast.err{background:' + C.dangerSoft + ';color:' + C.danger + ';border:1px solid #F3D6D6;}'
      + '#fg-ux-toast.ok{background:#EDF7F2;color:' + C.ok + ';border:1px solid #D3EADF;}'
      + '#fg-ux-toast.info{background:#fff;color:' + C.ink + ';border:1px solid ' + C.line + ';}'

      // 右侧规范侧栏
      + '#fg-ux-guide{position:fixed;top:0;right:0;bottom:0;width:340px;z-index:' + (Z - 1) + ';'
      + 'background:#fff;border-left:1px solid ' + C.line + ';box-shadow:-6px 0 24px rgba(20,22,26,.07);'
      + 'transform:translateX(100%);transition:transform .18s ease;display:flex;flex-direction:column;'
      + 'font:400 13px/1.75 ' + FONT + ';color:' + C.ink + ';}'
      + '#fg-ux-guide.on{transform:translateX(0);}'
      + '#fg-ux-guide .hd{display:flex;align-items:center;justify-content:space-between;'
      + 'padding:14px 16px;border-bottom:1px solid ' + C.line + ';}'
      + '#fg-ux-guide .hd b{font-size:13.5px;font-weight:600;}'
      + '#fg-ux-guide .bd{flex:1;overflow:auto;padding:14px 16px 24px;}'
      + '#fg-ux-guide .empty{color:' + C.ink2 + ';font-size:12.5px;line-height:1.9;}'
      + '#fg-ux-guide img{max-width:100%;border-radius:8px;border:1px solid ' + C.line + ';margin:10px 0;}'
      + '#fg-ux-guide h4{margin:16px 0 6px;font-size:12.5px;font-weight:600;color:' + C.ink2 + ';}'
      + '#fg-ux-guide pre{white-space:pre-wrap;word-break:break-word;font:400 12.5px/1.8 ' + FONT + ';margin:0;}'
      + '#fg-ux-guide .pill{display:inline-block;padding:1px 7px;border-radius:9px;background:' + C.primarySoft + ';'
      + 'color:' + C.primary + ';font-size:11px;margin-left:6px;}'
      + '.fg-iconbtn{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;'
      + 'border:0;background:transparent;border-radius:13px;cursor:pointer;color:' + C.ink2 + ';}'
      + '.fg-iconbtn:hover{background:' + C.primarySoft + ';color:' + C.primary + ';}';

    var st = el('style');
    st.id = 'fg-ux-css';
    st.textContent = css;
    document.head.appendChild(st);
  }

  // -------------------------------------------------------------------------
  // 图标（内联 SVG，不引外部资源）
  // -------------------------------------------------------------------------
  var ICO = {
    prev: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M10 3L5 8l5 5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    next: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 3l5 5-5 5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    save: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 2h8l2 2v10H3z"/><path d="M5.5 2v4h5V2M5.5 14v-4h5v4"/></svg>',
    skip: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 3l7 5-7 5zM12 3v10" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    submit: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 8.5l3.2 3.2L13 4.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    help: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6"/><path d="M6.2 6.2a1.9 1.9 0 113.4 1.2c-.5.7-1.6.9-1.6 1.9" stroke-linecap="round"/><circle cx="8" cy="11.6" r=".7" fill="currentColor" stroke="none"/></svg>',
    book: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2.5 3.2A1.2 1.2 0 013.7 2H13v11.2H3.7a1.2 1.2 0 00-1.2 1.2z"/><path d="M2.5 3.2V14"/></svg>',
    close: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 4l8 8M12 4l-8 8" stroke-linecap="round"/></svg>',
    reuse: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 8a5 5 0 015-5h3M14 8a5 5 0 01-5 5H6" stroke-linecap="round"/><path d="M9 1.6L11.4 3 9 4.4M7 11.6L4.6 13 7 14.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
  };
  function icon(name) {
    var s = el('span');
    s.innerHTML = ICO[name] || '';
    s.style.display = 'inline-flex';
    return s.firstChild || el('span');
  }
  function btn(label, iconName, opts) {
    opts = opts || {};
    var b = el('button', 'fg-btn' + (opts.primary ? ' fg-primary' : ''), '');
    b.type = 'button';
    if (iconName) b.appendChild(icon(iconName));
    var t = el('span', 'lb', label);   // .lb 供窄屏紧凑模式整体隐藏
    b.appendChild(t);
    if (opts.title) b.title = opts.title;
    if (opts.onClick) on(b, 'click', function (e) { e.preventDefault(); e.stopPropagation(); opts.onClick(); });
    return b;
  }

  // -------------------------------------------------------------------------
  // 提示浮层
  // -------------------------------------------------------------------------
  var toastTimer = null;
  function toast(msg, kind) {
    var t = $('#fg-ux-toast');
    if (!t) {
      t = el('div', '');
      t.id = 'fg-ux-toast';
      document.body.appendChild(t);
    }
    t.className = kind || 'info';
    t.textContent = msg;
    // 强制重排，保证连续 toast 也有动画
    void t.offsetWidth;
    t.classList.add('on');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('on'); }, kind === 'err' ? 4200 : 2400);
  }

  // -------------------------------------------------------------------------
  // 校验：必填未填 / 画了区域没选标签 / 完全空
  // -------------------------------------------------------------------------
  /**
   * 为什么不用 annotationStore.validate() 的返回值：
   * 它是 MobX action，把结果写进 annotationStore.validation（this.validation = ...），
   * 本身返回 undefined。直接读返回值会永远是 undefined，误判成「校验通过」。
   * 所以这里自己做三件事，再顺带把原生 validation 的错误一并算进去。
   */
  /**
   * 这个区域到底有没有选标签。
   *
   * 为什么不能只看 region.labeling.selected（实测踩到的阻塞级 bug）：
   * LSF 1.23 里 Region 的 `labeling` 是 Labeling 实例，字段是
   *   {from_name, to_name, type, value, selected, ...}
   * 其中 `selected` 是**界面上"哪个标签芯片被高亮"的瞬时态**，不是
   * "这个区域挂了哪些标签"。正常拖一个框、标签自动带上时，
   * value 里已经是 {rectanglelabels:["目标"]}，但 selected 仍然是 null。
   * 只信 selected 的后果：图像题、文本 span 题**每一次**提交都会被
   * 我们自己的校验拦住，提示"有 1 个区域还没有选择标签"，
   * 标注员只能绕开顶部快捷条去点原生按钮 —— 功能看着有，实际废掉。
   *
   * 所以按可信度从高到低取三个信源，任一为真即可：
   *   1. labeling.value 里任一分类型数组非空（落库口径，最可信）
   *   2. region.results 非空（区域挂的结果）
   *   3. labeling.selected 非空（界面上确有高亮标签）
   */
  function regionHasLabel(r) {
    if (!r) return false;
    var lg = r.labeling;
    if (lg) {
      var v = lg.value;
      if (v && typeof v === 'object') {
        for (var k in v) {
          if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
          var a = v[k];
          if (Array.isArray(a) ? a.length > 0 : (a !== null && a !== undefined && a !== '')) {
            return true;
          }
        }
      }
      if (lg.selected && lg.selected.length) return true;
    }
    if (r.results && r.results.length) return true;
    return false;
  }

  function collectValidation() {
    var h = H();
    var out = { ok: true, msgs: [] };
    if (!h || !h.annotationStore) { out.ok = false; out.msgs.push('标注引擎尚未就绪，请稍候重试'); return out; }

    var as = h.annotationStore;
    var sel = as.selected;
    if (!sel) { out.ok = false; out.msgs.push('当前没有可提交的标注'); return out; }

    // 1) 区域存在但没选标签（图像/文本 span 都适用）
    var regions = [];
    try {
      var rs = sel.regionStore || {};
      regions = rs.regions || rs.regionsArray || [];
      if (!regions.length && typeof rs.regions === 'function') regions = rs.regions;
    } catch (e) { regions = []; }
    var unlabeled = 0;
    for (var i = 0; i < regions.length; i++) {
      if (!regionHasLabel(regions[i])) unlabeled++;
    }
    if (unlabeled > 0) {
      out.ok = false;
      out.msgs.push('有 ' + unlabeled + ' 个区域还没有选择标签（选中区域后右侧面板选择标签即可）');
    }

    // 2) 完全空标注：没有区域、也没有任何分类选择
    var hasClassify = false;
    try {
      var asList = sel.areas || sel.results || [];
      hasClassify = asList.length > 0;
      if (!hasClassify && typeof sel.serializeAnnotation === 'function') {
        var ser = sel.serializeAnnotation();
        hasClassify = Array.isArray(ser) ? ser.length > 0 : !!(ser && ser.length);
      }
    } catch (e) { hasClassify = false; }
    if (!regions.length && !hasClassify) {
      out.ok = false;
      out.msgs.push('这一条还没有任何标注内容，提交前请先完成标注');
    }

    // 3) 必填控件（label_config 里 required="true"）
    //
    // 为什么不无条件报（踩过的坑 · 条件分支题）：label_config 支持
    //   <View visibleWhen="choice-selected" whenTagName="has_defect"
    //         whenChoiceValue="有缺陷"> <Choices name="defect_type" required="true"/>
    // 甲方最常见的"两步标注"就是这么写的。选了「无缺陷」的人，第二步控件是
    // display:none 的，但 XML 里 required 还在 —— 只读 XML 就会拦住他，
    // 提示"必填项未完成：defect_type"，而这个控件界面上根本不存在。
    //
    // 可见性只有 LSF 自己算得准（它按 visibleWhen 求值）。实测发现两个可用信号：
    //   · as.validation 恒为 null（LSF 到提交时才校验），当判据没用；
    //   · **原生提交按钮的 disabled 状态**才是权威：条件块可见且必填未填时
    //     它是 disabled，条件块一收起立刻变 enabled。
    // 所以把"必填项未完成"降级为"LSF 也认为不完整时才说"，
    // 既保住友好文案，又不会误拦条件分支题。
    var required = requiredNames();
    if (required.length) {
      var filled = filledFromNames();
      var missing = required.filter(function (n) { return !filled[n]; });
      S.requiredMissing = missing;
      if (missing.length && !hasHiddenControls()) {
        out.ok = false;
        out.msgs.push('必填项未完成：' + missing.join('、'));
      }
    }

    // 4) 原生校验状态（required / perRegion 等由 LS 自己判定的部分）
    var native = as.validation;
    if (native) {
      var nm = [];
      if (Array.isArray(native)) nm = native.map(function (x) { return x && (x.message || x.msg) || ''; }).filter(Boolean);
      else if (native.messages) nm = [].concat(native.messages).filter(Boolean);
      else if (native.ids && native.ids.length) nm = ['有 ' + native.ids.length + ' 处标注不满足配置要求'];
      if (nm.length) { out.ok = false; out.msgs = out.msgs.concat(nm); }
    }

    S.lastValidate = out;
    return out;
  }

  /**
   * 页面上有没有"被条件分支收起"的控件。
   *
   * 判据：LSF 给 visibleWhen 未满足的控件加 *._hidden 类（实测 .lsf-choices_hidden，
   * 同时 display:none、高度 0）。这里只看控件容器，不扫全页，避免被无关的
   * 隐藏元素误伤。
   */
  function hasHiddenControls() {
    return !!document.querySelector('[class*="lsf-"][class*="_hidden"]');
  }

  /** 从 labeling config 里读 required="true" 的控件名。config 是 XML 字符串（Htx.config）。 */
  function requiredNames() {
    var h = H();
    var cfg = h && h.config ? String(h.config) : '';
    if (!cfg) return [];
    var names = [];
    // 不枚举控件标签名，直接找「任何带 required="true" 的标签」。
    // 为什么改（踩过的坑）：原来写死了一份白名单
    //   (Choices|Labels|Taxonomy|TextArea|Number|Rating|Rectangle|Polygon|KeyPoint|BrushLabels)
    // 而实际写图像标注用的是 <RectangleLabels> / <PolygonLabels> / <KeyPointLabels>
    // —— 是带 Labels 后缀的那几个，白名单里一个都没有。
    // 结果是图像任务上「必填项未完成：xxx」这条友好提示永远不出现
    // （LSF 原生仍会拦，所以不会漏拦，但提示退化成引擎的原始话术）。
    // 白名单这种写法还有个更坏的后果：LS 每加一个新控件就要改一次代码，
    // 忘了改就静默失效。所以改成通用匹配。
    var re = /<([A-Za-z][\w.]*)\b([^>]*\brequired\s*=\s*["']true["'][^>]*)>/g;
    var m;
    while ((m = re.exec(cfg))) {
      var tag = m[1];
      // View 是布局容器，不是可填控件；name 属性只出现在控件上
      if (/^(View|Style)$/.test(tag)) continue;
      var nm = /\bname\s*=\s*["']([^"']+)["']/.exec(m[2]);
      if (nm && names.indexOf(nm[1]) < 0) names.push(nm[1]);
    }
    return names;
  }

  /** 当前标注里，各 from_name 是否已有值。 */
  function filledFromNames() {
    var h = H();
    var filled = {};
    try {
      var sel = h.annotationStore.selected;
      var list = sel.serializeAnnotation ? sel.serializeAnnotation() : (sel.results || sel.areas || []);
      for (var i = 0; i < list.length; i++) {
        var it = list[i];
        if (it && it.from_name) filled[it.from_name] = true;
      }
      // 区域上的标签也算。
      // 注意 labeling.from_name 是**字符串**（控件名），而 labeling.selected
      // 里的元素才是带 from_name 的标签对象 —— 两者别混用；原来只读 selected，
      // 于是"框选控件的 required"永远判成没填（与 regionHasLabel 同源的那个坑）。
      var rs = sel.regionStore || {};
      var regions = rs.regions || rs.regionsArray || [];
      for (var j = 0; j < regions.length; j++) {
        var r = regions[j];
        if (!regionHasLabel(r)) continue;
        var lg = r.labeling || {};
        var fname = lg.from_name ||
          (lg.selected && lg.selected[0] && lg.selected[0].from_name) || '';
        if (fname) filled[fname] = true;
      }
    } catch (e) {}
    return filled;
  }

  // -------------------------------------------------------------------------
  // 复用上一题标签（只针对分类类控件，通过点击对应的选项实现）
  // -------------------------------------------------------------------------
  /**
   * 区域类控件的 result.type 名字（全小写）。
   *
   * 为什么需要这张表：文本题的 span 结果里也有 `value.labels` 字段，
   * 与 Choices 的 `value.choices` 在结构上看着一样，但一个是"区域"
   * （没法套用到下一题，只能重新画）一个是"分类"（可以一键复用）。
   * 只按 value 里有没有 labels/choices 判断，文本 span 会被误当成可复用标签，
   * 于是标注员点复用时得到「控件与上一题不一致」，而不是「这类标注不能复用」。
   */
  var REGION_TYPES = {
    labels: 1, rectanglelabels: 1, polygonlabels: 1, keypointlabels: 1,
    brushlabels: 1, ellipselabels: 1, videorectangle: 1
  };

  function snapshotLabels() {
    var h = H();
    if (!h || !h.annotationStore || !h.annotationStore.selected) return null;
    S.snapRegionHit = false;
    try {
      var sel = h.annotationStore.selected;
      var list = sel.serializeAnnotation ? sel.serializeAnnotation() : (sel.results || []);
      var map = {};
      var any = false;
      for (var i = 0; i < list.length; i++) {
        var r = list[i];
        if (!r || !r.from_name) continue;
        if (REGION_TYPES[String(r.type || '').toLowerCase()]) {
          S.snapRegionHit = true;   // 记下来：这题标的是区域，不是分类
          continue;
        }
        var v = r.value || {};
        // 分类类：choices / labels / taxonomy
        var keys = v.choices || v.labels || v.taxonomy;
        if (keys) {
          var arr = Array.isArray(keys) ? keys : [keys];
          map[r.from_name] = arr.map(function (x) { return String(x); });
          any = true;
        }
      }
      return any ? map : null;
    } catch (e) { return null; }
  }

  /**
   * 应用上一题标签：不直接写 store（各控件内部结构不同，容易写坏），
   * 而是找到界面上对应的选项元素点它 —— 等价于标注员自己点，
   * 控件自己会更新 state、触发 autosave 与撤销栈。
   */
  function applyLabels(map) {
    if (!map) return 0;
    var applied = 0;
    Object.keys(map).forEach(function (fromName) {
      var wanted = map[fromName] || [];
      wanted.forEach(function (val) {
        if (clickChoice(fromName, val)) applied++;
      });
    });
    return applied;
  }

  /**
   * 当前标注里有没有区域（框选/分割/关键点/文本 span）。
   *
   * 单独抽出来是因为「复用上一题标签」需要区分：空标注 / 只有区域 / 有分类标签。
   * 只有区域时复用没有意义，但要给出准确原因（区域类标注不能套用）。
   */
  function hasRegions() {
    var h = H();
    var sel = h && h.annotationStore && h.annotationStore.selected;
    if (!sel) return false;
    try {
      var rs = sel.regionStore || {};
      var list = rs.regions || rs.regionsArray || [];
      if (typeof rs.regions === 'function') list = rs.regions;
      return !!(list && list.length);
    } catch (e) { return false; }
  }

  /** 在标注界面里找到「某控件 + 某选项值」的元素并点击。 */
  function clickChoice(fromName, value) {
    // LSF 的 Choices 渲染成 ant-design 的复选框：
    //   div.lsf-choices > div.lsf-choice > label.ant-checkbox-wrapper
    //     > span.ant-checkbox > input[name="OK"][type=checkbox]
    // 所以第一优先按 input[name] 精确找 —— 文本匹配会撞上 [1] 这种提示角标。
    var inputs = document.querySelectorAll(
      '.lsf-choices input[type="checkbox"], .lsf-choices input[type="radio"],' +
      '.lsf-choice input[type="checkbox"], .lsf-choice input[type="radio"]');
    var candidates = [];
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].name === String(value)) candidates.push(inputs[i]);
    }
    // 同一个选项值可能出现在多个控件里（两个 Control 都叫 OK），
    // 这时只有 fromName 能区分；区分不了就不点，宁可提示「无法复用」
    // 也不能把标签打到别的控件上 —— 那是静默的错数据。
    if (candidates.length === 1) {
      // 已经是选中态就别再点。check/radio 的 click 是**切换**语义，
      // 再点一次等于取消 —— 实测踩到：提交后 LSF 停在原题（最后一题时
      // 不会跳走），这时按复用会把刚刚提交的选项又点掉，标注变成未完成。
      // 复用的语义是"让这一题达到上一题的状态"，已是目标状态就算成功。
      if (candidates[0].checked) return true;
      var label = candidates[0].closest('label') || candidates[0];
      label.click();
      return true;
    }
    if (candidates.length > 1) {
      var scoped = null;
      var blocks = document.querySelectorAll('.lsf-choices');
      for (var b = 0; b < blocks.length; b++) {
        var blk = blocks[b];
        var owner = blk.getAttribute('name') || blk.getAttribute('data-name') ||
          (blk.parentElement && (blk.parentElement.getAttribute('name') ||
            blk.parentElement.getAttribute('data-name')));
        if (owner && owner === fromName && blk.contains(candidates[0])) { scoped = blk; break; }
      }
      if (scoped) {
        var ins = scoped.querySelectorAll('input[name="' + String(value).replace(/"/g, '\\"') + '"]');
        if (ins.length === 1) {
          if (ins[0].checked) return true;   // 同上：已是选中态，别再切换掉
          (ins[0].closest('label') || ins[0]).click();
          return true;
        }
      }
      return false;
    }

    // 兜底：Taxonomy / Labels 等非 ant-design 控件按可见文本精确匹配
    var nodes = document.querySelectorAll('[class*="label"], [class*="choice"], label, [role="checkbox"], [role="radio"]');
    for (var j = 0; j < nodes.length; j++) {
      var n = nodes[j];
      var txt = (n.innerText || '').trim();
      if (txt !== value) continue;
      var scope = n.closest('[class*="' + fromName + '"], [data-name="' + fromName + '"]') || null;
      if (!scope && !isInsideCanvas(n)) {
        // 不限定控件时，只有唯一的同名选项才敢点，避免误点别的控件的同名选项
        var same = document.querySelectorAll('[class*="label"], [class*="choice"], label');
        var count = 0;
        for (var k = 0; k < same.length; k++) if ((same[k].innerText || '').trim() === value) count++;
        if (count > 1) continue;
      }
      var clickable = n.tagName === 'LABEL' ? n : (n.querySelector('input') || n);
      clickable.click();
      return true;
    }
    return false;
  }
  function isInsideCanvas(n) { return !!n.closest('.lsf-main-view, [class*="konva"]'); }

  // -------------------------------------------------------------------------
  // 顶部快捷条
  // -------------------------------------------------------------------------
  /**
   * 让快捷条别压住顶部面包屑。
   *
   * 做法不是"按窗口宽度猜"，而是量出来：拿面包屑的右边缘和快捷条的左边缘比，
   * 相撞就切紧凑模式（隐藏文字只留图标），变窄后再量一次。
   * 为什么不用媒体查询：这条栏宽度是内容决定的，窗口宽度不等于可用空间 ——
   * 项目名长短差很多，同一个窗口宽度下有时压住有时不压。
   */
  function fitBar() {
    var bar = $('#fg-ux-bar');
    if (!bar) return;
    bar.classList.remove('fg-compact');
    var crumb = document.querySelector('[class*="breadcrumbs"]') ||
                document.querySelector('.lsf-header');
    var main = document.querySelector('.lsf-main-view');
    var limit = 0;
    if (crumb && main && main.contains(bar) === false) {
      var cr = crumb.getBoundingClientRect();
      if (cr.width) limit = cr.right + 14;
    }
    var r = bar.getBoundingClientRect();
    if (limit && r.left < limit) bar.classList.add('fg-compact');
  }

  // -------------------------------------------------------------------------
  // 换题：LS 1.23 起没有 prev/next 原生按钮了
  // -------------------------------------------------------------------------
  /**
   * ≤1.22 的底栏有 data-testid="prev-task" / "next-task"；
   * 1.23.2 改成"数据管理 + 快速查看"之后，底栏只剩
   * update / undo / redo / reset / settings 五个 testid，换题只能点左侧表格行。
   * 原来只按老 testid 找按钮，于是 Alt+↑/↓、Alt+S 全成了哑键（不报错，只是没反应）。
   */
  function rowsNow() { return [].slice.call(document.querySelectorAll('.lsf-table-row')); }

  function projectIdNow() {
    var m = (location.pathname || '').match(/\/projects\/(\d+)/);
    return m ? m[1] : null;
  }

  function currentTaskId() {
    var h = H();
    if (h && h.task && h.task.id != null) return h.task.id;
    var q = new URLSearchParams(location.search).get('task');
    return q ? parseInt(q, 10) : null;
  }

  var idCache = { pid: null, ids: null, at: 0 };

  /** 这一批的任务 id（升序）。缓存 60 秒，避免连按方向键时把接口打爆。 */
  function taskIds(cb) {
    var pid = projectIdNow();
    if (!pid) { cb(null); return; }
    if (idCache.pid === pid && idCache.ids && Date.now() - idCache.at < 60000) {
      cb(idCache.ids); return;
    }
    try {
      fetch('/api/tasks?project=' + pid + '&page_size=1000', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var list = (j && j.tasks) || j || [];
          var ids = [];
          for (var i = 0; i < list.length; i++) ids.push(list[i].id);
          idCache = { pid: pid, ids: ids, at: Date.now() };
          cb(ids);
        })
        .catch(function () { cb(null); });
    } catch (e) { cb(null); }
  }

  /** dir: -1 上一题 / +1 下一题 */
  function navTask(dir) {
    var cur = currentTaskId();
    taskIds(function (ids) {
      if (!ids || !ids.length) { toast('读不到这一批的任务列表', 'err'); return; }
      var at = ids.indexOf(cur);
      if (at < 0) { toast('当前这一条不在本批列表里（可能刚被改动）', 'err'); return; }
      var j = at + dir;
      if (j < 0) { toast('已经是这一批的第一题了', 'info'); return; }
      if (j >= ids.length) { toast('已经是这一批的最后一题了', 'info'); return; }
      var rows = rowsNow();
      // 只按行数对不上就拒绝，宁可让人自己点，也别跳到错的那一条
      if (rows.length !== ids.length) {
        toast('列表被筛选或排序过，无法自动换题，请直接点行', 'info'); return;
      }
      rows[j].click();
    });
  }

  /**
   * 标记"跳过"。
   *
   * 不能直接 `sel.skipped = true`：annotation 是 MST 保护树，赋值会抛
   * "the object is protected and can only be modified by using an action"。
   * 所以按动作名挨个试（不同版本名字不一样），都试不到就老实说没标上。
   */
  function markSkipped(sel) {
    if (!sel) return false;
    var names = ['setSkipped', 'skip', 'markSkipped'];
    for (var i = 0; i < names.length; i++) {
      if (typeof sel[names[i]] === 'function') {
        try {
          sel[names[i]](true);
          if (typeof sel.autosave === 'function') sel.autosave();
          return true;
        } catch (e) {}
      }
    }
    return false;
  }

  /**
   * 跳过：LS 1.23 也没有 skip 按钮了。
   * 语义 = 这一题不标、进下一题；引擎认不认"跳过"这一笔要看版本，
   * 所以两种情况分开说，不把"没标上"说成"已跳过"。
   */
  function skipTask() {
    var h = H();
    var marked = markSkipped(h && h.annotationStore && h.annotationStore.selected);
    navTask(1);
    toast(marked ? '已跳过这一题' : '已切到下一题（这一条仍算未标注）', 'info');
  }

  function buildBar() {
    if ($('#fg-ux-bar')) return;
    var bar = el('div');
    bar.id = 'fg-ux-bar';

    bar.appendChild(btn('上一题', 'prev', {
      title: '上一题（Alt + ↑）',
      onClick: function () { navTask(-1); }
    }));
    bar.appendChild(btn('下一题', 'next', {
      title: '下一题（Alt + ↓）',
      onClick: function () { navTask(1); }
    }));
    bar.appendChild(el('span', 'fg-sep'));

    var count = el('span', 'fg-count');
    count.id = 'fg-ux-count';
    count.textContent = '第 — / — 条';
    bar.appendChild(count);
    bar.appendChild(el('span', 'fg-sep'));

    bar.appendChild(btn('保存草稿', 'save', {
      title: '立即保存草稿（Ctrl + S）',
      onClick: saveDraft
    }));
    bar.appendChild(btn('复用上题', 'reuse', {
      title: '把上一题提交的分类标签套到这一题（Alt + Shift + R）',
      onClick: reuseLast
    }));
    bar.appendChild(btn('跳过', 'skip', {
      title: '跳过这一题（Alt + S）',
      onClick: skipTask
    }));
    bar.appendChild(btn('提交并下一题', 'submit', {
      primary: true, title: '校验通过后提交，并自动进入下一题（Ctrl + Enter）',
      onClick: function () { submitAndNext(); }
    }));
    bar.appendChild(el('span', 'fg-sep'));
    bar.appendChild(btn('', 'book', {
      title: '标注规范与示例（G）',
      onClick: function () { toggleGuide(); }
    }));
    bar.appendChild(btn('', 'help', {
      title: '快捷键（?）',
      onClick: function () { toggleHelp(); }
    }));

    document.body.appendChild(bar);
    refreshCount();
  }

  function refreshCount() {
    var node = $('#fg-ux-count');
    if (!node) return;
    var pos = null;
    var total = null;

    // 优先镜像 LSF 底部那条「第 n / N 条」。
    // 为什么不直接用 Htx.queuePosition/queueTotal：那两个是"本批未完成队列"的
    // 序号，而 LSF 底部显示的是"项目内第几条"。批里一旦有人标过几题，两者就会
    // 不一样（实测同屏出现"第 1 / 1 条"和"第 1 / 2 条"），用户会以为坏了一个。
    // 快捷条和底部原始栏说的是同一件事才不会互相打脸。
    var ref = $('.lsf-current-task__task-count');
    if (ref) {
      var m = (ref.textContent || '').match(/(\d+)\s*\/\s*(\d+)/);
      if (m) { pos = Number(m[1]); total = Number(m[2]); }
    }

    if (pos === null || total === null) {
      // 兜底：底部计数器不在（版本变了/布局收起）时退回队列序号
      var h = H();
      pos = h && typeof h.queuePosition === 'number' ? h.queuePosition : null;
      total = h && typeof h.queueTotal === 'number' ? h.queueTotal : null;
    }

    node.innerHTML = (total === null || pos === null)
      ? '第 <b>—</b> / — 条'
      : '第 <b>' + pos + '</b> / ' + total + ' 条';
  }

  function saveDraft() {
    var h = H();
    var sel = h && h.annotationStore && h.annotationStore.selected;
    if (!sel || typeof sel.autosave !== 'function') { toast('当前没有可保存的标注', 'info'); return; }
    try {
      sel.autosave();
      toast('草稿已保存', 'ok');
    } catch (e) {
      toast('草稿保存失败：' + (e && e.message ? e.message : '未知错误'), 'err');
    }
  }

  function reuseLast() {
    if (!S.lastLabels) {
      // 区分"还没标过"和"标过但这题型不支持复用"。
      // 原来只有一句「提交一次后就有了」，在图像框选任务上会让标注员
      // 明明刚提交完却被告知"还没有可复用的标签" —— 归因错了，
      // 人会以为提交没成功或功能坏了。复用只对分类类控件有意义，
      // 区域（框选/分割/关键点）本来就没法"套用"。
      if (S.lastRegionOnly) {
        toast('上一题标的是区域（框选/分割），这类标注不能一键复用', 'info');
      } else {
        toast('还没有可复用的上一题标签（提交一次后就有了）', 'info');
      }
      return;
    }
    var n = applyLabels(S.lastLabels);
    if (n > 0) toast('已套用上一题的 ' + n + ' 个标签', 'ok');
    else toast('这一题的控件与上一题不一致，无法直接复用', 'info');
  }

  // -------------------------------------------------------------------------
  // 提交并下一题
  // -------------------------------------------------------------------------
  function submitAndNext() {
    if (S.submitting) { toast('上一条还在提交中…', 'info'); return; }
    var v = collectValidation();
    if (!v.ok) { toast(v.msgs[0] || '校验未通过', 'err'); return; }

    var h = H();
    var before = h.task && h.task.id;
    var snap = snapshotLabels();
    // 区域标记必须在**提交之前**取。原来是在提交完成回调里调 hasRegions()，
    // 而那时 LSF 常常已经自动跳到下一题 —— 读到的是下一题的区域，
    // 于是「上一题标的是区域」这句提示变得时有时无，取决于跳题快慢。
    var hadRegions = hasRegions();
    var submitBtn = nativeSubmitBtn();
    if (!submitBtn) { toast('找不到提交/更新按钮（可能是 LS 版本变化）', 'err'); return; }

    S.submitting = true;
    submitBtn.click();
    toast('提交中…', 'info');

    // 轮询：提交完成的判定是「isSubmitting 从 true 回落」或 task 变化
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      var hh = H();
      // 先看"被引擎拦住"：弹窗一出现就是没提交成功，别在这儿等 isSubmitting
      var dlg = nativeValidationDialog();
      if (dlg) {
        clearInterval(timer);
        S.submitting = false;
        var miss = dialogRequiredNames(dlg);
        toast(miss.length ? '必填项未完成：' + miss.join('、')
                          : '这一条还没通过标注引擎的校验', 'err');
        dismissDialog(dlg);
        return;
      }
      var busy = hh && hh.isSubmitting;
      var changed = hh && hh.task && hh.task.id !== before;
      if (!busy || changed || tries > 40) {
        clearInterval(timer);
        S.submitting = false;
        // 记住"上一题到底有什么"：有分类标签才谈得上复用；
        // 只有区域（框选/分割）时记一个标记，好在点复用时说清楚原因。
        // 两个来源取并集：regionStore 里数出来的 + 序列化结果里 type 认出来的。
        S.lastLabels = snap || null;
        S.lastRegionOnly = !snap && !!(S.snapRegionHit || hadRegions);
        reportEvent('submit');
        // 提交成功即以 submit 上的时长为准，清掉计时器：
        // 否则紧接着的 tickTask 还会再发一条 leave，把「提交后停在完成页」的
        // 时间也算进这题（实测：submit 2114ms 之后又来一条 leave 2402ms，
        // 统计取最后一条，等于把发呆时间记成了作业时间）。
        S.startedAt = 0;
        if (tries > 40 && busy) { toast('提交超时，请检查网络后重试', 'err'); return; }
        // 提交成功后前进；原生按钮自己也可能已经前进，这里判一下再点
        if (!changed) {
          var moved = clickNative('next-task');
          if (!moved) toast('已提交（这是最后一题）', 'ok');
          else toast('已提交，进入下一题', 'ok');
        } else {
          toast('已提交，进入下一题', 'ok');
        }
      }
    }, 250);
  }

  // -------------------------------------------------------------------------
  // 快捷键
  // -------------------------------------------------------------------------
  var KEYMAP = [
    { keys: 'Alt + ↑', desc: '上一题' },
    { keys: 'Alt + ↓', desc: '下一题' },
    { keys: 'Ctrl / ⌘ + Enter', desc: '校验并提交，自动进入下一题' },
    { keys: 'Ctrl / ⌘ + S', desc: '立即保存草稿' },
    { keys: 'Alt + S', desc: '跳过这一题' },
    { keys: 'Alt + Shift + R', desc: '复用上一题标签' },
    { keys: 'G', desc: '打开 / 关闭标注规范侧栏' },
    { keys: '+ / =', desc: '放大（图片题）' },
    { keys: '-', desc: '缩小（图片题）' },
    { keys: '0', desc: '恢复到初始大小（图片题）' },
    { keys: '?', desc: '打开 / 关闭这个面板' },
    { keys: 'Esc', desc: '关闭弹层' }
  ];

  function inTyping(e) {
    var t = e.target;
    if (!t) return false;
    var tag = (t.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    if (t.isContentEditable) return true;
    // LSF 的文本标注区可能是 contenteditable 的容器
    return !!(t.closest && t.closest('[contenteditable="true"]'));
  }

  function onKeyDown(e) {
    var k = e.key;

    if (k === 'Escape') {
      if (S.helpOpen) { toggleHelp(false); e.preventDefault(); return; }
      if (S.guideOpen) { toggleGuide(false); e.preventDefault(); return; }
      return;
    }
    if (inTyping(e)) return;              // 输入框里不抢键

    // Alt + ↑/↓ 换题。
    // 为什么不用更顺手的 Alt + ←/→：LSF 自己把 alt+left/right 绑给了
    // 「旋转图片 / 拖动进度 / 收缩片段」。我们的监听在 window 捕获阶段并
    // preventDefault，抢过来就等于让图片题**再也没法旋转**。
    // alt+up/down 在 LSF 的整个快捷键表里没有被占用（shift+up/down 是"聚焦
    // 任务列表"，ctrl+up/down 是"上/下一句"），是唯一干净的方向键组合。
    if (e.altKey && !e.ctrlKey && !e.metaKey && (k === 'ArrowUp' || k === 'ArrowDown')) {
      e.preventDefault();
      navTask(k === 'ArrowUp' ? -1 : 1);
      return;
    }
    // Ctrl/Cmd + Enter 提交并下一题；Ctrl/Cmd + S 保存草稿
    if ((e.ctrlKey || e.metaKey) && k === 'Enter') { e.preventDefault(); submitAndNext(); return; }
    if ((e.ctrlKey || e.metaKey) && (k === 's' || k === 'S')) { e.preventDefault(); saveDraft(); return; }
    // Alt + S 跳过
    if (e.altKey && !e.ctrlKey && !e.metaKey && (k === 's' || k === 'S')) {
      e.preventDefault();
      skipTask();
      return;
    }
    // Alt + Shift + R 复用上一题标签。
    // 裸 R 不能用了：LSF 把 R 绑给了「矩形工具」，抢过来会让标注员按 R 选不了
    // 矩形；Alt+R 也被「创建区域关系」占了。alt+shift+R 在 LSF 里是空的。
    if (e.altKey && e.shiftKey && (k === 'r' || k === 'R')) {
      e.preventDefault(); reuseLast(); return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    if (k === '?') { e.preventDefault(); toggleHelp(); return; }
    if (k === 'g' || k === 'G') { e.preventDefault(); toggleGuide(); return; }
    if (k === '+' || k === '=') { e.preventDefault(); zoom(1); return; }
    if (k === '-' || k === '_') { e.preventDefault(); zoom(-1); return; }
    if (k === '0') { e.preventDefault(); zoom(0); return; }
  }

  /**
   * 缩放：转发 LSF 自己的快捷键，而不是自己算比例。
   *
   * 为什么转发：LSF 的缩放最终是改 <img> 的 width/transform 内联样式，
   * 比例、边界、与框选坐标的换算都在它内部（Region 坐标依赖缩放态）。
   * 自己改样式，框选坐标就会对不上 —— 这是绝对不能碰的耦合点。
   *
   * 原生键位（从 LSF 的 hotkey 注册表里读出来的）：
   *   ctrl+plus  放大    ctrl+minus 缩小
   *   shift+1    适应窗口  shift+2  100%
   * 我们用 + / - / 0 三个单键去触发它们，因为标注员不想每次按三个键。
   * 派发后比对 <img> 的宽度是否变化：没变说明这题没有画布（纯文本题），给提示。
   */
  function nativeHotkey(opts) {
    try {
      var ev = new KeyboardEvent('keydown', {
        bubbles: true, cancelable: true,
        key: opts.key, code: opts.code,
        keyCode: opts.keyCode, which: opts.keyCode,
        ctrlKey: !!opts.ctrlKey, shiftKey: !!opts.shiftKey,
        altKey: !!opts.altKey, metaKey: !!opts.metaKey
      });
      // 关键：LSF 的热键表是按**旧式 keyCode** 认键的（注册表里写 "ctrl+plus"，
      // 实际比对的是 keyCode 187）。只给 key/code 它不认 —— 实测踩过：
      // 合成事件发出去、页面毫无反应，而物理按键正常。
      // keyCode/which 是只读属性，构造参数被忽略时要显式 defineProperty。
      if (!ev.keyCode) {
        Object.defineProperty(ev, 'keyCode', { get: function () { return opts.keyCode; } });
        Object.defineProperty(ev, 'which', { get: function () { return opts.keyCode; } });
      }
      document.dispatchEvent(ev);
      return true;
    } catch (e) { return false; }
  }

  /**
   * 当前题有没有「可缩放的画布」，是什么类型。
   *
   * 为什么不能只看 .lsf-image（踩过的坑）：原来只找 `.lsf-image img`，
   * 于是音频题、视频题、PDF 题一律被当成「纯文本题」——
   *   · 音频题按缩放键得到「纯文本题不支持缩放」，归因是错的；
   *   · 视频题更糟：LSF 的 video 是有缩放的，却被我们提前 return 掉，
   *     热键根本转发不出去，用户会觉得"缩放坏了"。
   * 所以先认出画布类型，再决定转发还是给一句准确的话。
   */
  function canvasKind() {
    if ($('.lsf-image img') || $('.lsf-object img')) return 'image';
    // 音视频的容器 class 是 lsf-video-tag / lsf-audio-tag（实测 DOM 里就是这个），
    // 不是想当然的 .lsf-video / .lsf-audio —— 猜错的表现是：
    // 音频题被当成"没有画布"，按缩放键得到一句与题型无关的提示。
    if ($('.lsf-video-tag') || $('.lsf-video-config') || $('.lsf-object video')) return 'video';
    if ($('.lsf-audio-tag') || $('.lsf-audio-config') || $('.lsf-object audio')) return 'audio';
    // PDF / 时序等用 canvas 或 svg 渲染，缩放由 LSF 自己实现。
    // 必须排在音视频之后：音频波形本身就是 canvas，先判 canvas 会把音频认成 canvas。
    if ($('.lsf-object canvas') || $('.lsf-object svg')) return 'canvas';
    return '';
  }

  function imageWidth() {
    var img = $('.lsf-image img') || $('.lsf-object img');
    return img && img.style ? String(img.style.width || '') : '';
  }

  function zoom(dir) {
    var kind = canvasKind();
    if (!kind) {
      toast('这一题没有可缩放的画布，缩放热键不适用', 'info');
      return;
    }
    if (kind === 'audio') {
      // 音频是波形 + 时间轴，缩放会改变时间轴密度但 LSF 没给这个能力，
      // 与其转发一个无反应的热键，不如直接说清楚。
      toast('音频题不支持缩放', 'info');
      return;
    }

    var before = imageWidth();
    if (dir > 0) nativeHotkey({ key: '+', code: 'Equal', keyCode: 187, ctrlKey: true });
    else if (dir < 0) nativeHotkey({ key: '-', code: 'Minus', keyCode: 189, ctrlKey: true });
    // 0 = 恢复默认 → 「适应窗口」比「100%」更符合"回到刚打开时那样"
    else nativeHotkey({ key: '!', code: 'Digit1', keyCode: 49, shiftKey: true });

    // 复查只在图片题做：视频/PDF 的缩放不体现在 img.style.width 上，
    // 拿它做"没变化"的判断会错报成"没有启用缩放"。
    if (kind !== 'image') return;
    setTimeout(function () {
      if (imageWidth() === before) {
        toast(dir === 0 ? '这题没有启用缩放' : '已到缩放边界，或这题没有启用缩放', 'info');
      } else if (dir === 0) {
        toast('已恢复默认缩放', 'info');
      }
    }, 220);
  }

  // -------------------------------------------------------------------------
  // 快捷键帮助面板
  // -------------------------------------------------------------------------
  function buildHelp() {
    if ($('#fg-ux-help')) return;
    var wrap = el('div');
    wrap.id = 'fg-ux-help';
    var box = el('div', 'box');
    var h3 = el('h3', null, '快捷键与操作');
    var sub = el('div', 'sub', '在输入框里打字时不会触发这些快捷键，可以放心输入。');
    var table = el('table');
    KEYMAP.forEach(function (row) {
      var tr = el('tr');
      var td1 = el('td');
      row.keys.split(' + ').forEach(function (part, i) {
        if (i) td1.appendChild(document.createTextNode(' + '));
        var k = el('kbd', null, part);
        td1.appendChild(k);
      });
      var td2 = el('td', null, row.desc);
      tr.appendChild(td1); tr.appendChild(td2);
      table.appendChild(tr);
    });
    var close = el('button', 'fg-iconbtn close');
    close.type = 'button';
    close.innerHTML = ICO.close;
    on(close, 'click', function () { toggleHelp(false); });
    box.appendChild(h3); box.appendChild(sub); box.appendChild(table); box.appendChild(close);
    wrap.appendChild(box);
    on(wrap, 'click', function (e) { if (e.target === wrap) toggleHelp(false); });
    document.body.appendChild(wrap);
  }

  function toggleHelp(force) {
    buildHelp();
    var node = $('#fg-ux-help');
    S.helpOpen = typeof force === 'boolean' ? force : !S.helpOpen;
    node.classList.toggle('on', S.helpOpen);
  }

  // -------------------------------------------------------------------------
  // 右侧规范 / 示例侧栏
  // -------------------------------------------------------------------------
  function buildGuide() {
    if ($('#fg-ux-guide')) return;
    var wrap = el('div');
    wrap.id = 'fg-ux-guide';
    var hd = el('div', 'hd');
    var left = el('div');
    var b = el('b', null, '标注规范');
    var pill = el('span', 'pill', '示例');
    pill.id = 'fg-ux-guide-pill';
    pill.style.display = 'none';
    left.appendChild(b); left.appendChild(pill);
    var close = el('button', 'fg-iconbtn');
    close.type = 'button';
    close.innerHTML = ICO.close;
    on(close, 'click', function () { toggleGuide(false); });
    hd.appendChild(left); hd.appendChild(close);
    var bd = el('div', 'bd');
    bd.id = 'fg-ux-guide-body';
    bd.innerHTML = '<div class="empty">正在读取本项目的标注规范…</div>';
    wrap.appendChild(hd); wrap.appendChild(bd);
    document.body.appendChild(wrap);
  }

  function toggleGuide(force) {
    buildGuide();
    var node = $('#fg-ux-guide');
    S.guideOpen = typeof force === 'boolean' ? force : !S.guideOpen;
    node.classList.toggle('on', S.guideOpen);
    if (S.guideOpen) loadGuide();
  }

  /**
   * 规范内容从编排层拉。凭据不是平台 JWT（LS 是另一个域，拿不到平台的
   * localStorage），而是 forge_sso 注入到页面里的短期上下文（见 ls_bridge 签票）。
   * 没有上下文或后端没配规范时，明确显示空态，不编造内容。
   */
  var guideLoaded = false;
  function loadGuide() {
    if (guideLoaded) return;
    var ctx = window.__FORGE_CTX__ || null;
    var body = $('#fg-ux-guide-body');
    if (!body) return;
    // 三种「读不到」要分开说，否则用户看到的永远是同一句含糊提示：
    //   没有上下文   → 会话问题，重新进入工作台就好
    //   没有接口地址 → 运维问题（FORGE_PLATFORM_API_BASE 没配）
    //   接口说没配   → 业务问题，管理员去填规范
    if (!ctx || !ctx.token) {
      body.innerHTML = '<div class="empty">未获取到平台会话上下文。<br>'
        + '请返回平台，从「标注工作台」重新进入这一批任务。</div>';
      return;
    }
    if (!ctx.guideline_url) {
      body.innerHTML = '<div class="empty">平台接口地址未配置，暂时无法读取规范。<br>'
        + '请联系系统管理员配置 FORGE_PLATFORM_API_BASE 后重新进入。</div>';
      return;
    }
    var x = new XMLHttpRequest();
    x.open('GET', ctx.guideline_url, true);
    x.setRequestHeader('X-Forge-Ctx', ctx.token);
    x.onload = function () {
      // 只有真正拿到 200 才锁定「已加载」。失败时不锁，用户点一下「规范」
      // 就能重试 —— 网络抖动后必须还能刷出来，不然只能刷新整页。
      if (x.status !== 200) {
        body.innerHTML = '<div class="empty">规范读取失败（HTTP ' + x.status + '）。'
          + (x.status === 401
            ? '会话已过期，请从平台重新进入标注工作台。'
            : '稍后可重试，或联系项目管理员。') + '</div>';
        return;
      }
      var data = null;
      try { data = JSON.parse(x.responseText); } catch (e) { data = null; }
      if (!data) {
        body.innerHTML = '<div class="empty">规范读取失败（返回内容无法解析）。</div>';
        return;
      }
      guideLoaded = true;
      if (!data.configured) {
        body.innerHTML = '<div class="empty">这个项目还没有配置标注规范。<br>'
          + (data.reason || '管理员可在平台的「项目详情 → 标注规范」里填写说明与示例图，保存后这里会自动显示。')
          + '</div>';
        return;
      }
      var html = '';
      if ((data.images || []).length) {
        html += '<h4>示例</h4>';
        data.images.forEach(function (u) {
          html += '<img src="' + String(u).replace(/"/g, '&quot;') + '" alt="示例">';
        });
      }
      if (data.markdown) {
        html += '<h4>规范说明</h4><pre>' + String(data.markdown)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</pre>';
      }
      body.innerHTML = html;
      var pill = $('#fg-ux-guide-pill');
      if (pill) pill.style.display = (data.images || []).length ? '' : 'none';
    };
    x.onerror = function () {
      body.innerHTML = '<div class="empty">规范读取失败（网络异常）。稍后重试。</div>';
    };
    x.send();
  }

  // -------------------------------------------------------------------------
  // 耗时打点
  // -------------------------------------------------------------------------
  /**
   * 计时口径：以「LS 任务切换」为界。每题进入时记开始时间，提交/跳过时上报时长。
   * 上报带 ls_task_id 与 batch（ctx 里），编排层据此入表，可直接做效率与计价。
   * 单题超过 4 小时视为挂机（不算有效时长），上限截断，避免污染均值。
   */
  var MAX_MS = 4 * 3600 * 1000;
  function reportEvent(kind) {
    var h = H();
    if (!h) return;
    var ctx = window.__FORGE_CTX__ || null;
    if (!ctx || !ctx.telemetry_url || !ctx.token) return;
    var lsTaskId = h.task && h.task.id;
    // enter 只表示"开始计时"，本身没有时长。带上一个 0 会让"每题取最后
    // 一条"的统计把真实耗时覆盖成 0（实测踩过：提交后又进入同题 → 0 秒）。
    var dur = (kind === 'enter' || !S.startedAt)
      ? null : Math.min(Date.now() - S.startedAt, MAX_MS);
    var payload = {
      ls_task_id: lsTaskId || null,
      ls_project_id: ctx.ls_project_id || null,
      event: kind,
      duration_ms: dur,
      queue_position: typeof h.queuePosition === 'number' ? h.queuePosition : null,
      queue_total: typeof h.queueTotal === 'number' ? h.queueTotal : null,
      at: new Date().toISOString()
    };
    try {
      var x = new XMLHttpRequest();
      x.open('POST', ctx.telemetry_url, true);
      x.setRequestHeader('Content-Type', 'application/json');
      // 标注页与平台不同源，凭据只能走这个头（编排层据此重建 RLS 上下文）
      x.setRequestHeader('X-Forge-Ctx', ctx.token);
      x.send(JSON.stringify(payload));
    } catch (e) { /* 打点失败不能影响标注 */ }
  }

  function tickTask() {
    var h = H();
    if (!h || !h.task) return;
    var key = String(h.task.id);
    if (key === S.taskKey) return;
    if (S.taskKey) reportEvent('leave');   // 离开上一题
    S.taskKey = key;
    S.startedAt = Date.now();
    refreshCount();
    // 切题瞬间 LSF 自己的计数器可能还没重渲染，先按队列显示了一版；
    // 隔一拍再取一次，保证和底部「第 n / N 条」对上（不然会短暂跳数字）。
    setTimeout(refreshCount, 350);
    reportEvent('enter');
  }

  // -------------------------------------------------------------------------
  // 提交拦截（校验不过就不放行）
  // -------------------------------------------------------------------------
  function installSubmitGuard() {
    on(document, 'click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var hit = t.closest('[data-testid="bottombar-submit-button"], [data-testid="bottombar-submit-dropdown"]');
      if (!hit) return;
      // 下拉里的「提交当前标注」也算提交，统一过校验
      var v = collectValidation();
      if (!v.ok) {
        e.preventDefault();
        e.stopPropagation();
        toast(v.msgs[0] || '校验未通过，无法提交', 'err');
        return;
      }
      var snap = snapshotLabels();
      if (snap) S.lastLabels = snap;
      // 放行：原生提交继续走
    }, true);
  }

  // -------------------------------------------------------------------------
  // 生命周期：只在标注页挂载 / 卸载
  // -------------------------------------------------------------------------
  var mounted = false;
  function mount() {
    if (mounted) return;
    if (!$('.lsf-editor') && !H()) return;
    mounted = true;
    injectCss();
    buildBar();
    fitBar();                 // 建完先量一次：面包屑长的时候直接进紧凑模式
    on(window, 'resize', fitBar);
    buildGuide();
    installSubmitGuard();
    tickTask();
    log('mounted v' + VERSION);
  }
  function unmount() {
    if (!mounted) return;
    mounted = false;
    ['#fg-ux-bar', '#fg-ux-guide', '#fg-ux-help', '#fg-ux-toast'].forEach(function (s) {
      var n = $(s); if (n && n.parentNode) n.parentNode.removeChild(n);
    });
    log('unmounted');
  }

  function boot() {
    // 监听挂在 window 的捕获阶段，不挂 document。
    // 原因（实测踩过）：Esc 关掉帮助面板后，document.activeElement 还指着
    // 刚被 display:none 掉的那个关闭按钮，此时再按 g，事件路径算不出来，
    // document 上的捕获监听就收不到 —— 表现是「关掉帮助后快捷键失灵」。
    // window 是传播路径的根，永远在链路上。
    on(window, 'keydown', onKeyDown, true);
    // 标注页是多标签页 SPA：切 tab / 换任务都不换 document，
    // 所以靠 MutationObserver + 低频轮询双保险来感知「进入/离开标注页」。
    var obs = new MutationObserver(function () { mount(); });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    setInterval(function () {
      if ($('.lsf-editor') || H()) { mount(); tickTask(); }
      else if (mounted) { unmount(); }
      refreshCount();
    }, 800);
    mount();
  }

  if (document.readyState === 'loading') on(document, 'DOMContentLoaded', boot);
  else boot();

  // 自动化测试用：不参与业务逻辑，但缺了它就只能靠截图猜。
  window.__FORGE_UX__ = {
    version: VERSION,
    isMounted: function () { return mounted; },
    toggleHelp: toggleHelp,
    toggleGuide: toggleGuide,
    validation: collectValidation,
    saveDraft: saveDraft,
    reuseLast: reuseLast,
    submitAndNext: submitAndNext,
    lastLabels: function () { return S.lastLabels; },
    diag: function () {
      var h = H();
      return {
        version: VERSION,
        mounted: mounted,
        hasHtx: !!h,
        lsTaskId: h && h.task ? h.task.id : null,
        queue: { pos: h && h.queuePosition, total: h && h.queueTotal },
        countText: ($('#fg-ux-count') || {}).innerText || null,
        barButtons: [].map.call(document.querySelectorAll('#fg-ux-bar .fg-btn'), function (b) {
          return (b.innerText || '').trim() || b.title;
        }),
        // prev/next/skip 现在靠点表格行，不再依赖原生按钮；行数 >1 才算可用。
        // submit 仍看原生提交/更新按钮（有标注时是 update-button）。
        native: {
          prev: rowsNow().length > 1,
          next: rowsNow().length > 1,
          skip: rowsNow().length > 1,
          submit: !!nativeSubmitBtn()
        },
        startedAt: S.startedAt,
        lastValidate: S.lastValidate,
        lastLabels: S.lastLabels,
        // 题型相关的两个事实：排查「必填不提示 / 缩放没反应 / 复用没反应」时
        // 光看界面看不出来，必须能看到增强层自己认成了什么题型、
        // 从 labeling config 里读到了哪些必填控件。
        canvasKind: canvasKind(),
        required: requiredNames(),
        // 按 XML 算出来的缺项：条件分支题里可能含有被隐藏的控件，
        // 与 required 对着看就能判断"是真的没填"还是"控件被 visibleWhen 收起了"
        requiredMissing: S.requiredMissing || [],
        submitBlocked: nativeSubmitBlocked(),
        ctx: window.__FORGE_CTX__ || null
      };
    }
  };
})();
