(() => {
  "use strict";

  const scene = document.querySelector("[data-hero-scene]");
  const hero = scene?.closest(".marketing-hero");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // 价格页、登录页等公开页面没有首屏场景；减少动态效果时也不注册指针监听。
  if (!scene || !hero || reducedMotion.matches) {
    return;
  }

  let frameId = 0;
  let pendingX = 0;
  let pendingY = 0;

  const clamp = (value) => Math.max(-1, Math.min(1, value));

  // 统一写入归一化坐标和带单位位移，让 CSS 不依赖尚未普及的乘法表达式。
  const renderPosition = () => {
    const x = clamp(pendingX);
    const y = clamp(pendingY);
    scene.style.setProperty("--hero-pointer-x", x.toFixed(3));
    scene.style.setProperty("--hero-pointer-y", y.toFixed(3));
    scene.style.setProperty("--hero-signal-x", `${(x * 12).toFixed(2)}px`);
    scene.style.setProperty("--hero-signal-y", `${(y * 12).toFixed(2)}px`);
    scene.style.setProperty("--hero-marker-x", `${(x * -6).toFixed(2)}px`);
    scene.style.setProperty("--hero-marker-y", `${(y * -6).toFixed(2)}px`);
    frameId = 0;
  };

  // 指针事件只记录最新位置，实际样式写入合并到下一帧，避免高频触发布局更新。
  hero.addEventListener("pointermove", (event) => {
    const bounds = hero.getBoundingClientRect();
    pendingX = ((event.clientX - bounds.left) / bounds.width - 0.5) * 2;
    pendingY = ((event.clientY - bounds.top) / bounds.height - 0.5) * 2;
    if (!frameId) {
      frameId = window.requestAnimationFrame(renderPosition);
    }
  });

  // 离开首屏后让两个信号块回到设计基准位置，下一次进入不会继承旧坐标。
  hero.addEventListener("pointerleave", () => {
    pendingX = 0;
    pendingY = 0;
    if (frameId) {
      window.cancelAnimationFrame(frameId);
    }
    renderPosition();
  });
})();
