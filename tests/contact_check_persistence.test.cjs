// 用浏览器最小替身验证真实脚本的恢复、清空和请求边界，不依赖第三方测试库。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../app/static/js/contact-check.js'), 'utf8');

function element() {
  return {hidden: false, disabled: false, value: '', textContent: '', dataset: {}, handlers: {}, children: [],
    addEventListener(name, callback) { this.handlers[name] = callback; },
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    insertCell() { const cell = element(); this.children.push(cell); return cell; },
    checkValidity() { return true; }, reportValidity() {}, setCustomValidity() {}, focus() {},
    showModal() { this.open = true; }, close() { this.open = false; },
  };
}
const contact = (id) => ({id, external_id: `friend-${id}`, name: `好友 ${id}`, remark: '', avatar_url: ''});
const result = (id, category = 'friend') => ({contact: contact(id), category, reason: '已取得姓名末字', pay_name_last_char: '张'});
const snapshot = (overrides = {}) => ({
  run_id: 10, generation: 'saved-generation', revision: 2, status: 'paused', total: 3, cursor: 2,
  elapsed_ms: 12000, interval_seconds: 1, contacts: [contact(1), contact(2), contact(3)],
  results: [result(1), result(2, 'deleted')], pending: null, ...overrides,
});
const progress = (overrides = {}) => ({run_id: 10, generation: 'saved-generation', cursor: 3, total: 3, elapsed_ms: 13000, ...overrides});
const settle = async () => { for (let index = 0; index < 8; index += 1) await new Promise((resolve) => setImmediate(resolve)); };

function openPage(replies, options = {}) {
  const parts = new Map();
  const names = ['channel', 'start', 'stop', 'restart', 'reload', 'interval', 'status', 'progress', 'action-status',
    'action-resume', 'remark-modal', 'remark-form', 'remark-suffix', 'remark-count', 'remark-preview',
    'count', 'elapsed', 'estimate', 'remaining'];
  names.forEach((name) => parts.set(name, element()));
  parts.get('channel').value = '1';
  parts.get('interval').value = '1';
  parts.get('remark-suffix').value = '- 单删';
  const selectors = new Map();
  const categories = ['friend', 'deleted', 'unknown'];
  const fields = ['result-body', 'result-count', 'result-empty', 'pagination', 'page-summary', 'page-label',
    'page-prev', 'page-next', 'select-all', 'clear-selection', 'selection-count', 'select-page'];
  categories.forEach((category) => fields.forEach((field) => selectors.set(`[data-${field}="${category}"]`, element())));
  const root = Object.assign(element(), {dataset: {canEdit: options.editable === false ? 'false' : 'true'},
    querySelector(selector) { assert.ok(selectors.has(selector), selector); return selectors.get(selector); },
    querySelectorAll() { return []; },
  });
  const state = {requests: [], events: {}, timers: [], confirmations: [], queue: [...replies]};
  const window = {location: {origin: 'http://localhost'}, appDialog: {async confirm(settings) {
    state.confirmations.push(settings); return options.confirm !== false;
  }}, addEventListener(name, callback) { state.events[name] = callback; }};
  const document = {
    getElementById(name) { return name === 'contact-check' ? root : parts.get(name.replace('contact-check-', '')); },
    querySelector() { return {content: 'csrf-test'}; },
    createElement() { return element(); }, createDocumentFragment() { return element(); },
  };
  vm.runInNewContext(source, {window, document, URL, AbortController, TypeError, performance,
    setTimeout(callback, ms) { state.timers.push(ms); if (ms < 10000) queueMicrotask(callback); return 1; },
    clearTimeout() {}, setInterval() {},
    fetch: async (url, request = {}) => {
      state.requests.push({url, request, payload: request.body ? JSON.parse(request.body) : undefined});
      let reply = state.queue.shift();
      if (typeof reply === 'function') reply = await reply(url, request);
      assert.ok(reply, `发生意外请求：${url}`);
      if (reply instanceof Error) throw reply;
      return {ok: !reply.httpStatus || reply.httpStatus < 400, status: reply.httpStatus || 200, json: async () => structuredClone(reply)};
    },
  });
  const click = async (name) => { if (!parts.get(name).disabled) await parts.get(name).handlers.click({preventDefault() {}}); await settle(); };
  const switchChannel = async (id) => { parts.get('channel').value = String(id); parts.get('channel').handlers.change(); await settle(); };
  const text = (field, category) => selectors.get(`[data-${field}="${category}"]`).textContent;
  return {parts, state, root, selectors, click, switchChannel, text};
}

test('重新打开页面从服务端恢复结果、进度和间隔，不自动创建任务', async () => {
  const saved = snapshot({interval_seconds: 2.5});
  for (let opened = 0; opened < 2; opened += 1) {
    const page = openPage([saved]);
    await settle();
    assert.equal(page.state.requests.length, 1);
    assert.equal(page.state.requests[0].url, '/contacts/check/1/state');
    assert.match(page.parts.get('count').textContent, /已检测 2 \/ 3 位/);
    assert.equal(page.parts.get('interval').value, '2.5');
    assert.equal(page.text('result-count', 'friend'), '1');
    assert.equal(page.text('result-count', 'deleted'), '1');
    assert.equal(page.parts.get('start').textContent, '继续检测');
    assert.equal(page.parts.get('start').disabled, false);
  }
});

test('只有用户确认清空才发清空请求，清空后不自动重新开始', async () => {
  const cancelled = openPage([snapshot()], {confirm: false});
  await settle();
  await cancelled.click('restart');
  assert.equal(cancelled.state.requests.length, 1);
  assert.equal(cancelled.text('result-count', 'friend'), '1');

  const cleared = snapshot({generation: 'cleared-generation', total: 0, cursor: 0, elapsed_ms: 0, contacts: null, results: []});
  const page = openPage([snapshot(), cleared]);
  await settle();
  await page.click('restart');
  assert.equal(page.state.requests.length, 2);
  assert.equal(page.state.requests[1].url, '/contacts/check/1/clear');
  assert.deepEqual(page.state.requests[1].payload, {generation: 'saved-generation', confirmed: true});
  assert.equal(page.state.requests[1].request.headers['X-CSRF-Token'], 'csrf-test');
  assert.equal(page.text('result-count', 'friend'), '0');
  assert.equal(page.parts.get('start').textContent, '开始检测');
  assert.equal(page.parts.get('start').disabled, false);
});

test('恢复正在检测的好友只查询原任务，避免刷新后重复创建命令', async () => {
  const saved = snapshot({pending: {id: 901, action: 'scan', contact_id: 3}});
  const done = snapshot({cursor: 3, status: 'completed', results: [result(1), result(2, 'deleted'), result(3)]});
  const page = openPage([saved, saved, {state: 'completed', result: result(3), progress: progress()}, done]);
  await settle();
  await page.click('start');
  assert.deepEqual(page.state.requests.map((item) => item.url), [
    '/contacts/check/1/state', '/contacts/check/1/run', '/contacts/check/1/jobs/901/poll', '/contacts/check/1/pause',
  ]);
  assert.equal(page.parts.get('start').disabled, true);
  assert.match(page.parts.get('count').textContent, /已检测 3 \/ 3 位/);
});

test('切换账号分别读取保存记录，切回仍保留原账号结果', async () => {
  const second = snapshot({run_id: 20, generation: 'account-two', cursor: 1, total: 1,
    contacts: [contact(5)], results: [result(5, 'unknown')]});
  const page = openPage([snapshot(), second, snapshot()]);
  await settle();
  await page.switchChannel(2);
  assert.equal(page.text('result-count', 'unknown'), '1');
  assert.equal(page.text('result-count', 'friend'), '0');
  await page.switchChannel(1);
  assert.equal(page.text('result-count', 'friend'), '1');
  assert.equal(page.text('result-count', 'deleted'), '1');
  assert.ok(page.state.requests.every((item) => item.request.method === 'GET'));
});

test('读取失败不清掉已有展示，禁止开始和清空，重新读取成功后恢复', async () => {
  const page = openPage([snapshot(), new TypeError('offline'), snapshot()]);
  await settle();
  page.state.events.pageshow({persisted: true});
  await settle();
  assert.equal(page.text('result-count', 'friend'), '1');
  assert.equal(page.parts.get('start').disabled, true);
  assert.equal(page.parts.get('restart').disabled, true);
  assert.equal(page.parts.get('reload').hidden, false);
  assert.match(page.parts.get('status').textContent, /记录未被清空/);
  await page.click('reload');
  assert.equal(page.parts.get('start').disabled, false);
  assert.ok(page.state.requests.every((item) => item.request.method === 'GET'));
});

test('另一页面更新记录后重新读取，不自动重发过期清空请求', async () => {
  const newer = snapshot({generation: 'new-generation', cursor: 3,
    results: [result(1), result(2, 'deleted'), result(3)]});
  const page = openPage([snapshot(), {httpStatus: 409, detail: '检测记录已更新'}, newer]);
  await settle();
  await page.click('restart');
  assert.deepEqual(page.state.requests.map((item) => item.url), [
    '/contacts/check/1/state', '/contacts/check/1/clear', '/contacts/check/1/state',
  ]);
  assert.equal(page.text('result-count', 'friend'), '2');
  assert.match(page.parts.get('status').textContent, /已重新读取/);
});

test('恢复未确认的好友操作仅查询原任务，不重复提交备注或删除', async () => {
  const saved = snapshot({pending: {id: 902, action: 'remark', contact_id: 2}});
  const page = openPage([saved, {state: 'completed', result: {action: 'remark', ok: true,
    contact: {...contact(2), remark: '已保存备注'}, message: '备注成功'}, progress: progress({cursor: 2})}]);
  await settle();
  assert.equal(page.parts.get('action-resume').hidden, false);
  await page.click('action-resume');
  assert.deepEqual(page.state.requests.map((item) => item.url), ['/contacts/check/1/state', '/contacts/check/1/actions/902/poll']);
  assert.equal(page.parts.get('action-resume').hidden, true);
});

test('正常检测用250毫秒查询结果，完成后以服务端进度为准', async () => {
  const saved = snapshot({interval_seconds: 0});
  const done = snapshot({cursor: 3, status: 'completed', results: [result(1), result(2, 'deleted'), result(3)]});
  const page = openPage([saved, saved, {job_id: 903}, {state: 'running'},
    {state: 'completed', result: result(3), progress: progress()}, done]);
  await settle();
  await page.click('start');
  assert.ok(page.state.timers.includes(250));
  assert.deepEqual(page.state.requests[2].payload, {generation: 'saved-generation', scan: true});
  assert.match(page.parts.get('count').textContent, /已检测 3 \/ 3 位/);
});

test('关闭页面只尽力暂停，保留结果且不发送清空或重新创建任务', async () => {
  const page = openPage([snapshot(), snapshot()]);
  await settle();
  page.state.events.pagehide();
  await settle();
  assert.equal(page.state.requests.length, 2);
  const pause = page.state.requests[1];
  assert.equal(pause.url, '/contacts/check/1/pause');
  assert.equal(pause.request.keepalive, true);
  assert.deepEqual(pause.payload, {generation: 'saved-generation'});
  assert.equal(page.text('result-count', 'friend'), '1');
});
