/* 统一提示条:window.appToast(message, tone)。tone: success(默认)/ danger / info。
 * 点击即保存的开关、设置保存成功或失败都用它提示;不打断操作,几秒后自动消失。
 */
(function () {
  "use strict";

  var host = null;

  function ensureHost() {
    if (host && document.body.contains(host)) return host;
    host = document.createElement("div");
    host.className = "app-toast-host";
    host.setAttribute("aria-live", "polite");
    host.setAttribute("role", "status");
    document.body.appendChild(host);
    return host;
  }

  window.appToast = function (message, tone) {
    var kind = tone === "danger" || tone === "info" ? tone : "success";
    var toast = document.createElement("div");
    toast.className = "app-toast app-toast-" + kind;
    var icon = document.createElement("span");
    icon.className = "app-toast-icon";
    icon.setAttribute("aria-hidden", "true");
    var text = document.createElement("span");
    text.textContent = message;
    toast.append(icon, text);
    ensureHost().appendChild(toast);
    // 下一帧再加 is-visible,触发进入动画。
    requestAnimationFrame(function () { toast.classList.add("is-visible"); });
    var lifetime = kind === "danger" ? 4500 : 2400;
    setTimeout(function () {
      toast.classList.remove("is-visible");
      setTimeout(function () { toast.remove(); }, 250);
    }, lifetime);
  };
})();
