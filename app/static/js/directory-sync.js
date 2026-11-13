(() => {
  const root = document.querySelector('[data-directory-sync-root]');
  if (!root) return;
  const form = root.querySelector('[data-directory-sync]');
  const monitor = root.querySelector('[data-directory-sync-monitor]');
  const summary = root.querySelector('[data-directory-sync-summary]');
  const counter = root.querySelector('[data-directory-sync-counter]');
  const list = root.querySelector('[data-directory-sync-jobs]');
  const progress = root.querySelector('[data-directory-sync-progress]');
  const track = root.querySelector('[data-directory-sync-track]');
  const dismiss = root.querySelector('[data-directory-sync-dismiss]');
  const submit = form.querySelector('button[type="submit"]');
  const originalLabel = submit.textContent;
  const subject = originalLabel.trim().replace(/^同步/, '');
  const header = form.closest('.page-header');
  if (header) header.insertAdjacentElement('afterend', monitor);
  const storageKey = `fulcrum:directory-sync:${window.location.pathname}`;
  let active = false;
  let leaving = false;
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function save(record) {
    try { sessionStorage.setItem(storageKey, JSON.stringify(record)); } catch (_) { /* 仍可使用网址中的任务编号恢复监控。 */ }
  }

  function clearSaved() {
    try { sessionStorage.removeItem(storageKey); } catch (_) { /* 浏览器可能禁止本地存储。 */ }
  }

  function setBusy(value) {
    active = value;
    submit.disabled = value;
    submit.textContent = value ? '同步中…' : originalLabel;
    const select = form.querySelector('select');
    if (select) select.disabled = value;
  }

  function cleanUrl() {
    const url = new URL(window.location.href);
    ['notice', 'sync_jobs', 'sync_refreshed'].forEach((name) => url.searchParams.delete(name));
    return url;
  }

  async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal});
      let body;
      try { body = await response.json(); }
      catch (_) { throw new Error('服务器未返回同步状态，请确认登录状态。'); }
      if (!response.ok) {
        const error = new Error(typeof body.detail === 'string' ? body.detail : '同步请求失败，请稍后重试。');
        error.terminal = [400, 401, 403, 404].includes(response.status);
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时，正在重新读取同步状态…');
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function render(body) {
    monitor.hidden = false;
    monitor.dataset.state = !body.done ? 'running' : body.failed ? 'error' : 'success';
    const title = !body.done ? '同步中' : body.failed === body.jobs.length ? '同步未完成' : body.failed ? '同步结束' : '同步完成';
    summary.textContent = `${subject}${title}`;
    counter.textContent = !body.done ? `已完成 ${body.successful + body.failed} / ${body.jobs.length} 个账号`
      : body.failed ? `${body.successful} 个成功 · ${body.failed} 个未完成`
      : `${body.jobs.length} 个账号 · ${body.count} ${subject === '好友' ? '位好友' : '个群聊'}`;
    progress.max = Math.max(body.jobs.length, 1);
    progress.value = body.successful + body.failed;
    track.classList.toggle('is-indeterminate', !body.done && progress.value === 0);
    list.replaceChildren();
    body.jobs.forEach((job) => {
      const row = document.createElement('div');
      row.className = 'directory-sync-job';
      row.dataset.state = job.state;
      const name = document.createElement('strong');
      name.className = 'directory-sync-account';
      name.textContent = job.channel_name;
      const status = document.createElement('span');
      status.className = 'directory-sync-status';
      status.textContent = job.message;
      const elapsed = document.createElement('span');
      elapsed.className = 'directory-sync-elapsed';
      elapsed.textContent = `耗时 ${Math.max(0, Number(job.elapsed_seconds) || 0)} 秒`;
      row.append(name, status, elapsed);
      list.appendChild(row);
    });
    dismiss.hidden = !body.done;
  }

  async function follow(record) {
    setBusy(true);
    monitor.hidden = false;
    dismiss.hidden = true;
    monitor.dataset.state = 'running';
    track.classList.add('is-indeterminate');
    summary.textContent = '正在读取同步进度…';
    counter.textContent = '';
    const statusUrl = `${form.action}/status?job_ids=${record.ids.join(',')}`;
    let failures = 0;
    try {
      while (!leaving) {
        let body;
        try {
          body = await request(statusUrl, {headers: {Accept: 'application/json'}});
          if (!Array.isArray(body.jobs) || typeof body.done !== 'boolean') throw new Error('同步状态格式异常，请稍后重试。');
          failures = 0;
        } catch (error) {
          if (error.terminal || ++failures >= 4) {
            if (error.terminal) clearSaved();
            throw error;
          }
          summary.textContent = '连接暂时中断，正在重新读取同步状态…';
          await delay(3000);
          continue;
        }
        if (leaving) return;
        render(body);
        if (body.done) {
          save({...record, refreshed: true});
          if (body.successful > 0 && !record.refreshed) {
            // 完成后只自动刷新一次；仅保留任务编号，刷新后重新读取服务端结果。
            summary.textContent = `${body.summary}，正在更新列表…`;
            const url = cleanUrl();
            url.searchParams.set('sync_jobs', record.ids.join(','));
            url.searchParams.set('sync_refreshed', '1');
            window.location.replace(url.href);
            return;
          }
          history.replaceState(null, '', cleanUrl());
          return;
        }
        await delay(1500);
      }
    } catch (error) {
      if (!leaving) {
        monitor.dataset.state = 'error';
        track.classList.remove('is-indeterminate');
        summary.textContent = error.message || '同步状态读取失败，请稍后重试。';
        counter.textContent = '';
        dismiss.hidden = false;
      }
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (active) return;
    const body = new URLSearchParams(new FormData(form));
    setBusy(true);
    monitor.hidden = false;
    dismiss.hidden = true;
    list.replaceChildren();
    monitor.dataset.state = 'running';
    track.classList.add('is-indeterminate');
    progress.max = 1;
    progress.value = 0;
    summary.textContent = '正在提交同步…';
    counter.textContent = '';
    try {
      const response = await request(form.action, {method: 'POST', body, headers: {Accept: 'application/json', 'X-Requested-With': 'fetch'}});
      const ids = Array.isArray(response.jobs) ? response.jobs.map((job) => job.id) : [];
      if (!ids.length || ids.some((id) => !Number.isInteger(id) || id < 1)) throw new Error('未取得同步任务，请稍后重试。');
      const record = {ids, refreshed: false};
      save(record);
      await follow(record);
    } catch (error) {
      monitor.dataset.state = 'error';
      track.classList.remove('is-indeterminate');
      summary.textContent = error.message || '同步提交失败，请稍后重试。';
      dismiss.hidden = false;
      setBusy(false);
    }
  });

  dismiss.addEventListener('click', () => {
    if (active) return;
    monitor.hidden = true;
    clearSaved();
    history.replaceState(null, '', cleanUrl());
  });
  window.addEventListener('pagehide', () => { leaving = true; });
  window.addEventListener('pageshow', () => { leaving = false; });

  let record;
  const url = new URL(window.location.href);
  if (url.searchParams.has('sync_jobs')) {
    record = {ids: (url.searchParams.get('sync_jobs') || '').split(',').map(Number), refreshed: url.searchParams.get('sync_refreshed') === '1'};
  } else {
    try { record = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) { /* 损坏或不可用的存储不影响列表。 */ }
  }
  if (record && Array.isArray(record.ids) && record.ids.length && record.ids.length <= 200 && record.ids.every((id) => Number.isInteger(id) && id > 0)) {
    follow(record);
  }
})();
