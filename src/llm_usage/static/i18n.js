/* Shared i18n runtime: English default, 简体中文 switch.
   Catalogs are injected by the FastAPI page renderer as window.__LLM_USAGE_I18N__. */
(function () {
  "use strict";
  var I = window.__LLM_USAGE_I18N__ || { keys: {}, source: {}, labels: {}, units: {} };
  var LOCALE = window.__LLM_USAGE_LOCALE__ || "en";
  var LANG_KEY = "llm-usage-lang";
  var CJK = /[\u3400-\u9fff]/;

  function t(key, params) {
    var value = (I.keys || {})[key];
    if (typeof value !== "string") return key;
    return value.replace(/\{(\w+)\}/g, function (_, name) {
      return params && params[name] !== undefined ? String(params[name]) : "{" + name + "}";
    });
  }

  function tLabel(label) {
    if (!label) return label;
    var exact = (I.labels || {})[label];
    if (exact) return exact;
    var m = /^(\d+)\s*(天|小时|分)$/.exec(String(label).trim());
    if (m) return m[1] + ({ "天": "d", "小时": "h", "分": "m" }[m[2]]);
    return tr(String(label));
  }

  function tUnit(unit) {
    if (!unit) return unit;
    return (I.units || {})[unit] || unit;
  }

  function tr(text) {
    if (LOCALE === "zh-CN" || !text || !CJK.test(text)) return text;
    var raw = String(text);
    var lead = raw.match(/^\s*/)[0];
    var tail = raw.match(/\s*$/)[0];
    var s = raw.trim();
    var exact = (I.source || {})[s] || (I.messages || {})[s];
    if (exact) return lead + exact + tail;
    var rules = [
      [/^认证失败\((\d+)\)$/, "Authentication failed ($1)"],
      [/^请求失败\(HTTP (\d+)\)$/, "Request failed (HTTP $1)"],
      [/^HTTP (\d+)$/, "HTTP $1"],
      [/^(\d+) 个账号$/, "$1 accounts"],
      [/^(\d+) 个平台\((\d+) 个启用\)$/, "$1 platforms ($2 enabled)"],
      [/^(\d+) 个账号，共 (.+) 个账号$/, "$1 accounts of $2"],
      [/^(\d+) 条用量$/, "$1 usage rows"],
      [/^(\d+) 条，共 (.+) 条$/, "$1 of $2 rows"],
      [/^正在显示第 (\d+) 条$/, "Showing item $1"],
      [/^正在轮播第 (\d+) 组$/, "Rotating group $1"],
      [/^(\d+)s 后刷新$/, "$1s until refresh"],
      [/^(\d+)s 后重试$/, "$1s until retry"],
      [/^获取失败：(.*)$/, "Fetch failed: $1"],
      [/^提示：(.*)$/, "Warning: $1"],
      [/^剩余 (.*)$/, "Remaining $1"],
      [/^已用 (.*)$/, "Used $1"],
      [/^限额 (.*)$/, "Limit $1"],
      [/^最大余额 (.*)$/, "Max balance $1"],
      [/^到期时间 (.*)$/, "Expiry time $1"],
      [/^重置时间 (.*)$/, "Reset time $1"],
      [/^已导入 (.*)$/, "Imported $1"],
      [/^添加失败: (.*)$/, "Add failed: $1"],
      [/^导入失败: (.*)$/, "Import failed: $1"],
      [/^顺序保存失败: (.*)$/, "Failed to save order: $1"],
      [/^设置刷新间隔失败: (.*)$/, "Failed to set refresh interval: $1"],
      [/^网络错误: (.*)$/, "Network error: $1"],
      [/^为用户 (.*)$/, "for user $1"],
      [/^第 (\d+) 条$/, "item $1"]
    ];
    for (var i = 0; i < rules.length; i++) {
      if (rules[i][0].test(s)) return lead + s.replace(rules[i][0], rules[i][1]) + tail;
    }
    if (s.indexOf("；") >= 0) {
      return lead + s.split("；").map(function (part) { return tr(part); }).join("; ") + tail;
    }
    if (s.indexOf("：") >= 0) {
      var idx = s.indexOf("：");
      return lead + s.slice(0, idx) + ": " + tr(s.slice(idx + 1)) + tail;
    }
    return text;
  }

  function translateAttrs(el) {
    var attrs = [
      ["data-i18n-title", "title"],
      ["data-i18n-aria-label", "aria-label"],
      ["data-i18n-placeholder", "placeholder"],
      ["data-i18n-alt", "alt"]
    ];
    for (var i = 0; i < attrs.length; i++) {
      var key = el.getAttribute && el.getAttribute(attrs[i][0]);
      if (key) el.setAttribute(attrs[i][1], t(key));
    }
    if (el.hasAttribute && el.hasAttribute("title") && CJK.test(el.getAttribute("title") || "")) {
      el.setAttribute("title", tr(el.getAttribute("title")));
    }
    if (el.hasAttribute && el.hasAttribute("aria-label") && CJK.test(el.getAttribute("aria-label") || "")) {
      el.setAttribute("aria-label", tr(el.getAttribute("aria-label")));
    }
    if (el.hasAttribute && el.hasAttribute("placeholder") && CJK.test(el.getAttribute("placeholder") || "")) {
      el.setAttribute("placeholder", tr(el.getAttribute("placeholder")));
    }
  }

  function apply(root) {
    root = root || document;
    var keyed = root.querySelectorAll ? root.querySelectorAll("[data-i18n]") : [];
    for (var k = 0; k < keyed.length; k++) {
      keyed[k].textContent = t(keyed[k].getAttribute("data-i18n"));
    }
    var all = root.querySelectorAll ? root.querySelectorAll("*") : [];
    for (var i = 0; i < all.length; i++) translateAttrs(all[i]);
    if (LOCALE === "zh-CN") return;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var p = node.parentNode;
        if (!p || !p.tagName) return NodeFilter.FILTER_REJECT;
        var tag = p.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEXTAREA") return NodeFilter.FILTER_REJECT;
        if (p.closest && p.closest("[data-i18n-skip]")) return NodeFilter.FILTER_REJECT;
        return CJK.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var node;
    while ((node = walker.nextNode())) {
      var next = tr(node.nodeValue);
      if (next !== node.nodeValue) node.nodeValue = next;
    }
  }

  function setLanguage(lang) {
    if (lang !== "en" && lang !== "zh-CN") return;
    try { localStorage.setItem(LANG_KEY, lang); } catch (_) {}
    var secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = "llm_usage_lang=" + encodeURIComponent(lang) +
      "; Path=/; Max-Age=31536000; SameSite=Lax" + secure;
    // Update the visible state immediately; the subsequent reload also refreshes
    // the server-rendered catalog and API messages.
    LOCALE = lang;
    window.__LLM_USAGE_LOCALE__ = lang;
    syncLanguageButton();
    syncLanguageSubmenu();
    try { location.reload(); } catch (_) {}
  }

  function syncLanguageButton() {
    var btn = document.getElementById("language-toggle");
    if (!btn) return;
    // Keep the current language explicit even before the reload completes.
    btn.textContent = LOCALE === "zh-CN" ? "语言：简体中文" : "Language: English";
    var sub = document.getElementById("language-submenu");
    if (sub) btn.setAttribute("aria-expanded", sub.hidden ? "false" : "true");
  }

  function syncLanguageSubmenu() {
    var options = document.querySelectorAll("#language-submenu [data-lang]");
    for (var i = 0; i < options.length; i++) {
      var active = options[i].getAttribute("data-lang") === LOCALE;
      options[i].classList.toggle("active", active);
      options[i].setAttribute("aria-checked", active ? "true" : "false");
      var label = options[i].getAttribute("data-lang-label") || "";
      options[i].textContent = (active ? "✓ " : "  ") + label;
    }
  }

  function syncTheme() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var light = document.documentElement.dataset.theme === "light";
    btn.textContent = light ? t("nav.theme.dark") : t("nav.theme.light");
  }

  function toggleTheme() {
    var light = document.documentElement.dataset.theme === "light";
    document.documentElement.dataset.theme = light ? "dark" : "light";
    try { localStorage.setItem("llm-usage-theme", light ? "dark" : "light"); } catch (_) {}
    syncTheme();
  }

  function initMenu() {
    var theme = document.getElementById("theme-toggle");
    if (theme) {
      if (!window.__LLM_USAGE_MENU_MANAGED__) theme.addEventListener("click", toggleTheme);
      syncTheme();
    }
    var btn = document.getElementById("more-menu-btn");
    var dropdown = document.getElementById("more-dropdown");
    if (btn && dropdown && !window.__LLM_USAGE_MENU_MANAGED__) {
      function setOpen(open) {
        dropdown.hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      }
      btn.addEventListener("click", function (e) { e.stopPropagation(); setOpen(dropdown.hidden); });
      dropdown.addEventListener("click", function (e) { if (e.target.closest("#language-toggle") || e.target.closest("#language-submenu")) return; setOpen(false); });
      document.addEventListener("click", function (e) {
        if (!dropdown.hidden && e.target !== btn && !dropdown.contains(e.target)) setOpen(false);
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !dropdown.hidden) setOpen(false);
      });
    }
    var langBtn = document.getElementById("language-toggle");
    var langSub = document.getElementById("language-submenu");
    if (langBtn && langSub) {
      langBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        langSub.hidden = !langSub.hidden;
        syncLanguageButton();
      });
    }
    var options = document.querySelectorAll("#language-submenu [data-lang]");
    for (var i = 0; i < options.length; i++) {
      options[i].addEventListener("click", function () {
        setLanguage(this.getAttribute("data-lang"));
      });
    }
    document.addEventListener("click", function (e) {
      if (!langSub || langSub.hidden) return;
      if (e.target !== langBtn && !langSub.contains(e.target) && e.target !== langBtn) {
        langSub.hidden = true;
        syncLanguageButton();
      }
    });
    syncLanguageButton();
    syncLanguageSubmenu();
  }

  window.t = t;
  window.tr = tr;
  window.tLabel = tLabel;
  window.tUnit = tUnit;
  window.setLanguage = setLanguage;
  window.applyI18n = apply;

  function boot() {
    try { apply(document); } catch (_) {}
    document.documentElement.removeAttribute("data-i18n-pending");
    initMenu();
    var obs = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        if (records[i].type === "characterData") {
          var v = tr(records[i].target.nodeValue);
          if (v !== records[i].target.nodeValue) records[i].target.nodeValue = v;
          continue;
        }
        if (records[i].type === "childList") {
          for (var j = 0; j < records[i].addedNodes.length; j++) {
            var n = records[i].addedNodes[j];
            if (n.nodeType === 1) apply(n);
            else if (n.nodeType === 3) { var v = tr(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; }
          }
        }
      }
    });
    obs.observe(document.body, { childList: true, characterData: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  setTimeout(function () { document.documentElement.removeAttribute("data-i18n-pending"); }, 1500);
})();
