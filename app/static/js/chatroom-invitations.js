/* 邀请图谱只展示服务端的明确关系。布局、搜索与放大共用成员 ID 和一份 DOM，不写回邀请证据。 */
(() => {
  const panel = document.querySelector('[data-invitation-graph]');
  if (!panel) return;
  const find = (selector) => panel.querySelector(selector);
  const viewport = find('[data-invitation-viewport]');
  const scene = find('[data-invitation-scene]');
  const svg = find('[data-invitation-svg]');
  const edges = find('[data-invitation-edges]');
  const inspector = find('[data-invitation-inspector]');
  const search = find('[data-invitation-search]');
  const results = find('[data-invitation-results]');
  const searchStatus = find('[data-invitation-search-status]');
  const dialog = find('[data-invitation-dialog]');
  const workspace = find('[data-invitation-workspace]');
  const fullscreen = find('[data-invitation-fullscreen]');
  const nodes = Array.from(panel.querySelectorAll('[data-invitation-node]'), (element) => ({
    key: element.dataset.invitationNode, parentKey: element.dataset.parent,
    connected: element.dataset.connected === 'true', element, children: [],
    select: element.querySelector('[data-invitation-select]'),
    toggle: element.querySelector('[data-invitation-toggle]'),
    data: element.dataset,
  }));
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const graph = nodes.filter((node) => node.connected);
  graph.forEach((node) => {
    node.parent = byKey.get(node.parentKey);
    if (node.parent) node.parent.children.push(node);
  });
  const roots = graph.filter((node) => !node.parent);
  // 初始展示根和第一层被邀请人，让进入页面时即可看到真实连线，同时控制大群首屏密度。
  const expanded = new Set(roots.map((node) => node.key));
  const view = {x: 0, y: 0, scale: 1};
  const cardWidth = 248;
  const gap = 32;
  const padding = 24;
  let bounds = {width: 1, height: 1};
  let selectedKey = '';
  let matches = [];
  let autoFit = true;
  let previousSize = {width: viewport.clientWidth, height: viewport.clientHeight};
  let drag = null;

  function applyView() {
    if (!scene) return;
    scene.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    const percentage = view.scale * 100;
    find('[data-invitation-scale]').textContent = `${percentage < 1 ? percentage.toFixed(1) : Math.round(percentage)}%`;
  }

  function fitScale() {
    return Math.max(0.00001, Math.min(1, (viewport.clientWidth - 32) / bounds.width, (viewport.clientHeight - 32) / bounds.height));
  }

  function fit() {
    if (!scene || !viewport.clientWidth) return;
    autoFit = true;
    view.scale = fitScale();
    view.x = (viewport.clientWidth - bounds.width * view.scale) / 2;
    view.y = (viewport.clientHeight - bounds.height * view.scale) / 2;
    applyView();
  }

  function zoom(factor, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) {
    if (!scene) return;
    // 最小缩放随图谱范围确定，展开大群后仍能完整归位；主动定位时会恢复可阅读比例。
    const next = Math.max(Math.min(0.2, fitScale()), Math.min(2, view.scale * factor));
    const ratio = next / view.scale;
    view.x = x - (x - view.x) * ratio;
    view.y = y - (y - view.y) * ratio;
    view.scale = next;
    autoFit = false;
    applyView();
  }

  function highlight() {
    const related = new Set();
    let cursor = byKey.get(selectedKey);
    while (cursor && !related.has(cursor.key)) {
      related.add(cursor.key);
      cursor = cursor.parent;
    }
    const selected = byKey.get(selectedKey);
    selected?.children.forEach((child) => related.add(child.key));
    nodes.forEach((node) => {
      node.element.classList.toggle('is-selected', node.key === selectedKey);
      node.element.classList.toggle('is-related', related.has(node.key));
      node.select.setAttribute('aria-pressed', String(node.key === selectedKey));
    });
    edges?.querySelectorAll('path').forEach((edge) => {
      edge.classList.toggle('is-highlighted', related.has(edge.dataset.from) && related.has(edge.dataset.to));
    });
  }

  function layout() {
    if (!scene) return;
    const levelHeights = [];
    // 服务端已输出父节点在前的森林，正序确定可见性，逆序计算子树宽度，全程无递归。
    graph.forEach((node) => {
      node.visible = !node.parent || node.parent.visible && expanded.has(node.parent.key);
      node.element.hidden = !node.visible;
      node.level = node.parent ? node.parent.level + 1 : 0;
      if (node.toggle) {
        const open = expanded.has(node.key);
        node.toggle.setAttribute('aria-expanded', String(open));
        node.toggle.setAttribute('aria-label', `${open ? '收起' : '展开'} ${node.data.name} 的邀请成员`);
        node.toggle.querySelector('[data-invitation-branch-label]').textContent = open ? '收起' : '展开';
      }
    });
    const visible = graph.filter((node) => node.visible);
    // 一起读取真实卡片高度后再写坐标，长中文昵称、ID 和来源正常换行，不裁切资料。
    visible.forEach((node) => {
      node.height = node.element.offsetHeight;
      levelHeights[node.level] = Math.max(levelHeights[node.level] || 0, node.height);
      node.visibleChildren = node.children.filter((child) => child.visible);
    });
    for (let index = visible.length - 1; index >= 0; index -= 1) {
      const node = visible[index];
      node.span = Math.max(cardWidth, node.visibleChildren.reduce((total, child) => total + child.span + gap, -gap));
    }
    const levelY = [];
    let bottom = padding;
    levelHeights.forEach((height, index) => {
      levelY[index] = bottom;
      bottom += height + 64;
    });
    let left = padding;
    roots.forEach((node) => {
      node.start = left;
      left += node.span + gap * 2;
    });
    visible.forEach((node) => {
      node.x = node.start + (node.span - cardWidth) / 2;
      node.y = levelY[node.level];
      node.element.style.left = `${node.x}px`;
      node.element.style.top = `${node.y}px`;
      let childStart = node.start + (node.span - node.visibleChildren.reduce((total, child) => total + child.span + gap, -gap)) / 2;
      node.visibleChildren.forEach((child) => {
        child.start = childStart;
        childStart += child.span + gap;
      });
    });
    bounds = {width: left - gap * 2 + padding, height: bottom - 64 + padding};
    scene.style.width = `${bounds.width}px`;
    scene.style.height = `${bounds.height}px`;
    svg.setAttribute('width', String(bounds.width));
    svg.setAttribute('height', String(bounds.height));
    const fragment = document.createDocumentFragment();
    visible.forEach((node) => {
      if (!node.parent) return;
      const fromX = node.parent.x + cardWidth / 2;
      const fromY = node.parent.y + node.parent.height;
      const toX = node.x + cardWidth / 2;
      const toY = node.y - 5;
      const middleY = (fromY + toY) / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${fromX} ${fromY} C ${fromX} ${middleY}, ${toX} ${middleY}, ${toX} ${toY}`);
      path.setAttribute('marker-end', 'url(#invitation-graph-arrow)');
      path.dataset.from = node.parent.key;
      path.dataset.to = node.key;
      fragment.append(path);
    });
    edges.replaceChildren(fragment);
    highlight();
  }

  function select(node) {
    selectedKey = node.key;
    const fields = {name: node.data.name, id: node.data.externalId,
      status: `${node.data.role} · ${node.data.status}`, inviter: node.data.inviter,
      source: node.data.source || '未知', time: node.data.recordedAt, count: `${node.data.directInvites} 人`};
    Object.entries(fields).forEach(([key, value]) => {
      find(`[data-invitation-detail-${key}]`).textContent = value;
    });
    inspector.hidden = false;
    highlight();
  }

  function center(node) {
    autoFit = false;
    // 定位恢复可阅读的卡片尺寸，不让“适应整图”产生的小比例妨碍查看目标成员。
    view.scale = Math.max(view.scale, Math.min(1, (viewport.clientWidth - 40) / cardWidth));
    view.x = viewport.clientWidth / 2 - (node.x + cardWidth / 2) * view.scale;
    view.y = viewport.clientHeight / 2 - (node.y + node.height / 2) * view.scale;
    applyView();
  }

  function locate(key) {
    const node = byKey.get(key);
    if (!node) return;
    select(node);
    if (node.connected) {
      let parent = node.parent;
      while (parent) {
        expanded.add(parent.key);
        parent = parent.parent;
      }
      layout();
      center(node);
      viewport.scrollIntoView({block: 'nearest'});
    } else {
      find('[data-invitation-unlinked]').open = true;
      node.element.scrollIntoView({block: 'nearest'});
    }
    results.hidden = true;
    searchStatus.textContent = `已定位：${node.data.name}（${node.data.externalId}）`;
    node.select.focus({preventScroll: true});
  }

  function searchMembers() {
    const query = search.value.trim().toLowerCase();
    results.replaceChildren();
    matches = query ? nodes.filter((node) => `${node.data.name}\n${node.data.externalId}`.toLowerCase().includes(query)) : [];
    matches.slice(0, 20).forEach((node) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.invitationResult = node.key;
      const name = document.createElement('strong');
      name.textContent = node.data.name;
      const id = document.createElement('small');
      id.textContent = node.data.externalId;
      button.append(name, id);
      item.append(button);
      results.append(item);
    });
    results.hidden = !matches.length;
    searchStatus.textContent = query ? (matches.length ? `找到 ${matches.length} 位成员${matches.length > 20 ? '，显示前 20 位，请缩小搜索范围' : '，选择后定位'}` : '没有匹配的成员') : '';
  }

  panel.addEventListener('click', (event) => {
    const target = event.target.closest('button');
    if (!target) return;
    if (target.hasAttribute('data-invitation-result')) return locate(target.dataset.invitationResult);
    if (target.hasAttribute('data-invitation-select')) return select(byKey.get(target.closest('[data-invitation-node]').dataset.invitationNode));
    if (target.hasAttribute('data-invitation-toggle')) {
      const node = byKey.get(target.closest('[data-invitation-node]').dataset.invitationNode);
      const anchor = {x: view.x + (node.x + cardWidth / 2) * view.scale, y: view.y + node.y * view.scale};
      if (expanded.has(node.key)) expanded.delete(node.key);
      else expanded.add(node.key);
      layout();
      // 在原位置展开分支，避免父卡片突然跳离正在操作的指针或键盘焦点。
      view.x = anchor.x - (node.x + cardWidth / 2) * view.scale;
      view.y = anchor.y - node.y * view.scale;
      autoFit = false;
      return applyView();
    }
    if (target.hasAttribute('data-invitation-zoom-in')) return zoom(1.25);
    if (target.hasAttribute('data-invitation-zoom-out')) return zoom(0.8);
    if (target.hasAttribute('data-invitation-fit')) return fit();
    if (target.hasAttribute('data-invitation-expand') || target.hasAttribute('data-invitation-collapse')) {
      expanded.clear();
      if (target.hasAttribute('data-invitation-expand')) graph.forEach((node) => expanded.add(node.key));
      layout();
      return fit();
    }
    if (target.hasAttribute('data-invitation-clear-selection')) {
      const selected = byKey.get(selectedKey);
      selectedKey = '';
      inspector.hidden = true;
      highlight();
      if (selected?.select.getClientRects().length) selected.select.focus({preventScroll: true});
      else viewport.focus({preventScroll: true});
    }
    if (target.hasAttribute('data-invitation-fullscreen')) {
      // 原生 dialog 继续使用共享关闭/遮罩处理；工作区仅移动，始终保留唯一事件与选择状态。
      results.hidden = true;
      find('[data-invitation-dialog-slot]').append(workspace);
      fullscreen.hidden = true;
      dialog.showModal();
      viewport.focus({preventScroll: true});
    }
  });

  search.addEventListener('input', searchMembers);
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && matches.length) {
      event.preventDefault();
      locate(matches[0].key);
    } else if (event.key === 'ArrowDown' && !results.hidden) {
      event.preventDefault();
      results.querySelector('button').focus();
    } else if (event.key === 'Escape' && !results.hidden) {
      event.preventDefault();
      results.hidden = true;
    }
  });
  results.addEventListener('keydown', (event) => {
    const buttons = Array.from(results.querySelectorAll('button'));
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      buttons[(index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length]?.focus();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      results.hidden = true;
      search.focus();
    }
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.invitation-search')) results.hidden = true;
  });

  viewport.addEventListener('pointerdown', (event) => {
    if (!scene || event.button !== 0 || event.target.closest('button')) return;
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY, originX: view.x, originY: view.y};
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('is-dragging');
    viewport.focus({preventScroll: true});
  });
  viewport.addEventListener('pointermove', (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    autoFit = false;
    view.x = drag.originX + event.clientX - drag.x;
    view.y = drag.originY + event.clientY - drag.y;
    applyView();
  });
  const endDrag = () => {
    drag = null;
    viewport.classList.remove('is-dragging');
  };
  viewport.addEventListener('pointerup', endDrag);
  viewport.addEventListener('pointercancel', endDrag);
  viewport.addEventListener('lostpointercapture', endDrag);
  viewport.addEventListener('wheel', (event) => {
    // 普通滚轮仍滚动页面，仅触控板捏合或 Ctrl+滚轮缩放，避免画布截走页面浏览操作。
    if (!scene || !event.ctrlKey) return;
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    zoom(Math.exp(-event.deltaY * 0.01), event.clientX - rect.left, event.clientY - rect.top);
  }, {passive: false});
  viewport.addEventListener('keydown', (event) => {
    if (event.target !== viewport || !scene) return;
    const directions = {ArrowLeft: [48, 0], ArrowRight: [-48, 0], ArrowUp: [0, 48], ArrowDown: [0, -48]};
    if (directions[event.key]) {
      event.preventDefault();
      autoFit = false;
      view.x += directions[event.key][0];
      view.y += directions[event.key][1];
      applyView();
    } else if (['+', '=', '-', '0', 'Home'].includes(event.key)) {
      event.preventDefault();
      if (event.key === '0' || event.key === 'Home') fit();
      else zoom(event.key === '-' ? 0.8 : 1.25);
    }
  });
  viewport.addEventListener('focusin', (event) => {
    const node = byKey.get(event.target.closest('[data-invitation-node]')?.dataset.invitationNode);
    if (!node || !node.visible) return;
    const x = view.x + node.x * view.scale;
    const y = view.y + node.y * view.scale;
    if (x < 0 || y < 0 || x + cardWidth * view.scale > viewport.clientWidth || y + node.height * view.scale > viewport.clientHeight) center(node);
    // 聚焦画布外的卡片时由视图平移接管，不允许浏览器再累积一个隐藏的滚动偏移。
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
  });

  function resizeView() {
    const size = {width: viewport.clientWidth, height: viewport.clientHeight};
    if (!size.width || !size.height) return;
    if (autoFit) fit();
    else {
      view.x += (size.width - previousSize.width) / 2;
      view.y += (size.height - previousSize.height) / 2;
      applyView();
    }
    previousSize = size;
  }
  dialog.addEventListener('close', () => {
    find('[data-invitation-inline-slot]').append(workspace);
    fullscreen.hidden = false;
    // 共享关闭逻辑执行后再返回可见入口，不能把焦点留在已关闭 dialog 的内部。
    requestAnimationFrame(() => {
      resizeView();
      fullscreen.focus({preventScroll: true});
    });
  });

  panel.classList.add('is-ready');
  search.disabled = !nodes.length;
  panel.querySelectorAll('.invitation-controls button').forEach((button) => { button.disabled = !graph.length; });
  layout();
  fit();
  new ResizeObserver(resizeView).observe(viewport);
  document.fonts.ready.then(() => {
    layout();
    if (autoFit) fit();
    else applyView();
  });
})();
