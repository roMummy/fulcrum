/* 触发规则编辑页:选触发类型/回复类型后只显示对应的字段,可以增删多条回复。
 * 被隐藏的字段同时设为 disabled,不会随表单提交(服务端也只读取被选中的类型)。 */
(function () {
  "use strict";

  var MAX_ACTIONS = 6;

  function setGroup(group, visible) {
    group.hidden = !visible;
    group.querySelectorAll("input, select, textarea").forEach(function (el) { el.disabled = !visible; });
  }

  function syncTrigger(form) {
    var select = form.querySelector("[data-tr-trigger-select]");
    if (!select) return;
    form.querySelectorAll("[data-tr-trigger]").forEach(function (group) { setGroup(group, group.dataset.trTrigger === select.value); });
    form.querySelectorAll("[data-tr-vars]").forEach(function (note) { note.hidden = note.dataset.trVars !== select.value; });
  }

  function syncAction(block) {
    var select = block.querySelector("[data-tr-action-type]");
    block.querySelectorAll("[data-tr-action-fields]").forEach(function (group) { setGroup(group, group.dataset.trActionFields === select.value); });
  }

  function blocks(form) { return form.querySelectorAll("[data-tr-action-block]"); }

  function refreshButtons(form) {
    var count = blocks(form).length;
    form.querySelectorAll("[data-tr-remove-action]").forEach(function (button) { button.hidden = count <= 1; });
    var add = form.querySelector("[data-tr-add-action]");
    if (add) add.hidden = count >= MAX_ACTIONS;
  }

  function nextIndex(form) {
    var max = -1;
    form.querySelectorAll('input[name="action_idx"]').forEach(function (input) { max = Math.max(max, parseInt(input.value, 10) || 0); });
    return max + 1;
  }

  document.addEventListener("change", function (event) {
    var form = event.target.closest("[data-tr-form]");
    if (!form) return;
    if (event.target.matches("[data-tr-trigger-select]")) syncTrigger(form);
    if (event.target.matches("[data-tr-action-type]")) syncAction(event.target.closest("[data-tr-action-block]"));
  });

  // 变量标签:点一下插入到最近聚焦过的、允许用变量的输入框里的光标处
  var lastTarget = null;
  document.addEventListener("focusin", function (event) {
    if (event.target.matches && event.target.matches("[data-tr-var-target]")) lastTarget = event.target;
  });

  function insertVariable(button) {
    var target = lastTarget;
    if (!target || target.disabled || !document.contains(target)) {
      if (window.appToast) window.appToast("请先点一下要填写的输入框,再点变量", "danger");
      return;
    }
    var token = button.dataset.trInsert;
    var start = target.selectionStart == null ? target.value.length : target.selectionStart;
    var end = target.selectionEnd == null ? start : target.selectionEnd;
    target.value = target.value.slice(0, start) + token + target.value.slice(end);
    target.focus();
    target.setSelectionRange(start + token.length, start + token.length);
  }

  document.addEventListener("mousedown", function (event) {
    // 点变量标签时不要让输入框失去焦点,否则光标位置会丢
    if (event.target.closest && event.target.closest("[data-tr-insert]")) event.preventDefault();
  });

  document.addEventListener("click", function (event) {
    var insert = event.target.closest && event.target.closest("[data-tr-insert]");
    if (insert) {
      insertVariable(insert);
      return;
    }
    var form = event.target.closest("[data-tr-form]");
    if (!form) return;
    if (event.target.matches("[data-tr-add-action]")) {
      if (blocks(form).length >= MAX_ACTIONS) return;
      var template = document.getElementById("tr-action-template");
      var holder = document.createElement("div");
      holder.innerHTML = template.innerHTML.split("__IDX__").join(String(nextIndex(form)));
      var block = holder.firstElementChild;
      form.querySelector("[data-tr-actions]").appendChild(block);
      syncAction(block);
      refreshButtons(form);
    }
    if (event.target.matches("[data-tr-remove-action]")) {
      if (blocks(form).length <= 1) return;
      event.target.closest("[data-tr-action-block]").remove();
      refreshButtons(form);
    }
  });

  document.querySelectorAll("[data-tr-form]").forEach(function (form) {
    syncTrigger(form);
    blocks(form).forEach(syncAction);
    refreshButtons(form);
  });
})();
