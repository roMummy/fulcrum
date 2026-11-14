/* 邀请树只控制已渲染节点的展开，不改邀请证据、不请求外部关系，也不按显示名称合并成员。 */
(() => {
  const tree = document.querySelector('[data-invitation-tree]');
  if (!tree) return;
  const nodes = Array.from(tree.querySelectorAll('[data-invitation-node]'));
  const byKey = new Map(nodes.map((node) => [node.dataset.invitationNode, node]));
  const expanded = new Set();

  function render() {
    // 服务端按父节点在前的深度顺序输出；一遍即可计算可见性，深层链也不用递归。
    nodes.forEach((node) => {
      const parent = byKey.get(node.dataset.parent);
      node.hidden = Boolean(parent && (parent.hidden || !expanded.has(parent.dataset.invitationNode)));
      const toggle = node.querySelector('[data-invitation-toggle]');
      if (!toggle) return;
      const open = expanded.has(node.dataset.invitationNode);
      toggle.setAttribute('aria-expanded', String(open));
      const name = node.querySelector('.identity-name')?.textContent || '该成员';
      toggle.setAttribute('aria-label', `${open ? '收起' : '展开'} ${name} 的邀请成员`);
    });
  }

  tree.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-invitation-toggle]');
    if (toggle) {
      const key = toggle.closest('[data-invitation-node]').dataset.invitationNode;
      if (expanded.has(key)) expanded.delete(key);
      else expanded.add(key);
    } else if (event.target.closest('[data-invitation-expand]')) {
      nodes.forEach((node) => expanded.add(node.dataset.invitationNode));
    } else if (event.target.closest('[data-invitation-collapse]')) {
      expanded.clear();
    } else return;
    render();
  });
  render();
})();
