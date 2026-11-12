(() => {
  const form = document.getElementById('chatroom-plugin-batch');
  if (!form) return;
  const key = form.dataset.selectionKey;
  const rooms = Array.from(document.querySelectorAll('[data-scope-room]'));
  const available = rooms.filter((room) => !room.disabled);
  const pageIds = new Set(available.map((room) => room.value));
  const selectPage = document.querySelector('[data-select-page]');
  const count = form.querySelector('[data-selection-count]');
  const notice = form.querySelector('[data-selection-notice]');
  const hiddenInputs = form.querySelector('[data-cross-page-inputs]');
  const clear = form.querySelector('[data-clear-selection]');
  const selectMatching = form.querySelector('[data-select-matching]');
  const matchingIds = JSON.parse(selectMatching?.dataset.roomIds || '[]').map(String);
  const buttons = Array.from(form.querySelectorAll('[data-batch-submit]'));
  let selected = new Set();
  let storageAvailable = true;
  let busy = false;
  try {
    if (form.dataset.clearSelection === 'true') sessionStorage.removeItem(key);
    const saved = JSON.parse(sessionStorage.getItem(key) || '[]');
    if (Array.isArray(saved)) selected = new Set(saved.filter((id) => /^\d+$/.test(String(id))).slice(0, 1000).map(String));
  } catch (_) {
    storageAvailable = false;
    notice.textContent = '浏览器无法保存跨页选择，请在离开本页前提交；仍可全选筛选结果。';
  }
  if (form.dataset.clearSelection === 'true') {
    const url = new URL(window.location.href);
    url.searchParams.delete('selection_cleared');
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  }
  rooms.filter((room) => room.disabled).forEach((room) => selected.delete(room.value));

  function save() {
    if (!storageAvailable) return;
    try { sessionStorage.setItem(key, JSON.stringify(Array.from(selected))); }
    catch (_) {
      storageAvailable = false;
      notice.textContent = '浏览器无法保存跨页选择，离开页面后可能丢失选择。';
    }
  }

  function render() {
    available.forEach((room) => { room.checked = selected.has(room.value); room.disabled = busy; });
    const selectedHere = available.filter((room) => selected.has(room.value)).length;
    count.textContent = `已选 ${selected.size} 个群（本页 ${selectedHere} 个）`;
    if (selectPage) {
      selectPage.disabled = busy || !available.length;
      selectPage.checked = available.length > 0 && selectedHere === available.length;
      selectPage.indeterminate = selectedHere > 0 && selectedHere < available.length;
    }
    buttons.forEach((button) => { button.disabled = busy || !selected.size; });
    clear.disabled = busy || !selected.size;
    if (selectMatching) selectMatching.disabled = busy || !matchingIds.length || matchingIds.length > 1000;
    // 只补齐不在当前页的 ID，当前页仍由原生复选框提交，保留普通表单能力。
    hiddenInputs.replaceChildren();
    selected.forEach((id) => {
      if (pageIds.has(id)) return;
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'chatroom_ids'; input.value = id;
      hiddenInputs.appendChild(input);
    });
  }

  available.forEach((room) => room.addEventListener('change', () => {
    if (room.checked && selected.size >= 1000 && !selected.has(room.value)) {
      room.checked = false;
      notice.textContent = '每次最多选择 1000 个群聊，请分批操作。';
    } else if (room.checked) selected.add(room.value);
    else selected.delete(room.value);
    save(); render();
  }));
  if (selectPage) selectPage.addEventListener('change', () => {
    const select = selectPage.checked;
    if (select && new Set([...selected, ...pageIds]).size > 1000) {
      notice.textContent = '全选后将超过 1000 个群聊，请取消部分选择后重试。';
    } else available.forEach((room) => { if (select) selected.add(room.value); else selected.delete(room.value); });
    save(); render();
  });
  selectMatching?.addEventListener('click', () => {
    if (!matchingIds.length || matchingIds.length > 1000 || busy) return;
    selected = new Set(matchingIds);
    notice.textContent = `已选择当前筛选结果中的全部 ${selected.size} 个群，包含所有分页，可继续取消个别群。${storageAvailable ? '' : '请在离开本页前提交。'}`;
    save(); render();
  });
  clear.addEventListener('click', () => { selected.clear(); save(); render(); });
  form.addEventListener('submit', async (event) => {
    if (!selected.size || selected.size > 1000) { event.preventDefault(); return; }
    if (!window.fetch) return;
    event.preventDefault();
    const payload = new FormData(form);
    payload.set('mode', event.submitter ? event.submitter.value : buttons[0].value);
    busy = true; render();
    try {
      const response = await fetch(form.action, {method: 'POST', body: new URLSearchParams(payload), credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || result.notice || '批量保存失败，请重试');
      selected.clear(); save();
      window.location.assign(result.redirect_url);
    } catch (error) {
      notice.textContent = error.message || '网络异常，批量保存失败，请重试';
      busy = false; render();
    }
  });
  save(); render();
})();
