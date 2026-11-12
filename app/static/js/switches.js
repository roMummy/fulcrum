/* 点击即保存的开关:在 <input type="checkbox" role="switch"> 或 <input type="radio"> 上声明 data-autosave-* 即可。
 *
 *   data-autosave-url     必填,提交地址(POST)
 *   data-autosave-key     携带取值的字段名,默认 "value"
 *   data-autosave-on/off  开/关时的取值(字符串);默认 JSON 用 true/false,表单用 "true"/"false";单选框取自身的 value
 *   data-autosave-json    额外的 JSON 字段(对象)—— 以 JSON 提交
 *   data-autosave-form    额外的表单字段(对象)—— 以表单提交
 *
 * 服务端返回 JSON:{ok, notice, ...}。成功:弹出 notice 并触发 autosave:done;失败:开关回到原状态并弹出错误提示(autosave:failed)。
 * 提交期间开关不可再点,避免连点造成状态错乱。CSRF 令牌取自页面 <meta name="csrf-token">。
 */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function toast(message, tone) {
    if (window.appToast) window.appToast(message, tone);
  }

  function parseObject(raw) {
    if (!raw) return null;
    try {
      var value = JSON.parse(raw);
      return value && typeof value === "object" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function post(url, data, asJson) {
    var headers = { "X-CSRF-Token": csrfToken(), "X-Requested-With": "fetch", Accept: "application/json" };
    var body;
    if (asJson) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(data);
    } else {
      body = new URLSearchParams(data).toString();
      headers["Content-Type"] = "application/x-www-form-urlencoded";
    }
    return fetch(url, { method: "POST", headers: headers, body: body, credentials: "same-origin" }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        return { ok: response.ok && payload.ok === true, payload: payload, status: response.status };
      });
    });
  }

  function buildRequest(input) {
    var asJson = !input.dataset.autosaveForm;
    var extra = parseObject(input.dataset.autosaveJson) || parseObject(input.dataset.autosaveForm) || {};
    var key = input.dataset.autosaveKey || "value";
    var value;
    if (input.type === "radio") {
      value = input.value;
    } else if (input.checked) {
      value = input.dataset.autosaveOn !== undefined ? input.dataset.autosaveOn : asJson ? true : "true";
    } else {
      value = input.dataset.autosaveOff !== undefined ? input.dataset.autosaveOff : asJson ? false : "false";
    }
    var data = Object.assign({}, extra);
    data[key] = value;
    return { data: data, asJson: asJson };
  }

  function groupOf(input) {
    return input.type === "radio" && input.name ? document.querySelectorAll('input[type="radio"][name="' + input.name + '"]') : [input];
  }

  function remember(input) {
    groupOf(input).forEach(function (item) { item.dataset.autosaveWas = item.checked ? "1" : "0"; });
  }

  function restore(input) {
    groupOf(input).forEach(function (item) { item.checked = item.dataset.autosaveWas === "1"; });
  }

  async function onChange(event) {
    var input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.dataset.autosaveUrl) return;
    var request = buildRequest(input);
    var holders = groupOf(input);
    holders.forEach(function (item) { item.disabled = true; });
    var wrapper = input.closest(".ui-switch");
    if (wrapper) wrapper.classList.add("is-busy");
    // 首次绑定才询问，普通启停继续沿用已保存的群范围。
    if (input.dataset.pluginBinding === "new" && input.checked) {
      request.data.group_default_enabled = window.appDialog ? await window.appDialog.confirm({
        title: "是否对所有群开放？",
        message: "选“是”后，此插件将对该机器人的所有群开放，包括以后同步的新群。选“否”后仅绑定插件，再到群管理中选择需要启用的群。群本身也需要处于正常运行状态。",
        confirmText: "是，对所有群开放",
        cancelText: "否，稍后选择群聊"
      }) : false;
    }
    post(input.dataset.autosaveUrl, request.data, request.asJson)
      .then(function (result) {
        if (result.ok) {
          if (result.payload.is_bound && input.dataset.pluginBinding) input.dataset.pluginBinding = "bound";
          remember(input);
          toast(result.payload.notice || "已保存", "success");
          input.dispatchEvent(new CustomEvent("autosave:done", { bubbles: true, detail: { response: result.payload, checked: input.checked } }));
        } else {
          restore(input);
          toast(result.payload.notice || result.payload.detail || "保存失败，请稍后再试", "danger");
          input.dispatchEvent(new CustomEvent("autosave:failed", { bubbles: true, detail: { response: result.payload } }));
        }
      })
      .catch(function () {
        restore(input);
        toast("网络异常，保存失败", "danger");
        input.dispatchEvent(new CustomEvent("autosave:failed", { bubbles: true, detail: {} }));
      })
      .then(function () {
        holders.forEach(function (item) { item.disabled = false; });
        if (wrapper) wrapper.classList.remove("is-busy");
      });
  }

  document.querySelectorAll("input[data-autosave-url]").forEach(remember);
  // 事件委托:动态插入的开关也生效;change 在 capture 阶段之前不需要处理,直接冒泡接收。
  document.addEventListener("change", onChange);

  window.appPost = post;
})();
