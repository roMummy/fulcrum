/* 全站共用的图片/视频放大查看器:window.appLightbox.open(items, index)。
 * items: [{type: "image"|"video", url: "..."}];图片点击背景/按钮/Esc 关闭,多张时可用左右箭头或键盘 ←/→ 切换;
 * 视频在查看器里带控制条播放,关闭或切换时会停止播放。消息中心与朋友圈共用。 */
(function () {
  "use strict";

  var root = null;
  var stage = null;
  var counter = null;
  var prevButton = null;
  var nextButton = null;
  var state = { items: [], index: 0 };

  function build() {
    root = document.createElement("div");
    root.className = "app-lightbox";
    root.hidden = true;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "媒体查看");
    root.innerHTML =
      '<button type="button" class="app-lightbox-close" aria-label="关闭">×</button>' +
      '<button type="button" class="app-lightbox-nav is-prev" aria-label="上一张">‹</button>' +
      '<div class="app-lightbox-stage"></div>' +
      '<button type="button" class="app-lightbox-nav is-next" aria-label="下一张">›</button>' +
      '<span class="app-lightbox-counter"></span>';
    document.body.appendChild(root);
    stage = root.querySelector(".app-lightbox-stage");
    counter = root.querySelector(".app-lightbox-counter");
    prevButton = root.querySelector(".is-prev");
    nextButton = root.querySelector(".is-next");
    root.querySelector(".app-lightbox-close").addEventListener("click", close);
    prevButton.addEventListener("click", function () { go(-1); });
    nextButton.addEventListener("click", function () { go(1); });
    // 点击背景(不是图片/视频/按钮)关闭
    root.addEventListener("click", function (event) {
      if (event.target === root || event.target === stage) close();
    });
  }

  function render() {
    var item = state.items[state.index];
    stage.innerHTML = "";
    var element;
    if (item.type === "video") {
      element = document.createElement("video");
      element.controls = true;
      element.autoplay = true;
      element.playsInline = true;
    } else {
      element = document.createElement("img");
      element.alt = "图片";
    }
    element.addEventListener("error", function () {
      stage.innerHTML = '<p class="app-lightbox-error">加载失败，请关闭后重试</p>';
    }, { once: true });
    element.src = item.url;
    stage.appendChild(element);
    var many = state.items.length > 1;
    prevButton.hidden = nextButton.hidden = counter.hidden = !many;
    counter.textContent = (state.index + 1) + " / " + state.items.length;
  }

  function go(step) {
    var next = state.index + step;
    if (next < 0 || next >= state.items.length) return;
    state.index = next;
    render();
  }

  function close() {
    if (!root || root.hidden) return;
    stage.innerHTML = ""; // 移除元素即停止视频播放与下载
    root.hidden = true;
    document.documentElement.classList.remove("has-lightbox");
  }

  function open(items, index) {
    items = (items || []).filter(function (item) { return item && item.url; });
    if (!items.length) return;
    if (!root) build();
    state.items = items;
    state.index = Math.min(Math.max(index || 0, 0), items.length - 1);
    root.hidden = false;
    document.documentElement.classList.add("has-lightbox");
    render();
    root.querySelector(".app-lightbox-close").focus();
  }

  document.addEventListener("keydown", function (event) {
    if (!root || root.hidden) return;
    if (event.key === "Escape") close();
    else if (event.key === "ArrowLeft") go(-1);
    else if (event.key === "ArrowRight") go(1);
  });

  window.appLightbox = { open: open, close: close };
})();
