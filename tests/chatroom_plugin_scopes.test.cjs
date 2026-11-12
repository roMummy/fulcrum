// 使用 Node 自带断言执行页面脚本，验证跨页选择和提交，不依赖浏览器测试框架。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/js/chatroom-plugin-scopes.js'), 'utf8');

function element() {
  return {checked: false, disabled: false, children: [], handlers: {}, dataset: {}, textContent: '',
    addEventListener(name, callback) { this.handlers[name] = callback; },
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); }};
}

function openPage(storage, ids, options = {}) {
  const key = options.key || 'fulcrum:group-plugin:7:11';
  const rows = ids.map((id) => Object.assign(element(), {value: String(id), disabled: id < 0}));
  const selectPage = element();
  const count = element();
  const notice = element();
  const inputs = element();
  const clear = element();
  const selectMatching = Object.assign(element(), {dataset: {roomIds: JSON.stringify(options.matching || [])}});
  const buttons = [Object.assign(element(), {value: 'enabled'}), Object.assign(element(), {value: 'disabled'})];
  const parts = {'[data-selection-count]': count, '[data-selection-notice]': notice, '[data-cross-page-inputs]': inputs, '[data-clear-selection]': clear, '[data-select-matching]': selectMatching};
  const form = Object.assign(element(), {
    action: '/chatrooms/plugins/batch', dataset: {selectionKey: key, clearSelection: options.clear ? 'true' : 'false'},
    querySelector(selector) { return parts[selector]; }, querySelectorAll() { return buttons; },
  });
  const document = {
    getElementById() { return form; }, querySelectorAll() { return rows; },
    querySelector() { return selectPage; }, createElement() { return element(); },
  };
  const result = {};
  class FormData {
    constructor() {
      this.fields = [['plugin_id', '11'], ['csrf_token', 'test-token']];
      rows.filter((row) => row.checked && !row.disabled).forEach((row) => this.fields.push(['chatroom_ids', row.value]));
      inputs.children.forEach((input) => this.fields.push([input.name, input.value]));
    }
    set(key, value) { this.fields = this.fields.filter(([name]) => name !== key); this.fields.push([key, value]); }
    [Symbol.iterator]() { return this.fields[Symbol.iterator](); }
  }
  const sessionStorage = {
    getItem(name) { return storage.get(name) || null; }, setItem(name, value) { storage.set(name, value); }, removeItem(name) { storage.delete(name); },
  };
  if (options.storageFails) sessionStorage.getItem = () => { throw new Error('storage blocked'); };
  const window = {fetch: true, history: {replaceState(_a, _b, url) { result.replaced = url; }}, location: {href: 'http://localhost/chatrooms/plugin-scopes?plugin_id=11&selection_cleared=1', assign(url) { result.redirect = url; }}};
  vm.runInNewContext(source, {document, sessionStorage, FormData, URL, URLSearchParams, window,
    fetch: async (_url, request) => {
      result.payload = request.body;
      return {ok: true, json: async () => ({ok: true, redirect_url: '/chatrooms/plugin-scopes?plugin_id=11'})};
    },
  });
  return {key, rows, selectPage, selectMatching, count, notice, inputs, clear, buttons, form, result};
}

async function main() {
  const storage = new Map();
  const first = openPage(storage, [1, 2, -3]);
  first.rows[0].checked = true;
  first.rows[0].handlers.change();
  const second = openPage(storage, [4, 5]);
  assert.equal(second.count.textContent, '已选 1 个群（本页 0 个）');
  second.selectPage.checked = true;
  second.selectPage.handlers.change();
  assert.deepEqual(JSON.parse(storage.get(second.key)), ['1', '4', '5']);
  assert.equal(second.inputs.children[0].value, '1');
  assert.equal(openPage(storage, [1], {key: 'fulcrum:group-plugin:7:12'}).rows[0].checked, false);
  assert.equal(openPage(storage, [1], {key: 'fulcrum:group-plugin:8:11'}).rows[0].checked, false);
  second.rows[0].checked = false;
  second.rows[0].handlers.change();
  assert.equal(second.selectPage.indeterminate, true);
  await second.form.handlers.submit({preventDefault() {}, submitter: second.buttons[1]});
  assert.deepEqual(second.result.payload.getAll('chatroom_ids').sort(), ['1', '5']);
  assert.equal(second.result.payload.get('mode'), 'disabled');
  assert.deepEqual(JSON.parse(storage.get(second.key)), []);
  assert.equal(second.result.redirect, '/chatrooms/plugin-scopes?plugin_id=11');

  storage.set(first.key, '["1","2"]');
  const cleared = openPage(storage, [1, 2], {clear: true});
  assert.deepEqual(JSON.parse(storage.get(first.key)), []);
  assert.equal(cleared.rows[0].checked, false);
  assert.equal(cleared.result.replaced.includes('selection_cleared'), false);

  const fallback = openPage(storage, [9], {storageFails: true});
  assert.match(fallback.notice.textContent, /离开本页前提交/);
  fallback.rows[0].checked = true;
  fallback.rows[0].handlers.change();
  assert.equal(fallback.buttons[0].disabled, false);
  fallback.clear.handlers.click();
  assert.equal(fallback.buttons[0].disabled, true);

  storage.set(first.key, JSON.stringify(Array.from({length: 1000}, (_, index) => String(index + 1))));
  const limit = openPage(storage, [1001]);
  limit.rows[0].checked = true;
  limit.rows[0].handlers.change();
  assert.equal(limit.rows[0].checked, false);
  assert.match(limit.notice.textContent, /最多选择 1000/);

  const matching = Array.from({length: 353}, (_, index) => index + 1);
  storage.set(first.key, '["999"]');
  const all = openPage(storage, [1, 2], {matching});
  all.selectMatching.handlers.click();
  assert.equal(all.count.textContent, '已选 353 个群（本页 2 个）');
  assert.equal(JSON.parse(storage.get(first.key)).includes('999'), false);
  all.rows[0].checked = false;
  all.rows[0].handlers.change();
  const next = openPage(storage, [31, 32], {matching});
  assert.equal(next.count.textContent, '已选 352 个群（本页 2 个）');
  assert.equal(next.rows.every((row) => row.checked), true);
  await next.form.handlers.submit({preventDefault() {}, submitter: next.buttons[1]});
  const submitted = next.result.payload.getAll('chatroom_ids');
  assert.equal(submitted.length, 352);
  assert.equal(new Set(submitted).size, 352);
  assert.equal(submitted.includes('1'), false);
  assert.equal(submitted.includes('999'), false);
  const tooMany = openPage(new Map(), [1], {matching: Array.from({length: 1001}, (_, index) => index + 1)});
  assert.equal(tooMany.selectMatching.disabled, true);
  tooMany.selectMatching.handlers.click();
  assert.equal(tooMany.buttons[0].disabled, true);
  assert.equal(openPage(new Map(), [1]).selectMatching.disabled, true);
  const withoutStorage = openPage(new Map(), [1], {matching, storageFails: true});
  withoutStorage.selectMatching.handlers.click();
  await withoutStorage.form.handlers.submit({preventDefault() {}, submitter: withoutStorage.buttons[0]});
  assert.equal(withoutStorage.result.payload.getAll('chatroom_ids').length, 353);
  process.stdout.write('chatroom plugin cross-page selection: passed\n');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
