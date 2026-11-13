/* 消息中心：增量轮询、媒体发送、引用回复、群成员提及和限时撤回。 */
(function () {
  "use strict";
  var center = document.getElementById("msg-center");
  if (!center) return;
  var body = document.getElementById("wechat-thread-body");
  var hint = document.getElementById("wechat-load-hint");
  var form = document.getElementById("wechat-composer");
  var conversationId = center.dataset.conversationId || "";
  var hasMore = center.dataset.hasMore === "1";
  var loading = false, stickToBottom = true, refreshPromise = null, pollTimer = null, stopped = false;

  function fit() {
    var workspace = center.closest(".workspace");
    var padding = workspace ? parseFloat(getComputedStyle(workspace).paddingBottom) || 0 : 24;
    var top = center.getBoundingClientRect().top + window.scrollY;
    center.style.height = Math.max(460, window.innerHeight - top - padding) + "px";
  }
  window.addEventListener("resize", fit);
  fit();
  // 会话列表独立刷新；尚未选择会话或暂无会话时，也能带出新收到的消息。
  var conversations = center.querySelector(".wechat-conversation-list");
  var listTimer = null, listRequest = null;
  function conversationFilters() {
    var query = new URLSearchParams();
    if (center.dataset.botId) query.set("bot_id", center.dataset.botId);
    if (center.dataset.channelId) query.set("channel_id", center.dataset.channelId);
    if (center.dataset.filterChatType) query.set("chat_type", center.dataset.filterChatType);
    return query;
  }
  function refreshConversations() {
    if (listRequest || !conversations) return listRequest || Promise.resolve();
    listRequest = request("/messages/conversations?" + conversationFilters().toString()).then(function (data) {
      if (!Array.isArray(data.conversations)) return;
      var top = conversations.scrollTop;
      var seen = new Set();
      var position = 0;
      data.conversations.forEach(function (item) {
        var id = String(item.id);
        seen.add(id);
        var link = Array.from(conversations.querySelectorAll(".wechat-conversation-item")).find(function (node) { return node.dataset.conversationId === id; });
        if (!link) {
          link = document.createElement("a");
          link.className = "wechat-conversation-item";
          link.dataset.conversationId = id;
          link.innerHTML = '<span class="wechat-avatar"></span><span class="wechat-conversation-copy"><span class="wechat-conversation-head"><strong class="wechat-conversation-name"></strong><time class="wechat-conversation-time"></time></span><span class="wechat-conversation-preview"></span></span>';
        }
        var query = conversationFilters();
        query.set("conversation_id", id);
        link.href = "/messages?" + query.toString();
        link.classList.toggle("active", id === conversationId);
        var nameEl = link.querySelector(".wechat-conversation-name");
        nameEl.textContent = item.display_name;
        if (item.kind === "official") {
          // 公众号在名称前带一个小图标,和服务端渲染的列表一致
          var badge = document.createElement("span");
          badge.className = "i i-newspaper msg-kind-badge";
          badge.title = "公众号";
          badge.setAttribute("aria-label", "公众号");
          nameEl.prepend(badge);
        }
        link.querySelector(".wechat-conversation-time").textContent = item.time || "-";
        link.querySelector(".wechat-conversation-preview").textContent = item.preview;
        var avatar = link.querySelector(".wechat-avatar");
        if (item.avatar_url) {
          var image = avatar.querySelector("img");
          if (!image) { image = document.createElement("img"); image.alt = ""; image.referrerPolicy = "no-referrer"; avatar.replaceChildren(image); }
          if (image.getAttribute("src") !== item.avatar_url) image.src = item.avatar_url;
        } else avatar.textContent = item.initial || "?";
        if (conversations.children[position] !== link) conversations.insertBefore(link, conversations.children[position] || null);
        position += 1;
      });
      conversations.querySelectorAll(".wechat-conversation-item").forEach(function (link) { if (!seen.has(link.dataset.conversationId)) link.remove(); });
      var empty = conversations.querySelector(".wechat-empty");
      if (empty && seen.size) empty.remove();
      if (!empty && !seen.size) { empty = document.createElement("div"); empty.className = "wechat-empty"; empty.textContent = "暂无会话消息"; conversations.appendChild(empty); }
      conversations.scrollTop = top;
    }).finally(function () { listRequest = null; });
    return listRequest;
  }
  function scheduleList() {
    clearTimeout(listTimer);
    if (stopped) return;
    listTimer = setTimeout(async function () {
      if (!document.hidden) { try { await refreshConversations(); } catch (_error) {} }
      scheduleList();
    }, 10000);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refreshConversations().catch(function () {}); });
  window.addEventListener("pagehide", function () { stopped = true; clearTimeout(listTimer); });
  window.addEventListener("pageshow", function () { stopped = false; scheduleList(); });
  scheduleList();
  if (!body || !conversationId) return;
  function rows() { return Array.from(body.querySelectorAll(".wechat-message-row")); }
  function toBottom() { body.scrollTop = body.scrollHeight; }

  // 断网时结束请求，防止一个挂起请求阻塞后续轮询。
  async function request(url, options) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 20000);
    try {
      var response = await fetch(url, Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal }, options));
      var data = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(data.notice || (typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试"));
      return data;
    } finally { clearTimeout(timeout); }
  }
  function fetchRows(params) {
    return request("/messages/history?" + new URLSearchParams(Object.assign({ conversation_id: conversationId }, params)).toString());
  }
  function parseRows(html) {
    var template = document.createElement("template");
    template.innerHTML = html || "";
    return Array.from(template.content.querySelectorAll(".wechat-message-row"));
  }
  function insertRows(html, older) {
    var known = new Set(rows().map(function (row) { return row.dataset.id; }));
    var additions = parseRows(html).filter(function (row) { return !known.has(row.dataset.id); });
    if (additions.length) {
      var placeholder = body.querySelector(".wechat-empty");
      if (placeholder) placeholder.remove();
      if (older) hint.after.apply(hint, additions);
      else body.append.apply(body, additions);
    }
    return additions;
  }
  function fillIfShort() {
    if (hasMore && !loading && body.scrollHeight <= body.clientHeight + 40) loadOlder();
  }
  async function loadOlder() {
    var first = rows()[0];
    if (loading || !hasMore || !first) return;
    loading = true;
    hint.hidden = false;
    hint.textContent = "正在加载更多消息…";
    try {
      var data = await fetchRows({ before_id: first.dataset.id });
      var previousHeight = body.scrollHeight, previousTop = body.scrollTop;
      insertRows(data.html, true);
      body.scrollTop = previousTop + body.scrollHeight - previousHeight;
      hasMore = data.has_more;
      hint.hidden = hasMore;
      if (!hasMore) hint.textContent = "没有更多消息了";
    } catch (_error) { hint.textContent = "加载失败，再向上滚动一下重试"; }
    finally { loading = false; }
    fillIfShort();
  }

  // 发送和轮询共用增量请求，按 ID 去重，补齐超过一页的新消息。
  function loadNewer() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async function () {
      var more = true;
      while (more && !stopped) {
        var all = rows(), last = all[all.length - 1];
        var data = await fetchRows({ after_id: last ? last.dataset.id : 0, sync_ids: all.slice(-100).map(function (row) { return row.dataset.id; }).join(",") });
        var follow = stickToBottom, oldTop = body.scrollTop;
        // 历史回显关联后移除多余卡片，原记录仍保留在服务端。
        (data.removed_ids || []).forEach(function (id) {
          var duplicate = body.querySelector('.wechat-message-row[data-id="' + Number(id) + '"]');
          if (duplicate) duplicate.remove();
        });
        parseRows(data.updates_html).forEach(function (row) {
          var existing = body.querySelector('.wechat-message-row[data-id="' + row.dataset.id + '"]');
          if (!existing) return;
          // 只更新发生变化的行，保留正在播放的语音和已展开的图片。
          if (["status", "revokeState", "revokeUntil", "version", "own"].some(function (key) { return existing.dataset[key] !== row.dataset[key]; })) existing.replaceWith(row);
        });
        var added = insertRows(data.html, false);
        if (follow) toBottom();
        else body.scrollTop = oldTop;
        more = data.has_more && added.length > 0;
      }
    })().finally(function () { refreshPromise = null; });
    return refreshPromise;
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (stopped) return;
    pollTimer = setTimeout(async function () {
      if (!document.hidden) {
        try { await loadNewer(); } catch (_error) { /* 下轮自动重试，保留输入中的内容。 */ }
      }
      schedulePoll();
    }, 2500);
  }
  body.addEventListener("scroll", function () {
    stickToBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    if (body.scrollTop < 160) loadOlder();
  }, { passive: true });
  body.addEventListener("load", function () { if (stickToBottom) toBottom(); }, true);
  // 引用条、附件和输入框变高会缩小消息区，原先在底部时继续露出最新消息。
  if ("ResizeObserver" in window) new ResizeObserver(function () { if (stickToBottom) toBottom(); }).observe(body);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) loadNewer().catch(function () {});
  });
  window.addEventListener("pagehide", function () { stopped = true; clearTimeout(pollTimer); });
  window.addEventListener("pageshow", function () { stopped = false; schedulePoll(); });
  toBottom();
  fillIfShort();
  schedulePoll();
  if (!form) return;

  var textarea = form.querySelector("textarea"), button = form.querySelector(".wechat-send");
  var tip = document.getElementById("wechat-composer-hint"), defaultTip = tip.textContent;
  var fileInput = document.getElementById("wechat-media-input");
  var attachment = document.getElementById("wechat-attachment"), reply = document.getElementById("wechat-reply");
  var attachmentType = document.getElementById("wechat-attachment-type");
  var attachmentImage = document.getElementById("wechat-attachment-image");
  var mentionList = document.getElementById("wechat-mentions"), menu = document.getElementById("wechat-context-menu");
  var mentionPicker = document.getElementById("wechat-mention-picker");
  var sending = false, tipTimer = null, file = null, mediaType = "file", pickedType = "file";
  var attachmentUrl = "";
  var replyTo = "", mentions = [], menuRow = null, capabilitiesPromise = null;
  var previousText = textarea.value, mentionQuery = null, mentionTimer = null, mentionSequence = 0;
  var mentionMembers = [], mentionActive = -1;
  var toolButtons = Array.from(form.querySelectorAll("[data-send-media]"));
  function setTip(text, isError) {
    clearTimeout(tipTimer);
    tip.textContent = text;
    tip.classList.toggle("is-error", !!isError);
    if (text !== defaultTip) tipTimer = setTimeout(function () { tip.textContent = defaultTip; tip.classList.remove("is-error"); }, isError ? 6000 : 3500);
  }
  function capabilities() {
    if (!capabilitiesPromise) capabilitiesPromise = request("/messages/capabilities?conversation_id=" + encodeURIComponent(conversationId))
      .then(function (data) { return data.capabilities; })
      .catch(function (error) { capabilitiesPromise = null; throw error; });
    return capabilitiesPromise;
  }
  async function supports(kind) {
    try {
      var supported = await capabilities();
      if (supported && supported[kind]) return true;
      var names = { image: "发送图片", voice: "发送语音", video: "发送视频", emoji: "发送表情", file: "发送文件", quote: "引用回复", mention: "@ 群成员", revoke: "撤回消息" };
      setTip("当前渠道不支持" + names[kind], true);
    } catch (error) { setTip(error.message, true); }
    return false;
  }
  function refreshControls() {
    button.disabled = sending || (!file && !textarea.value.trim() && !mentions.length);
    textarea.disabled = sending;
    attachmentType.disabled = sending;
    toolButtons.forEach(function (tool) { tool.disabled = sending; });
  }
  function autosize() {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
  }
  function clearReply() { replyTo = ""; reply.hidden = true; }
  function releaseAttachmentImage() {
    if (attachmentUrl) URL.revokeObjectURL(attachmentUrl);
    attachmentUrl = "";
    attachmentImage.hidden = true;
    attachmentImage.removeAttribute("src");
  }
  function clearFile() {
    file = null;
    fileInput.value = "";
    attachment.hidden = true;
    releaseAttachmentImage();
    refreshControls();
  }
  function fileKinds(candidate) {
    var mime = candidate.type.toLowerCase(), name = candidate.name.toLowerCase();
    if (mime.indexOf("image/") === 0 || /\.(png|jpe?g|gif|webp|bmp|avif|heic)$/.test(name)) return ["file", "image", "emoji"];
    if (mime.indexOf("video/") === 0 || /\.(mp4|mov|m4v|webm|avi|mkv)$/.test(name)) return ["file", "video"];
    if (mime.indexOf("audio/") === 0 || /\.(mp3|wav|m4a|aac|ogg|opus|amr|silk|slik|flac)$/.test(name)) return ["file", "voice"];
    return ["file"];
  }
  function attachmentFormatSupported() {
    if (fileKinds(file).indexOf(mediaType) === -1) {
      setTip("该附件格式不支持当前发送类型，请切换为文件发送", true);
      return false;
    }
    return true;
  }
  async function setFile(candidate, kind) {
    if (!candidate.size || candidate.size > 25 * 1024 * 1024) {
      setTip("文件不能为空，且不能超过 25 MB", true);
      fileInput.value = "";
      return;
    }
    releaseAttachmentImage();
    file = candidate;
    var kinds = fileKinds(file);
    var requestedType = kind || (kinds.indexOf("image") !== -1 ? "image" : (kinds.indexOf("video") !== -1 ? "video" : "file"));
    mediaType = kinds.indexOf(requestedType) !== -1 ? requestedType : "file";
    var labels = { file: "文件", image: "图片", emoji: "表情", video: "视频", voice: "语音" };
    attachmentType.replaceChildren();
    kinds.forEach(function (value) {
      var option = document.createElement("option");
      option.value = value;
      option.textContent = labels[value];
      attachmentType.appendChild(option);
    });
    attachmentType.value = mediaType;
    attachment.querySelector("[data-file-preview]").textContent = file.name;
    attachment.hidden = false;
    if (kinds.indexOf("image") !== -1) {
      attachmentUrl = URL.createObjectURL(file);
      attachmentImage.src = attachmentUrl;
      attachmentImage.hidden = false;
    }
    clearReply();
    closeMentionPicker();
    renderMentions();
    refreshControls();
    if (requestedType !== mediaType) {
      setTip("该附件格式不支持所选类型，已保留附件，请按文件发送", true);
    } else if (await supports(mediaType)) {
      setTip("附件已就绪，确认类型后点击发送", false);
    }
  }
  attachmentImage.addEventListener("error", function () { attachmentImage.hidden = true; });
  attachmentType.addEventListener("change", async function () {
    if (!file || sending) return;
    mediaType = attachmentType.value;
    if (attachmentFormatSupported() && await supports(mediaType)) setTip("已切换发送类型，点击发送", false);
  });
  // 只追踪已经选择的标签区间，避免把手写的 @ 或相同昵称正文当作提及删除。
  function syncMentionTokens() {
    var value = textarea.value;
    if (value === previousText) return;
    var prefix = 0, oldEnd = previousText.length, newEnd = value.length;
    while (prefix < oldEnd && prefix < newEnd && previousText[prefix] === value[prefix]) prefix += 1;
    while (oldEnd > prefix && newEnd > prefix && previousText[oldEnd - 1] === value[newEnd - 1]) { oldEnd -= 1; newEnd -= 1; }
    var shift = newEnd - oldEnd;
    mentions = mentions.filter(function (person) {
      if (person.end <= prefix) return true;
      if (person.start >= oldEnd) { person.start += shift; person.end += shift; return true; }
      return false;
    });
    previousText = value;
  }
  function mentionContent() {
    var content = textarea.value;
    mentions.slice().sort(function (a, b) { return b.start - a.start; }).forEach(function (person) {
      content = content.slice(0, person.start) + "{{mention:" + person.id + "}}" + content.slice(person.end);
    });
    return content.trim();
  }
  function renderMentions() {
    mentionList.replaceChildren();
    mentionList.hidden = !mentions.length;
    var shown = new Set();
    mentions.forEach(function (person) {
      if (shown.has(person.id)) return;
      shown.add(person.id);
      var chip = document.createElement("button");
      chip.type = "button";
      chip.textContent = "@" + person.name + " ×";
      chip.setAttribute("aria-label", "取消提及" + person.name);
      chip.addEventListener("click", function () {
        if (sending) return;
        mentions.filter(function (item) { return item.id === person.id; }).sort(function (a, b) { return b.start - a.start; }).forEach(function (item) {
          textarea.setRangeText("", item.start, item.end, "end");
          syncMentionTokens();
        });
        renderMentions();
        autosize();
        refreshControls();
      });
      mentionList.appendChild(chip);
    });
  }
  function closeMentionPicker() {
    clearTimeout(mentionTimer);
    mentionSequence += 1;
    mentionQuery = null;
    mentionMembers = [];
    mentionActive = -1;
    mentionPicker.hidden = true;
    textarea.setAttribute("aria-expanded", "false");
    textarea.removeAttribute("aria-activedescendant");
  }
  function mentionStatus(message) {
    mentionMembers = [];
    mentionActive = -1;
    mentionPicker.replaceChildren();
    var status = document.createElement("div");
    status.className = "wechat-mention-picker-status";
    status.setAttribute("role", "status");
    status.textContent = message;
    mentionPicker.appendChild(status);
    textarea.removeAttribute("aria-activedescendant");
  }
  function highlightMention() {
    mentionPicker.querySelectorAll("[data-member-index]").forEach(function (option, index) { option.setAttribute("aria-selected", String(index === mentionActive)); });
    var option = mentionPicker.querySelector('[data-member-index="' + mentionActive + '"]');
    if (option) {
      textarea.setAttribute("aria-activedescendant", option.id);
      option.scrollIntoView({ block: "nearest" });
    }
  }
  function renderMentionMembers(members, hasMore) {
    mentionMembers = members;
    if (!members.length) { mentionStatus("没有找到群成员，请换个名字搜索"); return; }
    mentionActive = 0;
    mentionPicker.replaceChildren();
    members.forEach(function (member, index) {
      var option = document.createElement("button");
      option.type = "button";
      option.tabIndex = -1;
      option.id = "wechat-mention-option-" + index;
      option.dataset.memberIndex = String(index);
      option.setAttribute("role", "option");
      option.setAttribute("aria-label", member.id === "notify@all" ? "@所有人" : member.name);
      var avatar = document.createElement("span");
      avatar.className = "wechat-avatar";
      if (member.avatar_url) {
        var image = document.createElement("img");
        image.src = member.avatar_url;
        image.alt = "";
        image.referrerPolicy = "no-referrer";
        avatar.appendChild(image);
      } else avatar.textContent = member.initial || member.name.slice(0, 1);
      var name = document.createElement("span");
      name.textContent = member.id === "notify@all" ? "@所有人" : member.name;
      option.append(avatar, name);
      mentionPicker.appendChild(option);
    });
    if (hasMore) {
      var hint = document.createElement("div");
      hint.className = "wechat-mention-picker-status";
      hint.textContent = "继续输入名字，查找更多群成员";
      mentionPicker.appendChild(hint);
    }
    highlightMention();
  }
  function scheduleMentionSearch() {
    if (center.dataset.chatType !== "group" || sending) { closeMentionPicker(); return; }
    var caret = textarea.selectionStart;
    var match = textarea.selectionStart === textarea.selectionEnd && textarea.value.slice(0, caret).match(/@([^\s@]{0,50})$/u);
    if (!match || mentions.some(function (person) { return person.start < caret && caret <= person.end; })) { closeMentionPicker(); return; }
    clearTimeout(mentionTimer);
    var sequence = ++mentionSequence;
    mentionQuery = { start: caret - match[0].length, end: caret, text: match[1] };
    mentionPicker.hidden = false;
    textarea.setAttribute("aria-expanded", "true");
    var thread = center.querySelector(".wechat-thread");
    mentionPicker.style.maxHeight = Math.max(80, Math.min(240, form.getBoundingClientRect().top - thread.getBoundingClientRect().top - 12)) + "px";
    mentionStatus("正在加载群成员…");
    mentionTimer = setTimeout(async function () {
      try {
        var supported = await capabilities();
        if (sequence !== mentionSequence) return;
        if (!supported.mention) { closeMentionPicker(); setTip("当前渠道不支持 @ 群成员", true); return; }
        var query = new URLSearchParams({ conversation_id: conversationId, q: mentionQuery.text });
        var data = await request("/messages/mention-members?" + query.toString());
        if (sequence !== mentionSequence) return;
        renderMentionMembers(Array.isArray(data.members) ? data.members : [], data.has_more);
      } catch (_error) {
        if (sequence === mentionSequence) mentionStatus("群成员加载失败，请继续输入重试");
      }
    }, 150);
  }
  function insertMention(member) {
    if (sending) return;
    syncMentionTokens();
    var ids = new Set(mentions.map(function (person) { return person.id; }));
    if (ids.size >= 20 && !ids.has(member.id)) { setTip("一次最多 @ 20 位群成员", true); return; }
    var start = mentionQuery ? mentionQuery.start : textarea.selectionStart;
    var end = mentionQuery ? mentionQuery.end : textarea.selectionEnd;
    var token = "@" + member.name + "\u2005";
    if (textarea.value.length - (end - start) + token.length > textarea.maxLength) { setTip("消息太长，无法插入提及", true); return; }
    textarea.setRangeText(token, start, end, "end");
    syncMentionTokens();
    mentions.push({ id: member.id, name: member.name, start: start, end: start + token.length });
    closeMentionPicker();
    renderMentions();
    autosize();
    refreshControls();
    textarea.focus();
  }
  mentionPicker.addEventListener("mousedown", function (event) { event.preventDefault(); });
  mentionPicker.addEventListener("click", function (event) {
    var option = event.target.closest("[data-member-index]");
    if (option && mentionMembers[Number(option.dataset.memberIndex)]) insertMention(mentionMembers[Number(option.dataset.memberIndex)]);
  });
  document.addEventListener("click", function (event) { if (event.target !== textarea && !mentionPicker.contains(event.target)) closeMentionPicker(); });
  form.querySelector("[data-clear-reply]").addEventListener("click", function () { if (!sending) clearReply(); });
  form.querySelector("[data-clear-file]").addEventListener("click", function () { if (!sending) clearFile(); });
  toolButtons.forEach(function (tool) {
    tool.addEventListener("click", async function () {
      var kind = tool.dataset.sendMedia;
      if (sending || !await supports(kind)) return;
      pickedType = kind;
      fileInput.accept = { image: "image/*", voice: "audio/*,.silk,.slik,.amr", video: "video/*", emoji: "image/gif,image/png,image/webp", file: "" }[kind];
      fileInput.value = "";
      fileInput.click();
    });
  });
  fileInput.addEventListener("change", function () {
    if (fileInput.files.length) setFile(fileInput.files[0], pickedType);
  });
  textarea.addEventListener("paste", function (event) {
    if (!event.clipboardData) return;
    var pasted = Array.from(event.clipboardData.files || []);
    if (!pasted.length) return; // 普通文字仍交给浏览器粘贴，不改变光标和选区语义。
    event.preventDefault();
    if (sending) { setTip("正在发送，请稍后再粘贴附件", true); return; }
    var text = event.clipboardData.getData("text/plain");
    if (text) {
      var available = textarea.maxLength - textarea.value.length + textarea.selectionEnd - textarea.selectionStart;
      text = text.slice(0, Math.max(0, available));
      textarea.setRangeText(text, textarea.selectionStart, textarea.selectionEnd, "end");
      syncMentionTokens();
      renderMentions();
      autosize();
      refreshControls();
    }
    if (pasted.length > 1) { setTip("一次只能添加 1 个附件，请分别粘贴", true); return; }
    if (file) { setTip("已有待发送附件，请先发送或取消后再粘贴", true); return; }
    setFile(pasted[0]);
  });
  function closeMenu(restoreFocus) {
    menu.hidden = true;
    if (restoreFocus && menuRow && menuRow.isConnected) menuRow.querySelector(".wechat-message-bubble").focus();
  }
  function showMenu(target, x, y) {
    var row = target.closest(".wechat-message-row");
    if (!row || sending || row.dataset.status === "revoked") return false;
    var avatar = !!target.closest(".wechat-avatar");
    var mentionButton = menu.querySelector('[data-message-action="mention"]');
    var quoteButton = menu.querySelector('[data-message-action="quote"]');
    var revokeButton = menu.querySelector('[data-message-action="revoke"]');
    mentionButton.hidden = !avatar || center.dataset.chatType !== "group" || row.dataset.own === "1" || !row.dataset.senderId;
    quoteButton.hidden = avatar;
    revokeButton.hidden = avatar || row.dataset.own !== "1";
    if (!mentionButton.hidden) mentionButton.textContent = "@ " + row.dataset.senderName;
    if (quoteButton.hidden && mentionButton.hidden && revokeButton.hidden) return false;
    menuRow = row;
    menu.hidden = false;
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + "px";
    menu.querySelector("button:not([hidden])").focus();
    return true;
  }
  body.addEventListener("contextmenu", function (event) {
    if (event.target.closest(".wechat-message-bubble, .wechat-avatar") && showMenu(event.target, event.clientX, event.clientY)) event.preventDefault();
  });
  body.addEventListener("keydown", function (event) {
    if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
    var rect = event.target.getBoundingClientRect();
    if (showMenu(event.target, rect.left, rect.bottom)) event.preventDefault();
  });
  document.addEventListener("click", function (event) { if (!menu.contains(event.target)) closeMenu(false); });
  body.addEventListener("scroll", function () { closeMenu(false); }, { passive: true });
  window.addEventListener("resize", function () { closeMenu(false); });
  menu.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { closeMenu(true); event.preventDefault(); return; }
    if (event.key === "Tab") { closeMenu(false); return; }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    var items = Array.from(menu.querySelectorAll("button:not([hidden]):not(:disabled)"));
    var next = items.indexOf(document.activeElement) + (event.key === "ArrowDown" ? 1 : -1);
    items[(next + items.length) % items.length].focus();
  });
  menu.addEventListener("click", async function (event) {
    var action = event.target.closest("[data-message-action]"), row = menuRow;
    if (!action || !row || sending) return;
    closeMenu(false);
    var kind = action.dataset.messageAction;
    if (!await supports(kind)) return;
    if (kind === "quote") {
      clearFile();
      replyTo = row.dataset.id;
      reply.querySelector("[data-reply-preview]").textContent = row.dataset.senderName + "：" + row.dataset.preview;
      reply.hidden = false;
      textarea.focus();
    } else if (kind === "mention") {
      insertMention({ id: row.dataset.senderId, name: row.dataset.senderName });
    } else if (kind === "revoke") {
      if (row.dataset.revokeState === "pending") { setTip("消息正在撤回，请稍候", false); return; }
      if (Number(row.dataset.revokeUntil) > 0 && Date.now() >= Number(row.dataset.revokeUntil)) { setTip("发送已超过 2 分钟，无法撤回", true); return; }
      var payload = new FormData();
      payload.set("conversation_id", conversationId);
      payload.set("message_id", row.dataset.id);
      payload.set("csrf_token", form.elements.csrf_token.value);
      action.disabled = true;
      try {
        var result = await request("/messages/revoke", { method: "POST", body: payload });
        setTip(result.notice || "撤回请求已提交", !result.ok);
        await loadNewer();
      } catch (error) { setTip(error.message, true); }
      finally { action.disabled = false; }
    }
  });
  async function send() {
    if (sending || (!file && !textarea.value.trim() && !mentions.length)) return;
    syncMentionTokens();
    closeMentionPicker();
    sending = true;
    refreshControls();
    if (file && (!attachmentFormatSupported() || !await supports(mediaType))) {
      sending = false;
      refreshControls();
      return;
    }
    var sendingFile = !!file;
    var payload = new FormData(form);
    // 渠道媒体接口单独发送附件，输入中的文字保留，供下一条发送。
    payload.set("content", sendingFile ? "" : mentionContent());
    payload.set("message_type", file ? mediaType : "text");
    if (file) payload.set("file", file, file.name);
    if (replyTo) payload.set("reply_to_id", replyTo);
    payload.set("mention_ids", JSON.stringify(sendingFile ? [] : Array.from(new Set(mentions.map(function (person) { return person.id; })))));
    setTip("发送中…", false);
    try {
      var result = await request("/messages/send", { method: "POST", body: payload });
      if (result.ok) {
        if (!sendingFile) textarea.value = "";
        clearFile();
        clearReply();
        if (!sendingFile) mentions = [];
        previousText = textarea.value;
        renderMentions();
        autosize();
        stickToBottom = true;
      }
      setTip(result.notice || (result.ok ? "消息已提交" : "发送失败，请稍后再试"), !result.ok);
      await loadNewer().catch(function () {});
    } catch (error) { setTip(error.message || "网络异常，发送失败", true); }
    finally { sending = false; refreshControls(); textarea.focus(); }
  }
  textarea.addEventListener("input", function (event) {
    syncMentionTokens();
    renderMentions();
    autosize();
    refreshControls();
    if (event.isComposing) closeMentionPicker();
    else scheduleMentionSearch();
  });
  textarea.addEventListener("compositionend", scheduleMentionSearch);
  textarea.addEventListener("click", scheduleMentionSearch);
  textarea.addEventListener("keyup", function (event) { if (["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(event.key) !== -1) scheduleMentionSearch(); });
  textarea.addEventListener("keydown", function (event) {
    if (event.isComposing || event.keyCode === 229) return;
    if (!mentionPicker.hidden) {
      if (event.key === "Escape") { event.preventDefault(); closeMentionPicker(); return; }
      if (event.key === "Tab") { closeMentionPicker(); return; }
      if ((event.key === "ArrowDown" || event.key === "ArrowUp") && mentionMembers.length) {
        event.preventDefault();
        mentionActive = (mentionActive + (event.key === "ArrowDown" ? 1 : -1) + mentionMembers.length) % mentionMembers.length;
        highlightMention();
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (mentionMembers[mentionActive]) insertMention(mentionMembers[mentionActive]);
        else setTip("请先选择群成员，或按 Esc 关闭选择", false);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); }
    if (event.key === "Escape") clearReply();
  });
  form.addEventListener("submit", function (event) { event.preventDefault(); send(); });
  capabilities().catch(function () {});
  refreshControls();
})();
