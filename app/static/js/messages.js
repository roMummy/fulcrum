/* 消息中心:整页撑满窗口高度、向上滚动加载更早的消息、不刷新页面的发送框。
 * 增量数据来自 /messages/history(返回渲染好的消息行 HTML),发送走 /messages/send(返回 JSON)。
 */
(function () {
  "use strict";

  var center = document.getElementById("msg-center");
  if (!center) return;

  var body = document.getElementById("wechat-thread-body");
  var hint = document.getElementById("wechat-load-hint");
  var form = document.getElementById("wechat-composer");
  var conversationId = center.dataset.conversationId || "";
  var hasMore = center.dataset.hasMore === "1";
  var loading = false;
  // 用户停在底部时,图片等内容后加载撑高了页面,要继续贴住底部。
  var stickToBottom = true;

  // ---------- 高度:从当前位置一直延伸到窗口底部,页面本身不再出现滚动条 ----------
  function fit() {
    var workspace = center.closest(".workspace");
    var bottomPadding = workspace ? parseFloat(getComputedStyle(workspace).paddingBottom) || 0 : 24;
    var top = center.getBoundingClientRect().top + window.scrollY;
    center.style.height = Math.max(420, window.innerHeight - top - bottomPadding) + "px";
  }
  window.addEventListener("resize", fit);
  fit();

  if (!body || !conversationId) return;

  function rows() {
    return body.querySelectorAll(".wechat-message-row");
  }

  function toBottom() {
    body.scrollTop = body.scrollHeight;
  }

  body.addEventListener("scroll", function () {
    stickToBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    if (body.scrollTop < 160) loadOlder();
  }, { passive: true });
  // 图片/视频加载完成会改变高度(load 不冒泡,所以用捕获)。
  body.addEventListener("load", function () {
    if (stickToBottom) toBottom();
  }, true);

  function fetchRows(params) {
    var query = new URLSearchParams(Object.assign({ conversation_id: conversationId }, params));
    return fetch("/messages/history?" + query.toString(), { credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  // ---------- 向上滚动加载更早的消息 ----------
  function loadOlder() {
    var first = rows()[0];
    if (loading || !hasMore || !first) return;
    loading = true;
    hint.hidden = false;
    hint.textContent = "正在加载更早的消息…";
    fetchRows({ before_id: first.dataset.id }).then(function (data) {
      var previousHeight = body.scrollHeight;
      var previousTop = body.scrollTop;
      hint.insertAdjacentHTML("afterend", data.html);
      // 在顶部插入内容后保持原来看的那条不动,而不是跳到最上面。
      body.scrollTop = previousTop + (body.scrollHeight - previousHeight);
      hasMore = data.has_more;
      hint.hidden = hasMore;
      if (!hasMore) {
        hint.hidden = false;
        hint.textContent = "没有更早的消息了";
      }
      loading = false;
      fillIfShort();
    }).catch(function () {
      hint.textContent = "加载失败，再向上滚动一下重试";
      loading = false;
    });
  }

  // 消息太少、撑不满一屏时没有滚动条,也就触发不了滚动加载,这里主动补到有滚动条为止。
  function fillIfShort() {
    if (hasMore && !loading && body.scrollHeight <= body.clientHeight + 40) loadOlder();
  }

  toBottom();
  fillIfShort();

  // ---------- 发送框 ----------
  if (!form) return;
  var textarea = form.querySelector("textarea");
  var button = form.querySelector(".wechat-send");
  var tip = document.getElementById("wechat-composer-hint");
  var defaultTip = tip.textContent;
  var sending = false;
  var tipTimer = null;

  function setTip(text, isError) {
    clearTimeout(tipTimer);
    tip.textContent = text;
    tip.classList.toggle("is-error", !!isError);
    if (text !== defaultTip) {
      tipTimer = setTimeout(function () {
        tip.textContent = defaultTip;
        tip.classList.remove("is-error");
      }, isError ? 6000 : 3500);
    }
  }

  function refreshControls() {
    button.disabled = sending || !textarea.value.trim();
  }

  // 文字区随内容长高,最高 140px(样式里的 max-height),再多就内部滚动。
  function autosize() {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
  }

  // 发送后把新记录(包括失败的那条)拉进对话里。
  function loadNewer() {
    var all = rows();
    var last = all[all.length - 1];
    return fetchRows({ after_id: last ? last.dataset.id : 0 }).then(function (data) {
      if (!data.html) return;
      var placeholder = body.querySelector(".wechat-empty");
      if (placeholder) placeholder.remove();
      body.insertAdjacentHTML("beforeend", data.html);
      toBottom();
    });
  }

  function send() {
    var text = textarea.value.trim();
    if (!text || sending) return;
    sending = true;
    refreshControls();
    setTip("发送中…", false);
    fetch("/messages/send", { method: "POST", body: new FormData(form), credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) { return { ok: response.ok && data.ok, data: data }; });
      })
      .then(function (result) {
        if (result.ok) {
          textarea.value = "";
          autosize();
          setTip(result.data.notice || "已提交", false);
        } else {
          setTip(result.data.notice || "发送失败，请稍后再试", true);
        }
        return loadNewer().catch(function () {});
      })
      .catch(function () {
        setTip("网络异常，发送失败", true);
      })
      .then(function () {
        sending = false;
        refreshControls();
        textarea.focus();
      });
  }

  textarea.addEventListener("input", function () {
    autosize();
    refreshControls();
  });
  // Enter 发送、Shift+Enter 换行;输入法正在选词时的回车不算发送。
  textarea.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      send();
    }
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    send();
  });
  refreshControls();
})();
