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

  document.addEventListener("click", function (event) {
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
