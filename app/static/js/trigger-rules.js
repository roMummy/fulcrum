/* 触发规则编辑页:选触发类型/回复类型后只显示对应的字段,可以增删多条回复。
 * 被隐藏的字段同时设为 disabled,不会随表单提交(服务端也只读取被选中的类型)。 */
(function () {
  "use strict";

  var MAX_ACTIONS = 6;

  function setGroup(group, visible) {
    group.hidden = !visible;
    group.querySelectorAll("input, select, textarea").forEach(function (el) { el.disabled = !visible; });
  }

  function checkedValue(container) {
    var input = container && container.querySelector("input:checked");
    return input ? input.value : "";
  }

  function syncTrigger(form) {
    var value = checkedValue(form.querySelector("[data-tr-trigger-select]"));
    form.querySelectorAll("[data-tr-trigger]").forEach(function (group) { setGroup(group, group.dataset.trTrigger === value); });
    form.querySelectorAll("[data-tr-vars]").forEach(function (note) { note.hidden = note.dataset.trVars !== value; });
  }

  function syncAction(block) {
    var value = checkedValue(block.querySelector("[data-tr-action-type]"));
    block.querySelectorAll("[data-tr-action-fields]").forEach(function (group) { setGroup(group, group.dataset.trActionFields === value); });
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
    if (event.target.closest("[data-tr-trigger-select]")) syncTrigger(form);
    if (event.target.closest("[data-tr-action-type]")) syncAction(event.target.closest("[data-tr-action-block]"));
    // 文件选择器:选好文件后把文件名显示出来
    if (event.target.type === "file") {
      var label = event.target.closest(".ui-file");
      var nameEl = label && label.querySelector("[data-ui-file-name]");
      if (nameEl) {
        var file = event.target.files && event.target.files[0];
        nameEl.textContent = file ? file.name : nameEl.dataset.empty;
        label.classList.toggle("has-file", !!file);
        var remove = event.target.closest("[data-tr-file]").querySelector("[data-tr-remove]");
        if (file && remove) remove.value = "";
      }
    }
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
    var clear = event.target.closest("[data-tr-file-clear]");
    if (clear) {
      // 去掉已上传的文件:标记删除,保存时才真正生效;再点一次撤销
      var wrap = clear.closest("[data-tr-file]");
      var remove = wrap.querySelector("[data-tr-remove]");
      var nameEl = wrap.querySelector("[data-ui-file-name]");
      var removing = remove.value !== "1";
      remove.value = removing ? "1" : "";
      wrap.classList.toggle("is-removed", removing);
      clear.textContent = removing ? "撤销去掉" : "去掉这个文件";
      nameEl.textContent = removing ? "保存后将去掉这个文件" : "已上传,重新选择可替换";
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
