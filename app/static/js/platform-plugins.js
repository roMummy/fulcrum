/* 平台后台「插件管理」:搜索与筛选、启停开关的卡片状态同步、可见范围弹窗(可搜索租户/套餐)、历史版本回滚、上传安装确认。
 *
 * 启停开关由 switches.js 点击即保存;可见范围弹窗用 fetch 提交;回滚与上传是普通表单提交(服务端跳转并显示提示)。
 * 弹窗是两个共用节点,点哪个插件就按那个插件卡片上的 data-* 填充。
 */
(function () {
  "use strict";

  var grid = document.querySelector("[data-pp-grid]");
  if (!grid) return;

  var cards = Array.prototype.slice.call(grid.querySelectorAll("[data-pp-card]"));
  var csrf = function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  };
  var toast = function (message, tone) { if (window.appToast) window.appToast(message, tone); };
  var ids = function (value) { return String(value || "").split(",").filter(Boolean); };

  /* ---------- 搜索与筛选 ---------- */
  var search = document.querySelector("[data-pp-search]");
  var filterGroup = document.querySelector("[data-pp-filter]");
  var emptyNote = document.querySelector("[data-pp-empty]");

  function applyFilter() {
    var query = (search.value || "").trim().toLowerCase();
    var checked = filterGroup.querySelector("input:checked");
    var mode = checked ? checked.value : "all";
    var shown = 0;
    cards.forEach(function (card) {
      var ok = (!query || card.dataset.search.indexOf(query) !== -1) &&
        (mode === "all" ||
          (mode === "used" && card.dataset.used === "1") ||
          (mode === "stopped" && card.dataset.stopped === "1") ||
          (mode === "limited" && card.dataset.visibility !== "all"));
      card.hidden = !ok;
      if (ok) shown += 1;
    });
    if (emptyNote) emptyNote.hidden = shown > 0;
  }
  search.addEventListener("input", applyFilter);
  filterGroup.addEventListener("change", applyFilter);

  /* ---------- 启停开关:保存成功后同步卡片外观与筛选数据 ---------- */
  document.addEventListener("autosave:done", function (event) {
    var input = event.target;
    var card = input.closest && input.closest("[data-pp-card]");
    if (!card) return;
    var stopped = !input.checked;
    card.classList.toggle("is-off", stopped);
    card.dataset.stopped = stopped ? "1" : "0";
    var chip = card.querySelector("[data-pp-off-chip]");
    if (chip) chip.hidden = !stopped;
  });

  /* ---------- 可见范围弹窗 ---------- */
  var visDialog = document.getElementById("pp-visibility-dialog");
  var seg = visDialog.querySelector("[data-pp-visibility-seg]");
  var hint = visDialog.querySelector("[data-pp-visibility-hint]");
  var count = visDialog.querySelector("[data-pp-count]");
  var pickers = {
    plans: visDialog.querySelector('[data-pp-picker="plans"]'),
    tenants: visDialog.querySelector('[data-pp-picker="tenants"]'),
  };
  var HINTS = {
    all: "所有租户都能看到并使用这个插件。",
    plans: "只有所选套餐下的租户能看到并使用。",
    tenants: "只有所选租户能看到并使用。",
    platform_only: "租户看不到也不能使用,仅平台可见(适合内部或测试中的插件)。",
  };
  var current = null;

  function currentVisibility() {
    var checked = seg.querySelector("input:checked");
    return checked ? checked.value : "all";
  }

  function visibleRows(picker) {
    return Array.prototype.slice.call(picker.querySelectorAll(".pp-pick-row")).filter(function (row) { return !row.hidden; });
  }

  function refreshVisibility() {
    var mode = currentVisibility();
    hint.textContent = HINTS[mode] || "";
    Object.keys(pickers).forEach(function (key) { pickers[key].hidden = key !== mode; });
    var picker = pickers[mode];
    count.textContent = picker ? "已选 " + picker.querySelectorAll("input:checked").length + " 个" : "";
  }

  function filterPicker(picker) {
    var query = (picker.querySelector("[data-pp-picker-search]").value || "").trim().toLowerCase();
    var shown = 0;
    picker.querySelectorAll(".pp-pick-row").forEach(function (row) {
      row.hidden = !!query && row.dataset.text.indexOf(query) === -1;
      if (!row.hidden) shown += 1;
    });
    var none = picker.querySelector("[data-pp-picker-empty]");
    if (none) none.hidden = shown > 0;
  }

  Object.keys(pickers).forEach(function (key) {
    var picker = pickers[key];
    picker.querySelector("[data-pp-picker-search]").addEventListener("input", function () { filterPicker(picker); });
    picker.querySelector("[data-pp-picker-all]").addEventListener("click", function () {
      visibleRows(picker).forEach(function (row) { row.querySelector("input").checked = true; });
      refreshVisibility();
    });
    picker.querySelector("[data-pp-picker-none]").addEventListener("click", function () {
      picker.querySelectorAll("input[type=checkbox]").forEach(function (input) { input.checked = false; });
      refreshVisibility();
    });
    picker.addEventListener("change", refreshVisibility);
  });
  seg.addEventListener("change", refreshVisibility);

  function openVisibility(card) {
    current = card;
    visDialog.querySelector("[data-pp-dialog-plugin]").textContent = card.dataset.name + " · " + card.dataset.code;
    var radio = seg.querySelector('input[value="' + card.dataset.visibility + '"]');
    if (radio) radio.checked = true;
    var selected = { plans: ids(card.dataset.planIds), tenants: ids(card.dataset.tenantIds) };
    Object.keys(pickers).forEach(function (key) {
      pickers[key].querySelector("[data-pp-picker-search]").value = "";
      filterPicker(pickers[key]);
      pickers[key].querySelectorAll("input[type=checkbox]").forEach(function (input) {
        input.checked = selected[key].indexOf(input.value) !== -1;
      });
    });
    refreshVisibility();
    visDialog.showModal();
  }

  visDialog.querySelector("[data-pp-visibility-save]").addEventListener("click", function (event) {
    var button = event.currentTarget;
    var mode = currentVisibility();
    var body = new FormData();
    body.append("visibility", mode);
    if (pickers[mode]) {
      pickers[mode].querySelectorAll("input:checked").forEach(function (input) {
        body.append(mode === "plans" ? "plan_ids" : "tenant_ids", input.value);
      });
    }
    button.disabled = true;
    fetch("/platform/plugins/" + encodeURIComponent(current.dataset.code) + "/visibility", {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf(), "X-Requested-With": "fetch", Accept: "application/json" },
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) { return { ok: response.ok && payload.ok === true, payload: payload }; });
      })
      .then(function (result) {
        if (!result.ok) {
          toast(result.payload.notice || "保存失败,请稍后再试", "danger");
          return;
        }
        current.dataset.visibility = result.payload.visibility;
        current.dataset.planIds = (result.payload.plan_ids || []).join(",");
        current.dataset.tenantIds = (result.payload.tenant_ids || []).join(",");
        var chip = current.querySelector("[data-pp-vis-chip]");
        if (chip) chip.textContent = result.payload.visibility_label;
        visDialog.close();
        applyFilter();
        toast(result.payload.notice || "已保存", "success");
      })
      .catch(function () { toast("网络异常,保存失败", "danger"); })
      .then(function () { button.disabled = false; });
  });

  /* ---------- 历史版本与回滚 ---------- */
  var verDialog = document.getElementById("pp-versions-dialog");
  var verList = verDialog.querySelector("[data-pp-version-list]");

  function submitPost(action, fields) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = action;
    fields.csrf_token = csrf();
    Object.keys(fields).forEach(function (name) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = fields[name];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  function openVersions(card) {
    var versions = [];
    try { versions = JSON.parse(card.dataset.versions || "[]"); } catch (error) { versions = []; }
    verDialog.querySelector("[data-pp-dialog-plugin]").textContent = card.dataset.name + " · " + card.dataset.code;
    verList.textContent = "";
    versions.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "pp-version";
      var copy = document.createElement("div");
      var title = document.createElement("strong");
      title.textContent = "v" + item.version;
      var when = document.createElement("small");
      when.textContent = "存档于 " + item.stored_at;
      copy.appendChild(title);
      copy.appendChild(when);
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-secondary btn-small";
      button.textContent = "回滚到此版本";
      button.addEventListener("click", function () {
        window.appDialog.confirm({
          title: "回滚「" + card.dataset.name + "」?",
          message: "当前运行的版本会换成 v" + item.version + "。当前版本会先自动存档,之后还可以再回滚回来。",
          confirmText: "回滚",
        }).then(function (ok) {
          if (ok) submitPost("/platform/plugins/" + encodeURIComponent(card.dataset.code) + "/rollback", { version_file: item.file });
        });
      });
      row.appendChild(copy);
      row.appendChild(button);
      verList.appendChild(row);
    });
    verDialog.showModal();
  }

  grid.addEventListener("click", function (event) {
    var card = event.target.closest("[data-pp-card]");
    if (!card) return;
    if (event.target.closest("[data-pp-open-visibility]")) openVisibility(card);
    if (event.target.closest("[data-pp-open-versions]")) openVersions(card);
  });

  /* ---------- 上传安装:选好文件后先确认再提交 ---------- */
  var upload = document.querySelector("[data-pp-upload]");
  if (upload) {
    var picker = upload.querySelector("[data-pp-file]");
    upload.querySelector("[data-pp-pick]").addEventListener("click", function () { picker.click(); });
    picker.addEventListener("change", function () {
      var file = picker.files && picker.files[0];
      if (!file) return;
      if (!/\.zip$/i.test(file.name)) {
        picker.value = "";
        toast("请选择 .zip 格式的插件包", "danger");
        return;
      }
      window.appDialog.confirm({
        title: "上传「" + file.name + "」?",
        message: "插件编码不存在会安装(默认仅平台可见),已存在会升级,旧版本自动存档、可随时回滚。\n插件会在服务里运行代码,请只安装可信来源的插件包。",
        confirmText: "上传并生效",
      }).then(function (ok) {
        if (ok) upload.submit();
        else picker.value = "";
      });
    });
  }
})();
