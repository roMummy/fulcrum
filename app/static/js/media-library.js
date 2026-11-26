/* 媒体库只预览已落盘原件；选择与删除仍由共享确认弹窗和服务端权限控制。 */
(() => {
  'use strict';
  const root = document.querySelector('[data-media-library]');
  if (!root) return;
  // 以实际卡片高度占用网格行，后续项目进入较短列；图片加载、封面完成和屏宽变化均重算。
  const grid = root.querySelector('.media-grid');
  const tiles = Array.from(root.querySelectorAll('[data-media-item]'));
  if (tiles.length) {
    grid.classList.add('is-masonry');
    function sizeTile(tile) {
      tile.style.gridRowEnd = `span ${Math.ceil(tile.getBoundingClientRect().height + 18)}`;
    }
    const sizes = new ResizeObserver(entries => entries.forEach(entry => sizeTile(entry.target)));
    tiles.forEach(tile => { sizeTile(tile); sizes.observe(tile); });
    // 图片加载完成（或失败回退）后再显示左上勾选与右上信息图标，避免未撑开前两枚浮层图标堆叠在一起。
    tiles.forEach(tile => {
      if (!tile.classList.contains('media-tile-image')) return;
      const picture = tile.querySelector('.media-open img');
      if (!picture) { tile.classList.remove('is-loading'); return; }
      const reveal = () => { tile.classList.remove('is-loading'); sizeTile(tile); };
      // 必须确认图片已有实际像素宽高（naturalWidth > 0）才视为加载完成，避免懒加载图片尚未拉取时提前显现浮层图标。
      if (picture.complete && picture.naturalWidth > 0) {
        reveal();
      } else {
        picture.addEventListener('load', reveal, { once: true });
        picture.addEventListener('error', reveal, { once: true });
      }
    });
  }
  const operations = Array.from(root.querySelectorAll('[data-media-operation]'));
  let busy = false;
  operations.forEach(form => form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    if (busy) { event.preventDefault(); return; }
    busy = true;
    const status = root.querySelector('[data-media-operation-status]');
    if (status) { status.hidden = false; status.textContent = form.dataset.mediaOperation; }
    operations.forEach(operation => {
      operation.setAttribute('aria-busy', 'true');
      Array.from(operation.elements).filter(control => control.type === 'submit').forEach(button => { button.disabled = true; });
    });
  }));
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    busy = false;
    operations.forEach(form => {
      form.removeAttribute('aria-busy');
      Array.from(form.elements).filter(control => control.type === 'submit').forEach(button => { button.disabled = false; });
    });
    const status = root.querySelector('[data-media-operation-status]');
    if (status) status.hidden = true;
  });
  // 只为进入视口的视频请求小封面；最多串行处理一张，避免一页并发启动解码。
  const coverQueue = [];
  let coverBusy = false;
  function nextCover() {
    if (coverBusy || !coverQueue.length) return;
    coverBusy = true;
    const picture = coverQueue.shift();
    function done() { coverBusy = false; nextCover(); }
    picture.addEventListener('load', () => { picture.hidden = false; done(); }, {once: true});
    picture.addEventListener('error', () => { picture.remove(); done(); }, {once: true});
    picture.src = picture.dataset.videoCover;
  }
  const coverObserver = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      coverObserver.unobserve(entry.target);
      coverQueue.push(entry.target.querySelector('[data-video-cover]'));
    });
    nextCover();
  }, {rootMargin: '100px'});
  root.querySelectorAll('[data-video-cover]').forEach(picture => coverObserver.observe(picture.parentElement));
  const infoPanels = Array.from(root.querySelectorAll('.media-info'));
  infoPanels.forEach(panel => panel.addEventListener('toggle', () => {
    if (panel.open) infoPanels.forEach(other => { if (other !== panel) other.open = false; });
  }));
  document.addEventListener('click', event => {
    infoPanels.forEach(panel => { if (panel.open && !panel.contains(event.target)) panel.open = false; });
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || document.documentElement.classList.contains('has-lightbox')) return;
    infoPanels.forEach(panel => {
      if (!panel.open) return;
      panel.open = false;
      panel.querySelector('summary').focus();
      event.preventDefault();
    });
  });
  const selections = Array.from(root.querySelectorAll('[data-media-select]'));
  const all = root.querySelector('[data-media-select-all]');
  const remove = root.querySelector('[data-media-delete]');
  function refresh() {
    const count = selections.filter(input => input.checked).length;
    if (remove) {
      remove.disabled = count === 0;
      remove.textContent = count ? `删除所选（${count}）` : '删除所选';
    }
    if (all) {
      all.checked = count > 0 && count === selections.length;
      all.indeterminate = count > 0 && count < selections.length;
    }
  }
  all?.addEventListener('change', () => {
    selections.forEach(input => { input.checked = all.checked; });
    refresh();
  });
  selections.forEach(input => input.addEventListener('change', refresh));
  root.querySelectorAll('audio').forEach(player => {
    player.addEventListener('play', () => {
      root.querySelectorAll('audio').forEach(other => { if (other !== player) other.pause(); });
    });
    player.addEventListener('error', () => { player.nextElementSibling.hidden = false; });
  });
  root.addEventListener('click', event => {
    const trigger = event.target.closest('[data-media-open]');
    if (trigger && window.appLightbox) {
      window.appLightbox.open([{ type: trigger.dataset.mediaOpen, url: trigger.dataset.url }], 0);
    }
  });
  refresh();
})();
