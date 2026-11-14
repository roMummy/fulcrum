// 用 Node 标准库验证异步反馈、恢复任务和刷新边界，不引入测试依赖。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/js/action-feedback.js'), 'utf8');

function element() {
  return {hidden: false, disabled: false, textContent: '', dataset: {}, handlers: {}, children: [],
    // 这些模拟元素默认没有 CSS 类；图标按钮判断同样需要浏览器的 contains 接口。
    classList: {toggle() {}, contains() { return false; }}, addEventListener(name, callback) { this.handlers[name] = callback; },
    setAttribute(name, value) { this[name] = value; }, replaceChildren() { this.children = []; },
    append(...children) { this.children.push(...children); }, appendChild(child) { this.children.push(child); }};
}
const statusUrl = '/channels/3/operations/status?job_ids=5';
const pending = {ok: true, done: false, jobs: [{id: 5, channel_name: '账号', state: 'pending', message: '等待手机领取'}], summary: '正在同步', status_url: statusUrl};
const completed = {ok: true, done: true, successful: 1, failed: 0, jobs: [{id: 5, channel_name: '账号', state: 'completed', message: '同步完成', elapsed_seconds: 2}], summary: '群成员同步完成'};

function openPage(replies, options = {}) {
  const parts = Object.fromEntries(['summary', 'counter', 'jobs', 'progress', 'track', 'dismiss'].map((name) => [`[data-action-${name}]`, element()]));
  const monitor = Object.assign(element(), {querySelector(name) { return parts[name]; }});
  const forms = [1, 2].map((id) => {
    const button = Object.assign(element(), {tagName: 'BUTTON', type: 'submit', textContent: `同步 ${id}`});
    return Object.assign(element(), {action: `http://localhost/channels/3/sync-${id}`, dataset: {actionLabel: `同步 ${id}`}, fields: [['csrf_token', 'test-token']], button, elements: [button],
      querySelectorAll() { return [button]; }, contains(node) { return node === button; }});
  });
  const result = {requests: [], redirects: [], events: {}, storage: options.storage || new Map()};
  const window = {location: {href: options.href || 'http://localhost/channels/3/chatrooms/7?tab=members', origin: 'http://localhost', pathname: '/channels/3/chatrooms/7', replace(url) { result.redirects.push(url); }},
    history: {replaceState(_a, _b, url) { result.cleaned = String(url); }}, addEventListener(name, fn) { result.events[name] = fn; }};
  const document = {querySelector(name) { return name === '[data-action-monitor]' ? monitor : {content: 'meta-token'}; }, querySelectorAll() { return forms; }, createElement() { return element(); }};
  const sessionStorage = {
    getItem(name) { if (options.storageFails) throw new Error('blocked'); return result.storage.get(name) || null; },
    setItem(name, value) { if (options.storageFails) throw new Error('blocked'); result.storage.set(name, value); },
    removeItem(name) { if (options.storageFails) throw new Error('blocked'); result.storage.delete(name); },
  };
  class FormData {
    constructor(form) { this.fields = [...form.fields]; }
    get(name) { return this.fields.find(([key]) => key === name)?.[1]; }
    append(name, value) { this.fields.push([name, value]); }
  }
  let index = 0;
  vm.runInNewContext(source, {window, document, sessionStorage, FormData, URL, AbortController, Date,
    setTimeout(fn, ms) { if (ms < 10000) queueMicrotask(fn); return 1; }, clearTimeout() {},
    fetch: async (url, request) => {
      result.requests.push({url, request});
      let reply = replies[index++];
      if (typeof reply === 'function') reply = await reply();
      if (reply instanceof Error) throw reply;
      assert.ok(reply, '发生了意外的额外请求');
      return {ok: reply.httpStatus ? reply.httpStatus < 400 : true, status: reply.httpStatus || 200, json: async () => reply};
    },
  });
  const submit = (index = 0) => forms[index].handlers.submit({preventDefault() {}, submitter: forms[index].button});
  return {forms, monitor, parts, result, submit};
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

async function main() {
  const immediate = openPage([{ok: true, done: true, successful: 1, failed: 0, jobs: [], refresh: false, summary: '检查正常'}]);
  await immediate.submit();
  assert.equal(immediate.parts['[data-action-summary]'].textContent, '检查正常');
  assert.equal(immediate.monitor.dataset.state, 'success');
  assert.equal(immediate.result.requests[0].request.headers['X-CSRF-Token'], 'test-token');
  assert.equal(immediate.result.redirects.length, 0);

  const page = openPage([pending, {...pending, jobs: [{...pending.jobs[0], state: 'running'}]}, completed]);
  await page.submit();
  assert.equal(page.result.requests.length, 3);
  assert.equal(page.result.redirects.length, 1);
  assert.equal(page.monitor.dataset.state, 'success');
  assert.equal(page.forms[0].button.disabled, false);
  const saved = openPage([], {storage: page.result.storage});
  assert.equal(saved.parts['[data-action-summary]'].textContent, '群成员同步完成');
  assert.equal(saved.result.requests.length, 0);
  assert.equal(saved.result.redirects.length, 0);

  const noStorage = openPage([pending, completed], {storageFails: true});
  await noStorage.submit();
  assert.equal(noStorage.result.redirects.length, 1);
  const resumed = openPage([completed], {storageFails: true, href: noStorage.result.redirects[0]});
  await settle();
  assert.equal(resumed.result.requests.length, 1);
  assert.equal(resumed.result.redirects.length, 0);
  assert.equal(resumed.monitor.dataset.state, 'success');

  const redirected = openPage([completed], {href: 'http://localhost/channels/3/chatrooms/7?operation_jobs=5&operation_channel=3&notice=已提交'});
  await settle();
  assert.equal(redirected.result.requests[0].url, statusUrl);
  assert.equal(redirected.result.redirects.length, 1);
  assert.equal(new URL(redirected.result.redirects[0]).searchParams.has('operation_jobs'), false);

  const unsafe = openPage([{...pending, status_url: 'https://other.example/channels/3/operations/status?job_ids=5'}]);
  await unsafe.submit();
  assert.equal(unsafe.result.requests.length, 1);
  assert.equal(unsafe.monitor.dataset.state, 'error');
  assert.match(unsafe.parts['[data-action-summary]'].textContent, /地址无效/);

  const retry = openPage([pending, new Error('offline'), completed]);
  await retry.submit();
  assert.equal(retry.result.requests.length, 3);
  assert.equal(retry.monitor.dataset.state, 'success');
  const failed = openPage([{ok: false, done: true, failed: 1, summary: '手机暂时离线', httpStatus: 400}]);
  await failed.submit();
  assert.equal(failed.monitor.dataset.state, 'error');
  assert.equal(failed.result.redirects.length, 0);

  let release;
  const busy = openPage([() => new Promise((resolve) => { release = resolve; })]);
  const first = busy.submit();
  await busy.submit(1);
  assert.equal(busy.result.requests.length, 1);
  release({ok: true, done: true, successful: 1, failed: 0, jobs: [], refresh: false, summary: '完成'});
  await first;

  let finishPoll;
  const leaving = openPage([pending, () => new Promise((resolve) => { finishPoll = resolve; })]);
  const submission = leaving.submit();
  await settle();
  leaving.result.events.pagehide();
  finishPoll(completed);
  await submission;
  assert.equal(leaving.result.requests.length, 2);
  assert.equal(leaving.result.redirects.length, 0);
  process.stdout.write('action feedback: passed\n');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
