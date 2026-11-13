/* 普通操作反馈复用群/好友同步面板；任务真正完成后才显示成功并自动更新页面。 */
(() => {
  const monitor = document.querySelector('[data-action-monitor]');
  if (!monitor) return;
  const forms = Array.from(document.querySelectorAll('form[data-action-feedback]'));
  const summary = monitor.querySelector('[data-action-summary]');
  const counter = monitor.querySelector('[data-action-counter]');
  const list = monitor.querySelector('[data-action-jobs]');
  const progress = monitor.querySelector('[data-action-progress]');
  const track = monitor.querySelector('[data-action-track]');
  const dismiss = monitor.querySelector('[data-action-dismiss]');
  const storageKey = `fulcrum:action-feedback:${window.location.pathname}`;
  // form.elements 包含通过 form="…" 关联的外部按钮，插件页隐藏表单也要同步忙碌状态。
  const controls = forms.flatMap((form) => Array.from(form.elements)
    .filter((button) => ['BUTTON', 'INPUT'].includes(button.tagName) && button.type === 'submit')
    .map((button) => ({button, form, disabled: button.disabled, text: button.textContent})));
  let active = false, leaving = false, run = 0, controller = null;
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function save(record) {
    try { sessionStorage.setItem(storageKey, JSON.stringify(record)); } catch (_) { /* 刷新时仍可从网址恢复任务。 */ }
  }
  function clearSaved() {
    try { sessionStorage.removeItem(storageKey); } catch (_) { /* 不影响本页反馈。 */ }
  }
  function cleanUrl() {
    const url = new URL(window.location.href);
    ['notice', 'tone', 'action_status', 'action_label', 'action_refreshed', 'operation_jobs', 'operation_channel'].forEach((name) => url.searchParams.delete(name));
    return url;
  }
  function statusUrl(raw) {
    if (typeof raw !== 'string') throw new Error('服务器未返回可用的操作状态地址。');
    let url;
    try { url = new URL(raw, window.location.href); }
    catch (_) { throw new Error('操作状态地址无效，未继续读取。'); }
    const ids = (url.searchParams.get('job_ids') || '').split(',');
    if (url.origin !== window.location.origin || url.username || url.password || url.hash
      || !/^\/channels\/[1-9]\d*\/operations\/status$/.test(url.pathname)
      || ids.length > 200 || ids.some((id) => !/^[1-9]\d{0,17}$/.test(id))) {
      throw new Error('操作状态地址无效，未继续读取。');
    }
    return url.pathname + url.search;
  }
  function setBusy(value, form) {
    active = value;
    controls.forEach(({button, form: owner, disabled, text}) => {
      button.disabled = value || disabled;
      if (button.tagName === 'BUTTON') button.textContent = value && owner === form ? '处理中…' : text;
    });
    monitor.setAttribute('aria-busy', String(value));
  }
  function showError(message) {
    monitor.hidden = false;
    monitor.dataset.state = 'error';
    summary.textContent = message;
    counter.textContent = '';
    track.hidden = true;
    dismiss.hidden = false;
  }
  function render(body, label) {
    const jobs = Array.isArray(body.jobs) ? body.jobs : [];
    monitor.hidden = false;
    monitor.dataset.state = !body.done ? 'running' : body.failed ? 'error' : 'success';
    summary.textContent = body.summary || `${label}${body.done ? '已完成' : '进行中…'}`;
    counter.textContent = jobs.length > 1 ? `已完成 ${Number(body.successful || 0) + Number(body.failed || 0)} / ${jobs.length}` : '';
    track.hidden = Boolean(body.done && !jobs.length);
    progress.max = Math.max(jobs.length, 1);
    progress.value = Number(body.successful || 0) + Number(body.failed || 0);
    track.classList.toggle('is-indeterminate', !body.done && !progress.value);
    list.replaceChildren();
    jobs.forEach((job) => {
      const row = document.createElement('div');
      row.className = 'directory-sync-job';
      row.dataset.state = job.state;
      const name = document.createElement('strong');
      name.className = 'directory-sync-account';
      name.textContent = job.channel_name || label;
      const status = document.createElement('span');
      status.className = 'directory-sync-status';
      status.textContent = job.message || '等待处理';
      const elapsed = document.createElement('span');
      elapsed.className = 'directory-sync-elapsed';
      elapsed.textContent = `耗时 ${Math.max(0, Number(job.elapsed_seconds) || 0)} 秒`;
      row.append(name, status, elapsed);
      list.appendChild(row);
    });
    dismiss.hidden = !body.done;
  }
  async function request(url, options = {}) {
    const activeController = new AbortController();
    controller = activeController;
    const timer = setTimeout(() => activeController.abort(), options.method === 'POST' ? 60000 : 15000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options, signal: activeController.signal});
      let body;
      try { body = await response.json(); }
      catch (_) { throw new Error('服务器未返回操作状态，请确认登录是否仍然有效。'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('操作状态格式异常，请稍后重试。');
      if (!response.ok || body.ok === false) {
        const error = new Error(body.summary || body.notice || (typeof body.detail === 'string' && body.detail) || '操作失败，请稍后重试。');
        error.terminal = [400, 401, 403, 404].includes(response.status);
        throw error;
      }
      if (typeof body.done !== 'boolean') throw new Error('操作状态格式异常，请稍后重试。');
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error(options.method === 'POST' ? '提交结果未确认，请勿重复操作。' : '读取状态超时，正在重新连接…');
      throw error;
    } finally {
      clearTimeout(timer);
      if (controller === activeController) controller = null;
    }
  }
  function complete(body, record) {
    render(body, record.label);
    const updated = {...record, result: body, refreshed: true};
    save(updated);
    if (Number(body.successful) > 0 && record.refresh !== false && body.refresh !== false && !record.refreshed) {
      // 任务标识放入网址作为存储被禁用时的后备；refreshed 必须在导航前写入，防止循环刷新。
      const url = cleanUrl();
      if (record.status_url) {
        url.searchParams.set('action_status', record.status_url);
        url.searchParams.set('action_label', record.label);
        url.searchParams.set('action_refreshed', '1');
      } else {
        url.searchParams.set('notice', body.summary || `${record.label}已完成`);
        url.searchParams.set('tone', body.failed ? 'danger' : 'success');
      }
      summary.textContent = `${body.summary || '操作完成'}，正在更新页面…`;
      window.location.replace(url.href);
      return;
    }
    window.history.replaceState(null, '', cleanUrl());
  }
  async function follow(record, token) {
    const until = Date.now() + 5 * 60 * 1000;
    let failures = 0;
    while (!leaving && token === run) {
      if (Date.now() > until) throw new Error('等待操作结果超时，任务可能仍在执行。');
      let body;
      try {
        body = await request(record.status_url, {headers: {Accept: 'application/json'}});
        failures = 0;
      } catch (error) {
        if (leaving || token !== run) return;
        if (error.terminal || ++failures >= 4) throw error;
        summary.textContent = '连接暂时中断，正在重新读取操作结果…';
        await delay(3000);
        continue;
      }
      if (leaving || token !== run) return;
      render(body, record.label);
      if (body.done) { complete(body, record); return; }
      await delay(1500);
    }
  }
  async function resume(record) {
    const token = ++run;
    setBusy(true);
    try {
      record.status_url = statusUrl(record.status_url);
      summary.textContent = '正在读取操作进度…';
      monitor.hidden = false;
      await follow(record, token);
    } catch (error) {
      if (!leaving && token === run) showError(error.message);
    } finally {
      if (token === run) setBusy(false);
    }
  }
  forms.forEach((form) => form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (active) return;
    const token = ++run;
    const label = form.dataset.actionLabel || event.submitter?.textContent?.trim() || '操作';
    const payload = new FormData(form);
    if (event.submitter?.name) payload.append(event.submitter.name, event.submitter.value);
    const csrf = payload.get('csrf_token') || document.querySelector('meta[name="csrf-token"]')?.content || '';
    const record = {label, refreshed: false, refresh: true, created_at: Date.now()};
    clearSaved();
    setBusy(true, form);
    render({done: false, summary: `正在${label}…`, jobs: []}, label);
    try {
      const action = new URL(form.action, window.location.href);
      if (action.origin !== window.location.origin) throw new Error('操作地址无效，未提交请求。');
      const body = await request(action.href, {method: 'POST', body: payload, headers: {Accept: 'application/json', 'X-Requested-With': 'fetch', 'X-CSRF-Token': csrf}});
      if (leaving || token !== run) return;
      record.refresh = body.refresh !== false;
      if (body.done) complete(body, record);
      else {
        record.status_url = statusUrl(body.status_url);
        save(record);
        render(body, label);
        await follow(record, token);
      }
    } catch (error) {
      if (!leaving && token === run) showError(error.message || '操作未完成，请稍后重试。');
    } finally {
      if (token === run) setBusy(false);
    }
  }));
  dismiss.addEventListener('click', () => {
    if (active) return;
    monitor.hidden = true;
    clearSaved();
    window.history.replaceState(null, '', cleanUrl());
  });
  function restore() {
    const url = new URL(window.location.href);
    let record;
    if (url.searchParams.has('operation_jobs') || url.searchParams.has('operation_channel')) {
      record = {status_url: `/channels/${url.searchParams.get('operation_channel')}/operations/status?job_ids=${url.searchParams.get('operation_jobs')}`, label: '操作', refreshed: false, created_at: Date.now()};
    } else if (url.searchParams.has('action_status')) {
      record = {status_url: url.searchParams.get('action_status'), label: (url.searchParams.get('action_label') || '操作').slice(0, 100), refreshed: url.searchParams.get('action_refreshed') === '1', created_at: Date.now()};
    } else {
      try { record = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) { /* 无存储仍支持本页操作。 */ }
    }
    if (record && Date.now() - Number(record.created_at) < 30 * 60 * 1000) {
      if (record.result?.done) { render(record.result, record.label); window.history.replaceState(null, '', cleanUrl()); }
      else if (record.status_url) resume(record);
    }
  }
  window.addEventListener('pagehide', () => { leaving = true; run += 1; controller?.abort(); setBusy(false); });
  window.addEventListener('pageshow', (event) => { leaving = false; if (event.persisted) restore(); });
  restore();
})();
