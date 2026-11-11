/* 功能卡片(群详情的群功能页):
 * - 开关、单选框的"点击即保存"由 switches.js 处理(data-autosave-*);这里只同步卡片的明暗,失败回退时也同步;
 * - 设置区可折叠:点右侧的箭头图标或标题展开/收起,状态记在浏览器里(下次打开保持原样);
 * - 设置区里有文本/数字/密钥输入时,点「保存」用 fetch 提交(不刷新页面),并弹出提示。
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

  function toast(message, tone) {
    if (window.appToast) window.appToast(message, tone);
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  document.querySelectorAll("[data-gf-card]").forEach(function (card) {
    var master = card.querySelector("[data-gf-master]");
    var state = card.querySelector("[data-gf-state]");

    function sync() {
      if (master) {
        card.classList.toggle("is-on", master.checked);
        if (state) state.textContent = master.checked ? master.dataset.onText : master.dataset.offText;
      }
      card.querySelectorAll("[data-gf-depends]").forEach(function (el) {
        var dependency = card.querySelector('input[type="checkbox"][name="' + el.dataset.gfDepends + '"]');
        el.classList.toggle("is-dim", !!dependency && !dependency.checked);
      });
      // 运行模式:选中的那一项高亮
      card.querySelectorAll(".gf-mode").forEach(function (label) {
        var radio = label.querySelector('input[type="radio"]');
        if (radio) label.classList.toggle("is-selected", radio.checked);
      });
    }
    card.addEventListener("change", sync);
    card.addEventListener("autosave:failed", sync);
    sync();

    var saveUrl = card.dataset.gfSaveUrl;
    if (saveUrl) {
      card.addEventListener("submit", function (event) {
        event.preventDefault();
        var button = card.querySelector('.gf-foot button[type="submit"]');
        if (button) button.disabled = true;
        fetch(saveUrl, {
          method: "POST",
          body: new FormData(card),
          credentials: "same-origin",
          headers: { "X-CSRF-Token": csrfToken(), "X-Requested-With": "fetch", Accept: "application/json" },
        })
          .then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) { return { ok: response.ok && payload.ok === true, payload: payload }; });
          })
          .then(function (result) {
            if (!result.ok) {
              toast(result.payload.notice || "保存失败，请稍后再试", "danger");
              return;
            }
            toast(result.payload.notice || "已保存", "success");
            // 密钥只保存不回显:保存后清空输入框,并把提示改成"已配置"。
            card.querySelectorAll('input[type="password"]').forEach(function (input) {
              if (input.value) {
                input.value = "";
                input.placeholder = "已配置，留空则保持不变";
              }
            });
          })
          .catch(function () { toast("网络异常，保存失败", "danger"); })
          .then(function () { if (button) button.disabled = false; });
      });
    }

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
