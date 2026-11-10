/* 朋友圈页面:刷新/加载更早(向手机发起取回并轮询进度)、显示更多(从缓存分页)、图片/视频占位卡片(点击才让手机下载解密并传回)。 */
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

  // ---------- 图片/视频:占位卡片,点击才让手机下载并解密,再传回服务器显示 ----------
  // 朋友圈媒体在微信 CDN 上是加密的,浏览器直接请求会被拒绝,所以必须经手机。已经传回过的图片进入可视区自动显示。
  var MEDIA_POLL_INTERVAL = 2000;
  var MEDIA_POLL_LIMIT = 170; // 约 340 秒,与服务端 330 秒的超时对应

  function setMediaState(tile, state, hint) {
    tile.dataset.state = state;
    tile.classList.toggle("is-loading", state === "loading");
    tile.classList.toggle("is-failed", state === "failed");
    var element = tile.querySelector("[data-media-hint]");
    if (element && hint) element.textContent = hint;
  }

  // 已传回的媒体:图片在卡片里显示缩略图;视频保持占位卡片(提示"点击播放"),都在点击时用放大查看器(lightbox.js)查看。
  function revealMedia(tile) {
    var url = tile.dataset.url;
    if (!url) return;
    if (tile.dataset.mediaType === "video") {
      setMediaState(tile, "ready", "点击播放");
      return;
    }
    if (tile.classList.contains("is-loaded")) return;
    var element = document.createElement("img");
    element.alt = "朋友圈图片";
    element.loading = "lazy";
    var placeholder = tile.innerHTML;
    // 文件损坏或已被清理:回到占位卡片并允许重试
    element.addEventListener("error", function () {
      tile.classList.remove("is-loaded");
      tile.innerHTML = placeholder;
      setMediaState(tile, "failed", "加载失败，点击重试");
    }, { once: true });
    element.src = url;
    tile.classList.add("is-loaded");
    tile.innerHTML = "";
    tile.appendChild(element);
  }

  // 放大查看:同一条动态里已传回的图片/视频可以左右切换。
  function openViewer(tile) {
    if (!window.appLightbox) return;
    var tiles = Array.prototype.filter.call(tile.parentElement.querySelectorAll("[data-moment-media]"), function (item) { return item.dataset.url; });
    window.appLightbox.open(tiles.map(function (item) { return { type: item.dataset.mediaType, url: item.dataset.url }; }), tiles.indexOf(tile));
  }

  // 刚传回来:显示(图片出缩略图)并直接放大打开,省得再点一次。
  function mediaReady(tile, url) {
    tile.dataset.url = url;
    revealMedia(tile);
    openViewer(tile);
  }

  function pollMedia(tile, attempt) {
    var query = "post_id=" + encodeURIComponent(tile.dataset.postId) + "&mid=" + encodeURIComponent(tile.dataset.mid);
    return getJson("/moments/media/status?" + query).then(function (data) {
      if (data.state === "ready") {
        mediaReady(tile, data.url);
      } else if (data.state === "failed") {
        setMediaState(tile, "failed", (data.error || "获取失败") + "，点击重试");
      } else if (attempt < MEDIA_POLL_LIMIT) {
        return new Promise(function (resolve) { setTimeout(resolve, MEDIA_POLL_INTERVAL); }).then(function () { return pollMedia(tile, attempt + 1); });
      } else {
        setMediaState(tile, "failed", "获取超时，点击重试");
      }
    }).catch(function () { setMediaState(tile, "failed", "网络异常，点击重试"); });
  }

  function loadMedia(tile) {
    setMediaState(tile, "loading", "正在从手机获取…");
    window.appPost("/moments/media/load", { post_id: tile.dataset.postId, mid: tile.dataset.mid }, false).then(function (result) {
      var data = result.payload || {};
      if (!result.ok || data.state === "failed") {
        setMediaState(tile, "failed", (data.notice || data.error || "获取失败") + "，点击重试");
        return null;
      }
      if (data.state === "ready") {
        mediaReady(tile, data.url);
        return null;
      }
      return pollMedia(tile, 0);
    }).catch(function () { setMediaState(tile, "failed", "网络异常，点击重试"); });
  }

  function activateMedia(tile) {
    var state = tile.dataset.state;
    if (state === "loading") return;
    if (state === "ready") openViewer(tile);
    else if (canRefresh) loadMedia(tile);
    else toast("只读成员不能让手机获取媒体", "info");
  }

  feed.addEventListener("click", function (event) {
    var tile = event.target.closest("[data-moment-media]");
    if (tile) activateMedia(tile);
  });
  feed.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var tile = event.target.closest && event.target.closest("[data-moment-media]");
    if (tile) {
      event.preventDefault();
      activateMedia(tile);
    }
  });

  var mediaObserver = "IntersectionObserver" in window
    ? new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          mediaObserver.unobserve(entry.target);
          if (entry.target.isConnected && entry.target.dataset.state === "ready" && entry.target.dataset.mediaType === "image") revealMedia(entry.target);
        });
      }, { rootMargin: "300px 0px" })
    : null;

  function watchMedia(root) {
    if (!mediaObserver) return;
    root.querySelectorAll('[data-moment-media][data-state="ready"][data-media-type="image"]').forEach(function (tile) {
      if (!tile.dataset.watched) {
        tile.dataset.watched = "1";
        mediaObserver.observe(tile);
      }
    });
  }

  watchMedia(feed);
  // 刷新后重新渲染、显示更多追加进来的动态:新出现的卡片同样处理
  new MutationObserver(function () { watchMedia(feed); }).observe(feed, { childList: true });

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
