/* 密码仅在用户主动点击时显示；脚本未加载时保留完整原生密码输入，不显示失效按钮。 */
document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
  const input = document.getElementById(toggle.getAttribute("aria-controls"));
  if (!input) return;
  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    toggle.textContent = visible ? "隐藏" : "显示";
    toggle.setAttribute("aria-pressed", String(visible));
    toggle.setAttribute("aria-label", `${visible ? "隐藏" : "显示"}${toggle.dataset.fieldLabel}`);
  });
});

/* 原生校验通过后再锁定提交；浏览器后退恢复页面时解除锁定，避免表单永久不可用。 */
const accessForms = document.querySelectorAll(".access-form form");
accessForms.forEach((form) => {
  form.addEventListener("submit", () => {
    form.setAttribute("aria-busy", "true");
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      // 同时给出可见提交状态，页面返回时再恢复原文案，不能把等待只表现为颜色变灰。
      button.dataset.submitLabel = button.textContent;
      button.textContent = "正在提交…";
      button.disabled = true;
    });
  });
});
window.addEventListener("pageshow", () => {
  accessForms.forEach((form) => {
    form.removeAttribute("aria-busy");
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      button.disabled = false;
      if (button.dataset.submitLabel) {
        button.textContent = button.dataset.submitLabel;
        delete button.dataset.submitLabel;
      }
    });
  });
});
