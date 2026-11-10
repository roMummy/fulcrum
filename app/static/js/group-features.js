/* 功能卡片(群详情的群功能、账号详情的自动收款共用):
 * - 总开关切换时同步文案和卡片明暗;"依赖"某个开关的设置项在该开关关闭时变淡;
 * - 设置区可折叠:点右侧的箭头图标或标题展开/收起,状态记在浏览器里(下次打开保持原样)。
 */
(function () {
  "use strict";

  var STORE_PREFIX = "gf-open:";

  function loadOpen(key, fallback) {
    try {
      var value = localStorage.getItem(STORE_PREFIX + key);
      return value === null ? fallback : value === "1";
    } catch (error) {
      return fallback;
    }
  }

  function saveOpen(key, open) {
    try {
      localStorage.setItem(STORE_PREFIX + key, open ? "1" : "0");
    } catch (error) {
      /* 浏览器禁用存储时只是不记住,不影响使用 */
    }
  }

  document.querySelectorAll("[data-gf-card]").forEach(function (card) {
    var master = card.querySelector("[data-gf-master]");
    var state = card.querySelector("[data-gf-state]");

    function sync() {
      if (!master) return;
      card.classList.toggle("is-on", master.checked);
      if (state) state.textContent = master.checked ? master.dataset.onText : master.dataset.offText;
      card.querySelectorAll("[data-gf-depends]").forEach(function (el) {
        var dependency = card.querySelector('[name="' + el.dataset.gfDepends + '"]');
        el.classList.toggle("is-dim", !!dependency && !dependency.checked);
      });
    }
    card.addEventListener("change", sync);
    sync();

    var toggle = card.querySelector("[data-gf-toggle]");
    var panel = card.querySelector("[data-gf-collapse]");
    if (!toggle || !panel) return;
    var key = card.dataset.gfKey || "";
    var open = loadOpen(key, card.dataset.gfOpen === "1");

    function apply() {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      card.classList.toggle("is-open", open);
    }

    function flip() {
      open = !open;
      apply();
      saveOpen(key, open);
    }

    apply();
    toggle.addEventListener("click", flip);
    var title = card.querySelector("[data-gf-toggle-title]");
    if (title) title.addEventListener("click", flip);
  });
})();
