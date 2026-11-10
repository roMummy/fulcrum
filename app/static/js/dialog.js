/*
 * 统一弹窗:确认框与提示框,替代浏览器默认的 confirm / alert。
 *
 * 编程接口(都返回 Promise):
 *   appDialog.confirm({ title, message, points, tone, confirmText, cancelText, matchText, matchLabel }) -> Promise<boolean>
 *   appDialog.alert({ title, message, tone, confirmText }) -> Promise<void>
 *   tone: "default" | "danger";points 是后果清单(字符串数组);
 *   matchText 非空时,用户必须原样输入该文本才能点确认(用于删除等不可恢复操作)。
 *
 * 声明式用法(写在 <form> 或提交按钮上):
 *   data-confirm-mode="confirm" | "match-text"    出现即启用确认
 *   data-confirm-title / data-confirm-message     标题与说明(说明里的换行会保留)
 *   data-confirm-points="后果一|后果二"            后果清单,用竖线分隔
 *   data-confirm-tone="danger"                    红色风险样式(match-text 默认就是 danger)
 *   data-confirm-confirm-text / data-confirm-cancel-text  按钮文案
 *   data-confirm-match-text / data-confirm-match-label    输入确认文本
 *   表单里若有 name="confirmation_name" 的隐藏域,确认通过后会填入用户输入的文本。
 */
(() => {
  const ICONS = {
    danger:
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    default:
      '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg>',
  };

  let dialog = null;
  let els = null;
  // 当前挂起的弹窗:{ resolve, matchText }。同一时刻只会有一个。
  let pending = null;

  /** 首次使用时创建弹窗节点,之后复用。 */
  const ensureDialog = () => {
    if (dialog) return;
    dialog = document.createElement("dialog");
    dialog.className = "app-modal app-dialog";
    dialog.setAttribute("aria-labelledby", "app-dialog-title");
    dialog.setAttribute("aria-describedby", "app-dialog-message");
    dialog.innerHTML = `
      <div class="app-dialog-card">
        <div class="app-dialog-icon" data-role="icon"></div>
        <h2 class="app-dialog-title" id="app-dialog-title" data-role="title"></h2>
        <p class="app-dialog-message" id="app-dialog-message" data-role="message"></p>
        <ul class="app-dialog-points" data-role="points" hidden></ul>
        <label class="app-dialog-match" data-role="match" hidden>
          <span data-role="match-label"></span>
          <input type="text" autocomplete="off" spellcheck="false" data-role="match-input" />
        </label>
        <div class="app-dialog-actions">
          <button type="button" class="btn btn-ghost" data-role="cancel">取消</button>
          <button type="button" class="btn" data-role="confirm">确认</button>
        </div>
      </div>`;
    document.body.appendChild(dialog);
    els = {};
    dialog.querySelectorAll("[data-role]").forEach((node) => {
      els[node.dataset.role] = node;
    });

    els.cancel.addEventListener("click", () => finish(false));
    els.confirm.addEventListener("click", () => finish(true));
    els["match-input"].addEventListener("input", syncConfirmState);
    els["match-input"].addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !els.confirm.disabled) {
        event.preventDefault();
        finish(true);
      }
    });
    // Esc 键、点击遮罩都视为取消。
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(false);
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) finish(false);
    });
  };

  /** 需要输入确认文本时,没输对之前“确认”不可点。 */
  const syncConfirmState = () => {
    if (!pending || !pending.matchText) return;
    els.confirm.disabled = els["match-input"].value.trim() !== pending.matchText;
  };

  const finish = (result) => {
    if (!pending) return;
    const current = pending;
    pending = null;
    const entered = els["match-input"].value.trim();
    if (dialog.open) dialog.close();
    current.resolve(result ? { ok: true, entered } : { ok: false, entered: "" });
  };

  const open = (options, { cancelable }) => {
    ensureDialog();
    // 已有弹窗未处理完时,先按取消收掉,避免 Promise 悬空。
    if (pending) finish(false);
    const tone = options.tone === "danger" ? "danger" : "default";
    const matchText = String(options.matchText || "").trim();

    dialog.dataset.tone = tone;
    els.icon.innerHTML = ICONS[tone];
    els.title.textContent = options.title || (cancelable ? "确认操作" : "提示");
    els.message.textContent = options.message || "";
    els.message.hidden = !options.message;

    const points = Array.isArray(options.points) ? options.points.filter(Boolean) : [];
    els.points.innerHTML = "";
    points.forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      els.points.appendChild(item);
    });
    els.points.hidden = points.length === 0;

    els.match.hidden = !matchText;
    els["match-label"].textContent = options.matchLabel || "请输入以下内容以确认：";
    els["match-input"].value = "";
    els["match-input"].placeholder = matchText;

    els.cancel.hidden = !cancelable;
    els.cancel.textContent = options.cancelText || "取消";
    els.confirm.textContent = options.confirmText || (cancelable ? "确认" : "知道了");
    els.confirm.className = "btn" + (tone === "danger" ? " btn-danger" : "");

    return new Promise((resolve) => {
      pending = { resolve, matchText };
      syncConfirmState();
      dialog.showModal();
      // 需要输入时聚焦输入框;危险操作默认聚焦“取消”,防止回车误确认。
      if (matchText) els["match-input"].focus();
      else if (tone === "danger" && cancelable) els.cancel.focus();
      else els.confirm.focus();
    });
  };

  window.appDialog = {
    confirm: (options = {}) => open(options, { cancelable: true }).then((result) => result.ok),
    alert: (options = {}) => open(options, { cancelable: false }).then(() => undefined),
    /** 内部使用:声明式表单确认需要拿到用户输入的文本。 */
    _confirmWithInput: (options = {}) => open(options, { cancelable: true }),
  };

  /** 从元素的 data-confirm-* 读出弹窗参数。 */
  const optionsFrom = (source) => {
    const data = source.dataset;
    const matchMode = data.confirmMode === "match-text";
    return {
      title: data.confirmTitle || "确认操作",
      message: data.confirmMessage || "请确认是否继续。",
      points: (data.confirmPoints || "").split("|").map((item) => item.trim()).filter(Boolean),
      tone: data.confirmTone || (matchMode ? "danger" : "default"),
      confirmText: data.confirmConfirmText || "",
      cancelText: data.confirmCancelText || "",
      matchText: matchMode ? (data.confirmMatchText || "").trim() : "",
      matchLabel: data.confirmMatchLabel || "",
    };
  };

  const confirmSource = (form, submitter) => {
    const marked = (node) => node && (node.dataset.confirmMode || node.dataset.confirmMessage);
    if (marked(submitter)) return submitter;
    if (marked(form)) return form;
    return null;
  };

  // 捕获阶段统一接管需要确认的表单提交;确认通过后带着原提交按钮重新提交(保留按钮的 name/value)。
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.dataset.confirmBypass === "true") {
        form.dataset.confirmBypass = "";
        return;
      }
      const source = confirmSource(form, event.submitter);
      if (!source) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      window.appDialog._confirmWithInput(optionsFrom(source)).then((result) => {
        if (!result.ok) return;
        const hidden = form.querySelector('input[name="confirmation_name"]');
        if (hidden) hidden.value = result.entered;
        form.dataset.confirmBypass = "true";
        form.requestSubmit(event.submitter || undefined);
      });
    },
    true,
  );
})();
