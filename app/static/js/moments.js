/* 朋友圈页面:刷新/加载更早(向手机发起取回并轮询进度)、显示更多(从缓存分页)、失效的预览图换成占位。 */
(function () {
  "use strict";

  var page = document.getElementById("moments-page");
  if (!page) return;

  var channelId = page.dataset.channelId;
  var canRefresh = page.dataset.canRefresh === "1";
  var feed = document.getElementById("moments-feed");
  var moreButton = document.getElementById("moments-more");
  var refreshButton = document.getElementById("moments-refresh");
  var olderButton = document.getElementById("moments-older");
  var runningChip = document.getElementById("moments-running");
  var cachedChip = document.getElementById("moments-cached");
  var refreshedChip = document.getElementById("moments-refreshed");
  var POLL_INTERVAL = 1500;
  var POLL_LIMIT = 50; // 最长轮询约 75 秒,与服务端 120 秒的取回超时留出余量
  var busy = false;

  function toast(message, tone) {
    if (window.appToast) window.appToast(message, tone);
  }

  function lastId() {
    var cards = feed.querySelectorAll(".moment-card");
    return cards.length ? cards[cards.length - 1].dataset.id : "0";
  }

  function getJson(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  // 预览图地址失效(过期或被拒绝)时换成占位,不影响其他内容。error 不冒泡,用捕获阶段接收。
  feed.addEventListener("error", function (event) {
    var image = event.target;
    if (image && image.matches && image.matches("img[data-moment-img]")) image.closest(".moment-thumb").classList.add("is-broken");
  }, true);

  // ---------- 显示更多:从缓存里取下一页 ----------
  function showMore() {
    return getJson("/moments/feed?channel_id=" + encodeURIComponent(channelId) + "&before_id=" + encodeURIComponent(lastId())).then(function (data) {
      if (data.html) feed.insertAdjacentHTML("beforeend", data.html);
      moreButton.hidden = !data.has_more;
      return data;
    });
  }

  if (moreButton) {
    moreButton.addEventListener("click", function () {
      moreButton.disabled = true;
      showMore().catch(function () { toast("加载失败，请稍后再试", "danger"); }).then(function () { moreButton.disabled = false; });
    });
  }

  // ---------- 从手机取回 ----------
  function setBusy(value) {
    busy = value;
    if (runningChip) runningChip.hidden = !value;
    if (refreshButton) refreshButton.disabled = value;
    if (olderButton && value) olderButton.disabled = true;
  }

  function applySummary(info) {
    if (cachedChip) cachedChip.textContent = "已缓存 " + info.cached + " 条";
    if (refreshedChip) refreshedChip.textContent = "最近刷新 " + (info.refreshed_at || "从未");
    if (olderButton) olderButton.disabled = !info.has_older;
  }

  function reloadHead() {
    return getJson("/moments/feed?channel_id=" + encodeURIComponent(channelId)).then(function (data) {
      feed.innerHTML = data.html || '<div class="wechat-empty">还没有缓存的朋友圈。</div>';
      moreButton.hidden = !data.has_more;
    });
  }

  function poll(direction, attempt) {
    return getJson("/moments/status?channel_id=" + encodeURIComponent(channelId)).then(function (info) {
      if (info.state === "running" && attempt < POLL_LIMIT) {
        return new Promise(function (resolve) { setTimeout(resolve, POLL_INTERVAL); }).then(function () { return poll(direction, attempt + 1); });
      }
      applySummary(info);
      if (info.state === "done") {
        var tail = direction === "tail";
        // 刷新最新:整页重新渲染;取更早的:把新取回的接在已显示的后面
        var next = tail ? showMore() : reloadHead();
        return next.then(function () {
          toast(tail ? "又取回 " + info.fetched_count + " 条更早的动态" : "已刷新：新增 " + info.new_count + " 条，共取回 " + info.fetched_count + " 条", "success");
        });
      }
      toast(info.state === "failed" ? info.error || "取回失败，请稍后重试" : "取回超时，请稍后再试", "danger");
    });
  }

  function start(direction) {
    if (busy || !canRefresh) return;
    setBusy(true);
    window.appPost("/moments/refresh", { channel_id: channelId, direction: direction }, false)
      .then(function (result) {
        if (!result.ok) {
          toast(result.payload.notice || "发起失败", "danger");
          return null;
        }
        if (!result.payload.started) {
          toast(result.payload.notice || "没有发起新的取回", "info");
          return null;
        }
        return poll(direction, 0);
      })
      .catch(function () { toast("网络异常，请稍后再试", "danger"); })
      .then(function () { setBusy(false); });
  }

  if (refreshButton) refreshButton.addEventListener("click", function () { start("head"); });
  if (olderButton) olderButton.addEventListener("click", function () { start("tail"); });

  // 页面打开时如果上一次取回还在进行(例如刷新了页面),接着显示进度。
  getJson("/moments/status?channel_id=" + encodeURIComponent(channelId)).then(function (info) {
    if (info.state === "running" && canRefresh) {
      setBusy(true);
      poll("head", 0).catch(function () {}).then(function () { setBusy(false); });
    }
  }).catch(function () {});
})();
