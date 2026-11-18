/* 网络代理模式：只有选择“代理”时才展示地址、认证和绕过配置。 */
(function () {
  "use strict";

  function apply(section) {
    var select = section.querySelector("[data-proxy-mode]");
    if (!select) return;
    var enabled = select.value === "proxy";
    section.querySelectorAll("[data-proxy-field]").forEach(function (field) {
      field.hidden = !enabled;
    });
  }

  document.querySelectorAll("[data-proxy-section]").forEach(function (section) {
    var select = section.querySelector("[data-proxy-mode]");
    if (!select) return;
    select.addEventListener("change", function () { apply(section); });
    apply(section);
  });
})();
