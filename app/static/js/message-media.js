/* 消息中心的媒体占位卡片(图片/视频/语音/表情/文件):
 * - 已下载的图片进入可视区后自动显示;视频、语音、文件不预加载,点击才显示/播放/下载;
 * - 没下载的(手机上还没传上来)点击后请求手机上传并轮询进度,传好就显示;失败显示原因,点击重试。
 * 用事件委托处理,向上滚动动态加载进来的消息也有效。
 */
(function () {
  "use strict";

  var body = document.getElementById("wechat-thread-body");
  if (!body) return;

  var POLL_INTERVAL = 2000;
  var POLL_LIMIT = 80; // 约 160 秒,服务端 150 秒超时后会返回 failed

  function toast(message, tone) {
    if (window.appToast) window.appToast(message, tone);
  }

  function hintOf(card) {
    return card.querySelector("[data-media-hint]");
  }

  function setState(card, state, hint) {
    card.dataset.state = state;
    card.classList.toggle("is-loading", state === "loading");
    card.classList.toggle("is-failed", state === "failed");
    var element = hintOf(card);
    if (element && hint) element.textContent = hint;
  }

  // 放大查看:图片取当前会话里已显示的所有图片(可左右切换);视频单独在查看器里播放。
  function openViewer(type, url) {
    if (!window.appLightbox) return;
    if (type === "video") {
      window.appLightbox.open([{ type: "video", url: url }], 0);
      return;
    }
    var images = Array.prototype.slice.call(body.querySelectorAll("img.msg-media-image"));
    var urls = images.map(function (image) { return image.getAttribute("src"); });
    window.appLightbox.open(urls.map(function (item) { return { type: "image", url: item }; }), Math.max(urls.indexOf(url), 0));
  }

  // 把占位卡片换成真正的媒体元素。userInitiated:用户点的才自动播放/放大(滚动自动显示的图片不涉及)。
  // 视频保持占位卡片(提示"点击播放"),在放大查看器里播放。
  function reveal(card, userInitiated) {
    var url = card.dataset.url;
    var type = card.dataset.mediaType;
    if (!url) return;
    if (type === "file") {
      var link = document.createElement("a");
      link.href = url;
      link.download = "";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setState(card, "ready", "已开始下载");
      return;
    }
    if (type === "video") {
      setState(card, "ready", "点击播放");
      if (userInitiated) openViewer("video", url);
      return;
    }
    var element;
    if (type === "voice") {
      element = document.createElement("audio");
      element.controls = true;
      element.preload = "metadata";
      element.autoplay = !!userInitiated;
      element.className = "msg-media-player";
    } else {
      element = document.createElement("img");
      element.alt = type === "emoji" ? "表情" : "图片";
      element.className = type === "emoji" ? "msg-media-emoji" : "msg-media-image";
      element.loading = "lazy";
    }
    // 文件损坏、被删除等导致加载失败:回到占位卡片并允许重试
    element.addEventListener("error", function () {
      element.replaceWith(card);
      setState(card, "failed", "加载失败，点击重试");
    }, { once: true });
    element.src = url;
    card.replaceWith(element);
    if (type === "image" && userInitiated) openViewer("image", url);
  }

  function poll(card, attempt) {
    var messageId = card.dataset.messageId;
    return fetch("/messages/media/status?message_id=" + encodeURIComponent(messageId), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.state === "ready") {
          card.dataset.url = data.url;
          reveal(card, true);
        } else if (data.state === "failed") {
          setState(card, "failed", (data.error || "获取失败") + "，点击重试");
        } else if (attempt < POLL_LIMIT) {
          return new Promise(function (resolve) { setTimeout(resolve, POLL_INTERVAL); }).then(function () { return poll(card, attempt + 1); });
        } else {
          setState(card, "failed", "获取超时，点击重试");
        }
      })
      .catch(function () { setState(card, "failed", "网络异常，点击重试"); });
  }

  function load(card) {
    setState(card, "loading", "正在从手机获取…");
    window.appPost("/messages/media/load", { message_id: card.dataset.messageId }, false)
      .then(function (result) {
        var data = result.payload || {};
        if (!result.ok || data.state === "failed") {
          setState(card, "failed", (data.notice || data.error || "获取失败") + "，点击重试");
          if (result.status === 403) toast("没有权限从手机获取媒体", "danger");
          return null;
        }
        if (data.state === "ready") {
          card.dataset.url = data.url;
          reveal(card, true);
          return null;
        }
        return poll(card, 0);
      })
      .catch(function () { setState(card, "failed", "网络异常，点击重试"); });
  }

  function activate(card) {
    var state = card.dataset.state;
    if (state === "loading") return;
    if (state === "ready") reveal(card, true);
    else load(card);
  }

  body.addEventListener("click", function (event) {
    var card = event.target.closest("[data-media-card]");
    if (card) {
      activate(card);
      return;
    }
    // 已显示的图片:点击放大
    var image = event.target.closest("img.msg-media-image");
    if (image) openViewer("image", image.getAttribute("src"));
  });
  body.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var card = event.target.closest && event.target.closest("[data-media-card]");
    if (card) {
      event.preventDefault();
      activate(card);
    }
  });

  // 已下载的图片/表情:进入可视区(含预留的 300px)再自动显示,不一次性加载整个会话的图片。
  var observer = "IntersectionObserver" in window
    ? new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          observer.unobserve(entry.target);
          var card = entry.target;
          if (card.isConnected && card.dataset.state === "ready") reveal(card, false);
        });
      }, { root: body, rootMargin: "300px 0px" })
    : null;

  function watch(card) {
    var type = card.dataset.mediaType;
    if (observer && card.dataset.state === "ready" && (type === "image" || type === "emoji") && !card.dataset.watched) {
      card.dataset.watched = "1";
      observer.observe(card);
    }
  }

  body.querySelectorAll("[data-media-card]").forEach(watch);
  // 向上滚动加载更早的消息、发送后追加的消息:新出现的卡片同样处理
  new MutationObserver(function (records) {
    records.forEach(function (record) {
      record.addedNodes.forEach(function (node) {
        if (node.nodeType !== 1) return;
        if (node.matches && node.matches("[data-media-card]")) watch(node);
        if (node.querySelectorAll) node.querySelectorAll("[data-media-card]").forEach(watch);
      });
    });
  }).observe(body, { childList: true, subtree: true });
})();
