/* 本群插件只在当前卡片同步保存结果；通用展开、开关和保存仍走现有公共组件。 */
(function () {
  "use strict";

  function setValue(card, name, value) {
    const inputs = card.querySelectorAll('[name="' + name + '"]');
    inputs.forEach(function (input) {
      if (input.type === "hidden") return;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value == null ? "" : String(value);
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  function feedback(card, message, error) {
    const output = card.querySelector("[data-scope-feedback]");
    if (!output) return;
    output.textContent = message;
    output.hidden = !message;
    output.classList.toggle("is-error", Boolean(error));
  }

  function sync(card, settings) {
    if (!settings) return;
    card.querySelector("[data-scope-source]").textContent = settings.custom ? "本群已自定义" : "跟随默认";
    card.querySelector("[data-scope-commands]").textContent = settings.triggers.length ? "指令：" + settings.triggers.join("、") : "按消息或插件规则触发";
    card.querySelector("[data-scope-mention]").hidden = !settings.require_mention;
    card.querySelector("[data-scope-config-reset]").hidden = !settings.config_custom;
    card.querySelector("[data-scope-all-reset]").hidden = !settings.custom;
    // 保存也使用服务端规范化后的值回显：空指令恢复默认、数字边界等不会与摘要矛盾。
    settings.commands.forEach(function (command) { setValue(card, "command__" + command.index, command.value); });
    setValue(card, "require_mention", settings.require_mention);
    if (settings.keywords_supported) setValue(card, "keywords", settings.keywords_value);
    settings.config_view.fields.forEach(function (field) { setValue(card, "schema__" + field.name, field.field_type === "boolean" ? field.value : field.render_value); });
    // 平台发送策略与插件字段分别回填，恢复默认时也必须重置开关和隐藏的等待区间。
    const delivery = settings.config_view.delivery;
    setValue(card, "delivery__revoke_enabled", delivery.enabled);
    setValue(card, "delivery__revoke_min_seconds", delivery.min_seconds);
    setValue(card, "delivery__revoke_max_seconds", delivery.max_seconds);
  }

  document.querySelectorAll("[data-group-plugin-settings]").forEach(function (card) {
    card.addEventListener("gf:saved", function (event) {
      sync(card, event.detail.settings);
      feedback(card, event.detail.notice || "本群插件设置已保存", false);
    });
    card.addEventListener("gf:save-failed", function (event) {
      feedback(card, event.detail.notice || "保存失败，请稍后重试", true);
    });
    card.addEventListener("autosave:done", function (event) {
      const custom = Boolean(event.detail.response.custom);
      card.querySelector("[data-scope-source]").textContent = custom ? "本群已自定义" : "跟随默认";
      card.querySelector("[data-scope-all-reset]").hidden = !custom;
      // 开关即时保存后同步导航计数，无需刷新页面才能看到本群的启用数量。
      const count = document.querySelector("[data-group-plugin-count]");
      if (count) count.textContent = document.querySelectorAll("[data-group-plugin-settings] [data-gf-master]:checked").length + "/" + document.querySelectorAll("[data-group-plugin-settings]").length;
      applyFilter();
    });
    const reset = card.querySelector("[data-scope-config-reset]");
    reset.addEventListener("click", async function () {
      reset.disabled = true;
      const body = new FormData();
      body.set("csrf_token", card.elements.namedItem("csrf_token").value);
      try {
        const response = await fetch(reset.dataset.scopeConfigReset, {
          method: "POST", credentials: "same-origin", body: body,
          headers: { "X-CSRF-Token": card.elements.namedItem("csrf_token").value, "X-Requested-With": "fetch", Accept: "application/json" },
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.notice || "恢复失败，请稍后重试");
        sync(card, result.settings);
        feedback(card, result.notice, false);
        if (window.appToast) window.appToast(result.notice, "success");
      } catch (error) {
        feedback(card, error.message || "网络异常，恢复失败", true);
      } finally {
        reset.disabled = false;
      }
    });
  });

  // 本群插件本地即时搜索与启停状态筛选，避免几十个插件翻找不便。
  const filterBar = document.querySelector("[data-group-plugin-filter]");
  const searchInput = filterBar ? filterBar.querySelector("[data-group-plugin-search]") : null;
  const stateButtons = filterBar ? Array.from(filterBar.querySelectorAll("[data-group-plugin-state]")) : [];
  const emptyNotice = document.querySelector("[data-group-plugin-empty]");
  let activeState = "all";

  function applyFilter() {
    if (!filterBar) return;
    const query = String(searchInput ? searchInput.value : "").trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll("[data-group-plugin-settings]").forEach(function (card) {
      const enabled = Boolean(card.querySelector("[data-gf-master]:checked"));
      const haystack = card.dataset.pluginSearch || "";
      const matchQuery = !query || haystack.includes(query);
      const matchState = activeState === "all" || (activeState === "enabled" ? enabled : !enabled);
      const show = matchQuery && matchState;
      card.hidden = !show;
      if (show) visible += 1;
    });
    if (emptyNotice) emptyNotice.hidden = visible > 0;
  }

  if (searchInput) {
    searchInput.addEventListener("input", applyFilter);
  }
  stateButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      activeState = button.dataset.groupPluginState || "all";
      stateButtons.forEach(function (item) {
        item.setAttribute("aria-pressed", String(item === button));
      });
      applyFilter();
    });
  });
})();
