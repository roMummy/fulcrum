(() => {
  const panel = document.querySelector("[data-webridge-connect]");
  if (!panel) return;
  const address = document.getElementById("webridge-connection-url");
  const copyStatus = document.getElementById("webridge-copy-status");
  const transport = panel.querySelector("[data-webridge-transport]");
  if (transport) {
    transport.addEventListener("change", () => {
      address.value = transport.selectedOptions[0].dataset.address;
      copyStatus.textContent = "";
      document.getElementById("webridge-address-help").textContent = transport.value === "ws"
        ? "在手机微桥设置中选择「WebSocket」，将完整地址粘贴到「WebSocket 服务端地址」，并打开总开关。请保留 /ws 路径，不要追加 /agent 或查询参数。地址内含专属 Token，请只用于这个账号。"
        : "在手机微桥设置中选择「长轮询」，将完整地址粘贴到「服务端地址」，并打开总开关。手机需要能访问本服务。地址内含专属 Token，请只用于这个账号。";
    });
  }
  panel.querySelector("[data-copy-address]").addEventListener("click", async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(address.value);
      } else {
        // 局域网 HTTP 没有 Clipboard API，保留浏览器原生复制及手动选择兜底。
        address.focus();
        address.select();
        if (!document.execCommand("copy")) throw new Error("copy_failed");
      }
      copyStatus.textContent = panel.dataset.copyStatus || "地址已复制，请粘贴到微桥插件的服务端地址。";
    } catch (_) {
      address.focus();
      address.select();
      copyStatus.textContent = "浏览器未允许自动复制，地址已选中，请手动复制。";
    }
  });

  const confirm = panel.querySelector("[data-connection-confirm]");
  const error = panel.querySelector("[data-connection-error]");
  let stopped = false;
  window.addEventListener("pagehide", () => { stopped = true; });
  const refresh = async () => {
    if (stopped) return;
    try {
      const response = await fetch(panel.dataset.statusUrl, {
        credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
      });
      if (!response.ok || response.redirected) throw new Error("status_failed");
      const { connection, account } = await response.json();
      const connected = Boolean(account.account_id);
      panel.querySelector("[data-connection-state]").textContent = connection.label;
      const steps = document.querySelectorAll(".connection-step");
      if (steps.length === 3) steps[2].classList.toggle("is-active", ["verified", "active"].includes(connection.state));
      panel.querySelector("[data-connection-waiting]").hidden = connected;
      panel.querySelector("[data-connection-account]").hidden = !connected;
      panel.querySelector("[data-account-id]").textContent = account.account_id || "";
      panel.querySelector("[data-account-nickname]").textContent = account.nickname || panel.dataset.nicknamePending || "正在读取，暂未取得昵称";
      panel.querySelector("[data-account-version]").textContent = account.runtime_version || "暂未上报";
      confirm.elements.expected_account_id.value = account.account_id || "";
      confirm.hidden = connection.state !== "verified";
      panel.querySelector("[data-connection-manage]").hidden = connection.state !== "active";
      const avatar = panel.querySelector("[data-account-avatar]");
      avatar.hidden = !account.avatar_url;
      if (account.avatar_url) avatar.querySelector("img").src = account.avatar_url;
      else avatar.querySelector("img").removeAttribute("src");
      error.textContent = "";
      if (connection.state === "active") return;
    } catch (_) {
      error.textContent = "暂时无法读取连接状态，请检查登录状态和网络，或刷新页面重试。";
    }
    if (!stopped) window.setTimeout(refresh, 2000);
  };
  refresh();
})();
