(() => {
  const root = document.getElementById('contact-check');
  if (!root) return;
  const byId = (name) => document.getElementById(`contact-check-${name}`);
  const channel = byId('channel'), start = byId('start'), stop = byId('stop');
  const restart = byId('restart'), interval = byId('interval'), status = byId('status');
  const progress = byId('progress'), actionStatus = byId('action-status'), actionResume = byId('action-resume');
  const remarkModal = byId('remark-modal'), remarkForm = byId('remark-form'), suffix = byId('remark-suffix');
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const categories = ['friend', 'deleted', 'unknown'];
  const titles = {friend: '互相是好友', deleted: '可能已被删除', unknown: '状态未知'};
  const tables = Object.fromEntries(categories.map((key) => [key, {rows: [], lookup: new Map(), page: 1, size: 50, selected: new Set()}]));
  let queue = null, cursor = 0, total = 0, pending = null;
  let busy = '', stopRequested = false, leaving = false, wakeInterval = null;
  let elapsed = 0, activeSince = null, samples = [], actionQueue = null, remarkItems = [];
  const editable = root.dataset.canEdit === 'true';
  const base = () => `/contacts/check/${encodeURIComponent(channel.value)}`;
  const query = (key, category) => root.querySelector(`[data-${key}="${category}"]`);
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const elapsedMs = () => elapsed + (activeSince === null ? 0 : performance.now() - activeSince);
  const intervalMs = () => Math.min(60, Math.max(0, Number(interval.value) || 0)) * 1000;
  const blocked = () => Boolean(busy || actionQueue || !editable);
  const actionsBlocked = () => blocked() || Boolean(pending);
  const available = (item) => !item.removed;

  function duration(ms) {
    const seconds = Math.max(0, Math.ceil(ms / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
  }

  function renderProgress() {
    byId('count').textContent = `已检测 ${cursor} / ${total} 位 · ${total ? Math.floor(cursor / total * 100) : 0}%`;
    progress.max = Math.max(total, 1);
    progress.value = cursor;
    byId('elapsed').textContent = `已用时 ${duration(elapsedMs())}`;
    const average = samples.length ? samples.reduce((sum, value) => sum + value, 0) / samples.length : 3000;
    const estimate = total * average + Math.max(0, total - 1) * intervalMs();
    const remainingCount = total - cursor;
    const remaining = Math.max(0, remainingCount * average + Math.max(0, remainingCount - 1) * intervalMs()
      - (pending ? Math.min(average, elapsedMs() - pending.began) : 0));
    byId('estimate').textContent = total ? `预计总时长 ${duration(estimate)}${samples.length ? '' : '（按每位约 3 秒预估）'}` : '';
    byId('remaining').textContent = total > cursor ? `预计剩余 ${duration(remaining)}` : '';
  }

  function renderControls() {
    const locked = Boolean(busy || actionQueue);
    channel.disabled = locked || Boolean(pending) || !channel.value;
    interval.disabled = locked;
    start.disabled = locked || !channel.value || !editable || Boolean(queue && cursor >= total);
    start.textContent = queue && cursor < total ? '继续检测' : (queue ? '检测已完成' : '开始检测');
    stop.disabled = busy !== 'scan' || stopRequested;
    stop.textContent = stopRequested && busy === 'scan' ? '等待当前检测完成…' : '停止检测';
    restart.hidden = !queue;
    restart.disabled = locked || Boolean(pending) || !editable;
    actionResume.hidden = !actionQueue || Boolean(busy);
    root.querySelectorAll('[data-row-action], [data-batch-action]').forEach((button) => {
      const table = tables[button.dataset.category];
      const item = button.dataset.contactId ? table.lookup.get(button.dataset.contactId) : null;
      button.disabled = actionsBlocked() || (item ? !available(item) : !table.selected.size);
    });
    categories.filter((key) => key !== 'friend').forEach((key) => {
      const table = tables[key];
      query('select-all', key).disabled = actionsBlocked() || !table.rows.some(available);
      query('clear-selection', key).disabled = actionsBlocked() || !table.selected.size;
      query('selection-count', key).textContent = `已选 ${table.selected.size} 位`;
      const pageRows = table.rows.slice((table.page - 1) * table.size, table.page * table.size).filter(available);
      const checkbox = query('select-page', key);
      checkbox.disabled = actionsBlocked() || !pageRows.length;
      checkbox.checked = pageRows.length > 0 && pageRows.every((row) => table.selected.has(String(row.contact.id)));
      checkbox.indeterminate = !checkbox.checked && pageRows.some((row) => table.selected.has(String(row.contact.id)));
    });
    root.querySelectorAll('[data-select-contact]').forEach((checkbox) => {
      const item = tables[checkbox.dataset.category].lookup.get(checkbox.value);
      checkbox.disabled = actionsBlocked() || !available(item);
      checkbox.checked = tables[checkbox.dataset.category].selected.has(checkbox.value);
    });
  }

  async function request(url, method = 'GET', payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        method, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: {'Accept': 'application/json', ...(method === 'POST' ? {'X-CSRF-Token': csrf} : {}), ...(payload ? {'Content-Type': 'application/json'} : {})},
        ...(payload ? {body: JSON.stringify(payload)} : {}),
      });
      let body;
      try { body = await response.json(); }
      catch (_) { throw new Error('服务器未返回有效结果，请检查登录状态。'); }
      if (!response.ok) {
        const error = new Error(typeof body.detail === 'string' ? body.detail : '请求失败，请稍后重试。');
        error.retryable = response.status >= 500 || response.status === 429;
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === 'AbortError') {
        const timeout = new Error('请求超时，请检查网络连接。');
        timeout.retryable = true;
        throw timeout;
      }
      if (error instanceof TypeError) error.retryable = true;
      throw error;
    } finally { clearTimeout(timer); }
  }

  async function waitForResult(jobId, action = 'recheck') {
    let failures = 0;
    while (!leaving) {
      let body;
      try {
        body = await request(`${base()}/${action === 'recheck' ? 'jobs' : 'actions'}/${jobId}/poll`, 'POST');
        failures = 0;
      } catch (error) {
        // 只重试同一任务的轮询，不能重发创建命令，以免重复操作微信好友。
        if (!error.retryable || ++failures > 3) throw error;
        (busy === 'scan' ? status : actionStatus).textContent = '网络暂时中断，正在重试当前任务结果…';
        await delay(1000);
        continue;
      }
      if (body.state === 'completed') {
        const result = body.result;
        if (!result || !result.contact || (action === 'recheck' ? !categories.includes(result.category) : typeof result.ok !== 'boolean' || result.action !== action)) {
          throw new Error('任务结果格式异常，已停止后续操作。');
        }
        return result;
      }
      if (!['pending', 'running'].includes(body.state)) throw new Error('任务状态异常，已停止后续操作。');
      await delay(1000);
    }
    return null;
  }

  function appendContact(cell, contact) {
    const name = String(contact.name || contact.external_id || '好友');
    const identity = document.createElement('span');
    identity.className = 'identity-cell identity-lg';
    const avatar = document.createElement('span');
    avatar.className = 'identity-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    const initial = document.createElement('span');
    initial.textContent = Array.from(name.trim())[0] || '友';
    avatar.appendChild(initial);
    if (contact.avatar_url) {
      try {
        const url = new URL(contact.avatar_url, window.location.origin);
        if (['http:', 'https:'].includes(url.protocol)) {
          const image = document.createElement('img');
          image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
          image.addEventListener('error', () => avatar.replaceChildren(initial), {once: true});
          image.src = url.href;
          avatar.replaceChildren(image);
        }
      } catch (_) { /* 无效头像使用名字首字。 */ }
    }
    const text = document.createElement('span');
    text.className = 'identity-text';
    const label = document.createElement('strong');
    label.className = 'identity-name'; label.textContent = name; label.title = name;
    text.appendChild(label); identity.append(avatar, text); cell.appendChild(identity);
  }

  function renderCategory(category, renderRows = true) {
    const table = tables[category], length = table.rows.length;
    const pages = Math.max(1, Math.ceil(length / table.size));
    table.page = Math.min(table.page, pages);
    const rows = table.rows.slice((table.page - 1) * table.size, table.page * table.size);
    const fragment = document.createDocumentFragment();
    if (renderRows) rows.forEach((item) => {
      const row = document.createElement('tr');
      row.dataset.contactId = String(item.contact.id);
      if (item.removed) row.className = 'contact-check-removed';
      if (category !== 'friend') {
        const cell = row.insertCell(), checkbox = document.createElement('input');
        checkbox.type = 'checkbox'; checkbox.value = String(item.contact.id);
        checkbox.dataset.selectContact = ''; checkbox.dataset.category = category;
        checkbox.setAttribute('aria-label', `选择 ${item.contact.remark || item.contact.name || item.contact.external_id}`);
        cell.appendChild(checkbox);
      }
      appendContact(row.insertCell(), item.contact);
      [item.contact.remark, item.contact.external_id, item.pay_name_last_char].forEach((value) => { row.insertCell().textContent = value || '—'; });
      const resultCell = row.insertCell();
      const reason = document.createElement('span');
      reason.textContent = item.reason || '—';
      resultCell.appendChild(reason);
      if (item.recheckCategory) {
        const note = document.createElement('strong');
        note.className = `contact-check-row-note${item.recheckCategory !== category ? ' contact-check-changed' : ''}`;
        note.textContent = `复查：${titles[item.recheckCategory]}${item.recheckCategory !== category ? '（状态变化）' : ''}`;
        resultCell.appendChild(note);
      }
      if (item.message) {
        const note = document.createElement('span');
        note.className = 'contact-check-row-note'; note.textContent = item.message;
        resultCell.appendChild(note);
      }
      if (category !== 'friend') {
        const actions = row.insertCell(), group = document.createElement('div');
        group.className = 'contact-check-row-actions';
        [['recheck', '重新检查'], ['remark', '备注后缀'], ['delete', '删除好友']].forEach(([action, text]) => {
          const button = document.createElement('button');
          button.type = 'button'; button.className = `btn ${action === 'delete' ? 'btn-danger-soft' : 'btn-ghost'}`;
          button.textContent = text; button.dataset.rowAction = action; button.dataset.category = category; button.dataset.contactId = String(item.contact.id);
          button.setAttribute('aria-label', `${text}：${item.contact.remark || item.contact.name || item.contact.external_id}`);
          group.appendChild(button);
        });
        actions.appendChild(group);
      }
      fragment.appendChild(row);
    });
    // 几千位好友保存在内存中，每个分类只创建当前页的行和头像节点。
    if (renderRows) query('result-body', category).replaceChildren(fragment);
    query('result-count', category).textContent = String(length);
    query('result-empty', category).hidden = length > 0;
    query('pagination', category).hidden = !length;
    query('page-summary', category).textContent = `第 ${(table.page - 1) * table.size + 1}–${Math.min(table.page * table.size, length)} 位，共 ${length} 位`;
    query('page-label', category).textContent = `${table.page} / ${pages}`;
    query('page-prev', category).disabled = table.page <= 1;
    query('page-next', category).disabled = table.page >= pages;
    renderControls();
  }

  function resetResults() {
    queue = null; cursor = 0; pending = null; elapsed = 0; samples = [];
    total = Number(channel.selectedOptions[0]?.dataset.total || 0);
    categories.forEach((key) => { tables[key].rows = []; tables[key].lookup.clear(); tables[key].page = 1; tables[key].selected.clear(); renderCategory(key); });
    actionStatus.hidden = true;
    renderProgress(); renderControls();
  }

  async function waitInterval() {
    await new Promise((resolve) => {
      const timer = setTimeout(() => { wakeInterval = null; resolve(); }, intervalMs());
      wakeInterval = () => { clearTimeout(timer); wakeInterval = null; resolve(); };
    });
  }

  async function runScan() {
    if (blocked() || !channel.value || (queue && cursor >= total)) return;
    if (!interval.checkValidity()) { interval.reportValidity(); return; }
    busy = 'scan'; stopRequested = false; activeSince = performance.now(); renderControls();
    try {
      if (!queue) {
        status.textContent = '正在读取全部好友…';
        const body = await request(`${base()}/friends`);
        if (!Array.isArray(body.contacts)) throw new Error('好友列表格式异常，请稍后重试。');
        queue = body.contacts; total = queue.length; renderProgress();
      }
      while (cursor < total && !stopRequested && !leaving) {
        const contact = queue[cursor];
        status.textContent = `正在检测 ${cursor + 1} / ${total}：${contact.remark || contact.name || contact.external_id}`;
        if (!pending) {
          const began = elapsedMs();
          const job = await request(`${base()}/friends/${encodeURIComponent(contact.id)}`, 'POST');
          if (!Number.isInteger(job.job_id) || job.job_id < 1) throw new Error('检测任务创建结果异常。');
          pending = {id: job.job_id, began};
        }
        const result = await waitForResult(pending.id);
        if (leaving || !result) break;
        samples.push(elapsedMs() - pending.began);
        if (samples.length > 20) samples.shift();
        const table = tables[result.category], item = {...result};
        table.rows.push(item); table.lookup.set(String(item.contact.id), item);
        pending = null; cursor += 1;
        renderCategory(result.category, table.rows.length <= table.page * table.size); renderProgress();
        if (cursor < total && !stopRequested) {
          status.textContent = `已检测 ${cursor} / ${total}，等待下一位…`;
          await waitInterval();
        }
      }
      if (!leaving) status.textContent = cursor === total
        ? (total ? `检测完成，共 ${total} 位好友` : '没有可检测的好友，请先同步好友资料。')
        : `已暂停，保留 ${cursor} 位结果，剩余 ${total - cursor} 位未检测。`;
    } catch (error) {
      if (!leaving) status.textContent = `检测已暂停：${error.message || '网络异常'} 已完成结果已保留，可继续检测。`;
    } finally {
      elapsed = elapsedMs(); activeSince = null; busy = ''; renderControls(); renderProgress();
    }
  }

  function describeAction(action) { return {recheck: '重新检查', remark: '添加备注后缀', delete: '删除好友'}[action]; }

  async function runActions() {
    if (busy || !actionQueue) return;
    busy = 'operation'; actionStatus.hidden = false; renderControls();
    const operation = actionQueue;
    try {
      while (operation.index < operation.items.length && !leaving) {
        const item = operation.items[operation.index];
        actionStatus.textContent = `${describeAction(operation.action)} ${operation.index + 1} / ${operation.items.length}：${item.contact.remark || item.contact.name || item.contact.external_id}`;
        if (!operation.job) {
          const path = `${base()}/friends/${encodeURIComponent(item.contact.id)}${operation.action === 'recheck' ? '' : '/' + operation.action}`;
          const job = await request(path, 'POST', operation.payload);
          if (!Number.isInteger(job.job_id) || job.job_id < 1) throw new Error('操作任务创建结果异常。');
          operation.job = job.job_id;
        }
        const result = await waitForResult(operation.job, operation.action);
        if (leaving || !result) break;
        if (operation.action === 'recheck') {
          item.recheckCategory = result.category;
          item.contact = {...item.contact, ...result.contact};
          item.pay_name_last_char = result.pay_name_last_char;
          item.reason = result.reason;
          if (result.category !== item.category) operation.changed += 1;
          operation.ok += 1;
          item.message = '';
        } else {
          item.message = result.message || (result.ok ? '操作完成' : '操作失败');
          if (result.ok) {
            operation.ok += 1;
            if (operation.action === 'remark') item.contact = {...item.contact, ...result.contact};
            if (operation.action === 'delete') {
              item.removed = true; item.message = '已删除微信好友'; tables[item.category].selected.delete(String(item.contact.id));
              if (queue) {
                queue = queue.slice(0, cursor).concat(queue.slice(cursor).filter((contact) => String(contact.id) !== String(item.contact.id)));
                total = queue.length; renderProgress();
              }
            }
          } else operation.failed += 1;
        }
        operation.job = null; operation.index += 1;
        renderCategory(item.category);
        if (operation.index < operation.items.length) await delay(intervalMs());
      }
      if (!leaving && operation.index === operation.items.length) {
        actionStatus.textContent = `${describeAction(operation.action)}完成：成功 ${operation.ok} 位${operation.failed ? `，失败 ${operation.failed} 位` : ''}${operation.action === 'recheck' ? `，状态变化 ${operation.changed} 位（保留在原表）` : ''}。`;
        actionQueue = null;
      }
    } catch (error) {
      if (operation.job) {
        actionStatus.textContent = `操作已暂停：${error.message} 请继续查询当前任务结果。`;
      } else {
        // 创建请求不自动重发；网络中断时无法确定手机是否已收到命令。
        actionStatus.textContent = `操作已停止：${error.message} 当前提交未自动重试，请核实好友状态后再操作。`;
        actionQueue = null;
      }
    } finally { busy = ''; renderControls(); }
  }

  async function beginAction(action, items, payload) {
    if (actionsBlocked() || !items.length) return;
    if (action === 'remark' && !payload) {
      remarkItems = items; suffix.value = '- 单删';
      byId('remark-count').textContent = `为 ${items.length} 位好友的当前备注（无备注时使用昵称）添加后缀。`;
      updateRemarkPreview();
      busy = 'confirm'; renderControls(); remarkModal.showModal(); suffix.focus(); return;
    }
    if (action === 'delete') {
      busy = 'confirm'; renderControls();
      const confirmed = await window.appDialog.confirm({
        title: `删除 ${items.length} 位微信好友？`,
        message: `这将从微信通讯录实际删除所选的 ${items.length} 位好友，无法撤销。`,
        tone: 'danger', confirmText: `确认删除 ${items.length} 位好友`, cancelText: '取消',
        matchText: items.length > 1 ? String(items.length) : '', matchLabel: `请输入人数 ${items.length} 以确认：`,
      });
      busy = ''; renderControls();
      if (!confirmed || leaving) return;
      payload = {confirmed: true};
    }
    actionQueue = {action, items: [...items], payload, index: 0, job: null, ok: 0, failed: 0, changed: 0};
    await runActions();
  }

  function updateRemarkPreview() {
    const contact = remarkItems[0]?.contact || {};
    byId('remark-preview').textContent = `${contact.remark || contact.name || contact.external_id || ''}${suffix.value}`;
  }

  root.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    for (const [attribute, direction] of [['pagePrev', -1], ['pageNext', 1]]) {
      if (button.dataset[attribute]) { const key = button.dataset[attribute]; tables[key].page += direction; renderCategory(key); return; }
    }
    if (button.dataset.selectAll) {
      const key = button.dataset.selectAll;
      tables[key].rows.filter(available).forEach((item) => tables[key].selected.add(String(item.contact.id)));
      renderControls();
    }
    if (button.dataset.clearSelection) { tables[button.dataset.clearSelection].selected.clear(); renderControls(); }
    if (button.dataset.rowAction || button.dataset.batchAction) {
      const table = tables[button.dataset.category];
      const items = table.rows.filter((item) => available(item) && (button.dataset.rowAction ? String(item.contact.id) === button.dataset.contactId : table.selected.has(String(item.contact.id))));
      void beginAction(button.dataset.rowAction || button.dataset.batchAction, items);
    }
  });
  root.addEventListener('change', (event) => {
    const field = event.target;
    if (field.dataset.pageSize) { const key = field.dataset.pageSize; tables[key].size = Number(field.value); tables[key].page = 1; renderCategory(key); }
    if (field.dataset.selectPage) {
      const table = tables[field.dataset.selectPage];
      table.rows.slice((table.page - 1) * table.size, table.page * table.size).filter(available).forEach((item) => {
        if (field.checked) table.selected.add(String(item.contact.id)); else table.selected.delete(String(item.contact.id));
      });
      renderControls();
    }
    if (field.hasAttribute('data-select-contact')) {
      const selected = tables[field.dataset.category].selected;
      if (field.checked) selected.add(field.value); else selected.delete(field.value);
      renderControls();
    }
  });
  start.addEventListener('click', () => void runScan());
  restart.addEventListener('click', async () => {
    if (actionsBlocked()) return;
    busy = 'confirm'; renderControls();
    const confirmed = await window.appDialog.confirm({title: '清空结果并重新开始？', message: '当前检测结果、分页选择和未完成进度会清空，所有好友将重新检测。', confirmText: '清空并重新开始'});
    busy = ''; renderControls();
    if (confirmed && !leaving) { resetResults(); void runScan(); }
  });
  stop.addEventListener('click', () => {
    if (busy !== 'scan') return;
    stopRequested = true;
    status.textContent = pending ? '正在停止：当前好友检测完成后暂停。' : '正在暂停检测…';
    if (wakeInterval) wakeInterval();
    renderControls();
  });
  channel.addEventListener('change', () => { if (!actionsBlocked()) { resetResults(); status.textContent = '尚未开始检测'; } });
  interval.addEventListener('input', renderProgress);
  actionResume.addEventListener('click', () => void runActions());
  suffix.addEventListener('input', updateRemarkPreview);
  remarkForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!suffix.value.trim()) { suffix.setCustomValidity('请输入备注后缀'); suffix.reportValidity(); return; }
    const items = remarkItems, value = suffix.value;
    remarkModal.close(); busy = ''; renderControls();
    void beginAction('remark', items, {suffix: value});
  });
  suffix.addEventListener('input', () => suffix.setCustomValidity(''));
  remarkModal.addEventListener('close', () => { if (busy === 'confirm') { busy = ''; renderControls(); } });
  window.addEventListener('pagehide', () => { leaving = true; stopRequested = true; if (wakeInterval) wakeInterval(); });
  window.addEventListener('pageshow', () => { leaving = false; });
  setInterval(() => { if (busy === 'scan') renderProgress(); }, 1000);
  resetResults();
})();
