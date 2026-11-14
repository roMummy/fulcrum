/* 猜图片题库的单页管理：复用全站 dialog 外观和焦点管理，业务请求只消费 JSON。
 * 表单失败时不重建 DOM、不清空文件；成功后保留当前筛选及页码并显示统一通知。 */
(() => {
  "use strict";

  const bank = document.querySelector("[data-gi-bank]");
  const questionDialog = document.getElementById("gi-question-dialog");
  if (!bank || !questionDialog) return;
  const base = bank.dataset.base;
  const byId = (id) => document.getElementById(id);
  const questionForm = byId("gi-question-form");
  const fields = byId("gi-question-fields");
  const saveButton = byId("gi-question-save");
  const deleteButton = byId("gi-question-delete");
  const retryQuestion = byId("gi-question-retry");
  const questionError = byId("gi-question-error");
  const questionLoading = byId("gi-question-loading");
  const imageInput = byId("gi-image-input");
  const imagePreview = byId("gi-image-preview");
  const imagePlaceholder = byId("gi-image-placeholder");
  const importDialog = byId("gi-import-dialog");
  const previewForm = byId("gi-import-preview-form");
  const confirmForm = byId("gi-import-confirm-form");
  const importPreview = byId("gi-import-preview");
  const importError = byId("gi-import-error");
  const auditDialog = byId("gi-audit-dialog");
  const auditError = byId("gi-audit-error");
  let questionId = "";
  let originalImage = "";
  let localImage = "";
  let detailRequest = null;
  let auditRequest = null;

  const clearError = (element) => {
    element.hidden = true;
    element.textContent = "";
  };
  const showError = (element, error) => {
    element.textContent = error.message || "操作失败，请稍后重试。";
    element.hidden = false;
    element.focus();
  };
  const refresh = (notice) => {
    const url = new URL(window.location.href);
    url.searchParams.set("notice", notice);
    url.hash = "";
    window.location.assign(url.href);
  };

  async function request(url, options = {}) {
    let response;
    try {
      response = await fetch(url, {
        ...options, credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json", "X-Requested-With": "fetch" },
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("网络连接失败，请检查连接后重试。");
    }
    // 会话失效可能由认证中间件跳转到登录页；留在弹窗提示，避免丢失尚未保存的输入。
    if (response.redirected || !response.headers.get("content-type")?.includes("application/json")) {
      throw new Error("页面状态已变化，请检查登录状态后重试。");
    }
    const data = await response.json();
    if (!response.ok) {
      const messages = {
        401: "登录已失效，请重新登录后重试。",
        403: "当前操作未获授权，请检查登录状态。",
        404: "题目或插件已不可用，请刷新题库后重试。",
      };
      throw new Error(messages[response.status] || (typeof data.detail === "string" ? data.detail : "操作失败，请稍后重试。"));
    }
    return data;
  }

  async function withBusy(dialog, submitter, action) {
    // 提交前先构造 FormData，再锁定控件；保存结束后恢复原禁用状态，不能误启用加载失败的表单。
    const controls = [...dialog.querySelectorAll("button, input, select, textarea, fieldset")];
    const states = controls.map((control) => control.disabled);
    dialog.dataset.busy = "true";
    dialog.setAttribute("aria-busy", "true");
    controls.forEach((control) => { control.disabled = true; });
    submitter?.classList.add("is-busy");
    try {
      return await action();
    } finally {
      controls.forEach((control, index) => { control.disabled = states[index]; });
      submitter?.classList.remove("is-busy");
      delete dialog.dataset.busy;
      dialog.removeAttribute("aria-busy");
    }
  }

  [questionDialog, importDialog].forEach((dialog) => {
    // 写入期间阻止 Escape 和遮罩关闭，避免请求尚未结束时重新打开表单而重复创建题目。
    dialog.addEventListener("cancel", (event) => {
      if (dialog.dataset.busy === "true") event.preventDefault();
    });
    dialog.addEventListener("click", (event) => {
      if (dialog.dataset.busy === "true" && event.target === dialog) event.stopImmediatePropagation();
    }, true);
  });

  function releaseLocalImage() {
    if (localImage) URL.revokeObjectURL(localImage);
    localImage = "";
  }

  function displayImage(url) {
    imagePreview.hidden = !url;
    imagePlaceholder.hidden = Boolean(url);
    if (url) imagePreview.src = url;
    else imagePreview.removeAttribute("src");
  }

  function syncLicenseRequired() {
    const enabled = questionForm.elements.namedItem("status").value === "enabled";
    ["source", "license"].forEach((name) => { questionForm.elements.namedItem(name).required = enabled; });
  }

  function resetQuestion(identifier = "") {
    detailRequest?.abort();
    detailRequest = null;
    releaseLocalImage();
    questionForm.reset();
    questionId = identifier;
    originalImage = "";
    fields.disabled = false;
    saveButton.disabled = false;
    deleteButton.hidden = true;
    retryQuestion.hidden = true;
    questionLoading.hidden = true;
    clearError(questionError);
    imageInput.required = !identifier;
    imageInput.setCustomValidity("");
    questionForm.action = identifier ? `${base}/questions/${encodeURIComponent(identifier)}` : `${base}/new`;
    byId("gi-question-title").textContent = identifier ? "编辑题目" : "添加题目";
    byId("gi-image-label").textContent = identifier ? "替换图片（不选则保留）" : "题目图片";
    displayImage("");
    syncLicenseRequired();
    // 原生 select 的赋值不会自动同步全站增强控件，显式派发 change 保持显示和提交一致。
    questionForm.elements.namedItem("status").dispatchEvent(new Event("change", { bubbles: true }));
    questionDialog.querySelector(".app-modal-card").scrollTop = 0;
  }

  async function loadQuestion(identifier) {
    resetQuestion(identifier);
    const pending = new AbortController();
    detailRequest = pending;
    fields.disabled = true;
    saveButton.disabled = true;
    questionLoading.hidden = false;
    try {
      const data = await request(questionForm.action, { signal: pending.signal });
      // 关闭或切换题目后，较晚返回的详情不能覆盖新表单，也不能重新展示旧图片。
      if (detailRequest !== pending || !questionDialog.open) return;
      ["answer", "category", "tags", "source", "license", "status"].forEach((name) => {
        questionForm.elements.namedItem(name).value = data[name];
      });
      originalImage = data.image_url;
      displayImage(originalImage);
      fields.disabled = false;
      saveButton.disabled = false;
      deleteButton.hidden = false;
      questionForm.elements.namedItem("status").dispatchEvent(new Event("change", { bubbles: true }));
      questionForm.elements.namedItem("answer").focus();
    } catch (error) {
      if (error.name === "AbortError" || detailRequest !== pending) return;
      showError(questionError, error);
      retryQuestion.hidden = false;
    } finally {
      if (detailRequest === pending) questionLoading.hidden = true;
    }
  }

  document.querySelector("[data-gi-create]").addEventListener("click", () => {
    if (questionDialog.dataset.busy === "true") return;
    resetQuestion();
    imageInput.focus();
  });
  document.querySelectorAll("[data-gi-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      if (questionDialog.dataset.busy !== "true") loadQuestion(button.dataset.giEdit);
    });
  });
  retryQuestion.addEventListener("click", () => loadQuestion(questionId));
  questionDialog.addEventListener("close", () => {
    detailRequest?.abort();
    detailRequest = null;
    releaseLocalImage();
  });
  questionForm.elements.namedItem("status").addEventListener("change", syncLicenseRequired);
  imageInput.addEventListener("change", () => {
    releaseLocalImage();
    imageInput.setCustomValidity("");
    clearError(questionError);
    const file = imageInput.files[0];
    if (file && file.size > 5 * 1024 * 1024) {
      imageInput.setCustomValidity("图片必须在 5 MiB 以内");
      displayImage(originalImage);
      showError(questionError, new Error("图片必须在 5 MiB 以内，请重新选择。"));
      return;
    }
    localImage = file ? URL.createObjectURL(file) : "";
    displayImage(localImage || originalImage);
  });
  imagePreview.addEventListener("error", () => {
    imagePreview.hidden = true;
    imagePlaceholder.hidden = false;
  });

  questionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (questionDialog.dataset.busy === "true" || saveButton.disabled) return;
    const body = new FormData(questionForm);
    clearError(questionError);
    try {
      const result = await withBusy(questionDialog, saveButton, () => request(questionForm.action, { method: "POST", body }));
      refresh(result.notice);
    } catch (error) { showError(questionError, error); }
  });
  deleteButton.addEventListener("click", async () => {
    const identifier = questionId;
    const confirmed = await window.appDialog.confirm({
      title: "删除这道题目？", message: "删除后无法从题库恢复，进行中的游戏仍可使用原图片。",
      tone: "danger", confirmText: "删除题目",
    });
    if (!confirmed || !questionDialog.open || questionId !== identifier || questionDialog.dataset.busy === "true") return;
    const body = new FormData();
    body.set("csrf_token", questionForm.elements.namedItem("csrf_token").value);
    clearError(questionError);
    try {
      const result = await withBusy(questionDialog, deleteButton, () => request(`${base}/questions/${encodeURIComponent(identifier)}/delete`, { method: "POST", body }));
      refresh(result.notice);
    } catch (error) { showError(questionError, error); }
  });

  function selectArchive() {
    previewForm.hidden = false;
    importPreview.hidden = true;
    confirmForm.elements.namedItem("token").value = "";
    byId("gi-import-samples").replaceChildren();
    clearError(importError);
    importDialog.querySelector(".app-modal-card").scrollTop = 0;
    previewForm.elements.namedItem("archive").focus();
  }

  document.querySelector('[data-modal-open="gi-import-dialog"]').addEventListener("click", () => {
    previewForm.reset();
    selectArchive();
  });
  byId("gi-import-back").addEventListener("click", selectArchive);
  previewForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (importDialog.dataset.busy === "true") return;
    clearError(importError);
    const archive = previewForm.elements.namedItem("archive").files[0];
    if (archive && archive.size > 20 * 1024 * 1024) {
      showError(importError, new Error("题包必须在 20 MiB 以内，请重新选择。"));
      return;
    }
    const body = new FormData(previewForm);
    try {
      const result = await withBusy(importDialog, event.submitter, () => request(previewForm.action, { method: "POST", body }));
      confirmForm.elements.namedItem("token").value = result.token;
      byId("gi-import-summary").textContent = `校验通过，共 ${result.report.total} 道题；${result.report.skip} 个已有 ID 将跳过。`;
      // 题包内容属于用户输入，使用文本节点渲染样例，不能把答案或分类插入 innerHTML。
      const samples = byId("gi-import-samples");
      samples.replaceChildren();
      result.report.samples.forEach((sample) => {
        const row = document.createElement("tr");
        [sample.answer, sample.category || "未分类", { draft: "草稿", enabled: "已启用", disabled: "已停用" }[sample.status]].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.appendChild(cell);
        });
        samples.appendChild(row);
      });
      previewForm.hidden = true;
      importPreview.hidden = false;
      importDialog.querySelector(".app-modal-card").scrollTop = 0;
      confirmForm.querySelector('button[type="submit"]').focus({ preventScroll: true });
    } catch (error) { showError(importError, error); }
  });
  confirmForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (importDialog.dataset.busy === "true") return;
    const body = new FormData(confirmForm);
    clearError(importError);
    try {
      const result = await withBusy(importDialog, event.submitter, () => request(confirmForm.action, { method: "POST", body }));
      refresh(result.notice);
    } catch (error) { showError(importError, error); }
  });

  async function loadAudit() {
    auditRequest?.abort();
    const pending = new AbortController();
    auditRequest = pending;
    clearError(auditError);
    byId("gi-audit-results").hidden = true;
    byId("gi-audit-loading").hidden = false;
    byId("gi-audit-retry").disabled = true;
    try {
      const result = await request(`${base}/audit`, { signal: pending.signal });
      if (auditRequest !== pending || !auditDialog.open) return;
      auditDialog.querySelectorAll("[data-gi-audit]").forEach((element) => {
        element.textContent = result[element.dataset.giAudit];
      });
      byId("gi-audit-results").hidden = false;
    } catch (error) {
      if (error.name !== "AbortError" && auditRequest === pending) showError(auditError, error);
    } finally {
      if (auditRequest === pending) {
        byId("gi-audit-loading").hidden = true;
        byId("gi-audit-retry").disabled = false;
      }
    }
  }
  document.querySelector('[data-modal-open="gi-audit-dialog"]').addEventListener("click", loadAudit);
  byId("gi-audit-retry").addEventListener("click", loadAudit);
  auditDialog.addEventListener("close", () => auditRequest?.abort());
})();
