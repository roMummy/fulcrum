# 套餐价格居中与 Swiss Hero 交互 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复公开套餐价格页左右偏移，并将营销首屏替换为无截图、可交互、支持减少动态效果的 Swiss 几何场景。

**Architecture:** 保留现有 FastAPI、Jinja2 与共享营销布局，只移除冲突的网格外边距和旧截图链路。Hero 使用普通 DOM 元素、CSS 自定义属性与一个无依赖原生脚本完成受限指针反馈，业务文案、路由和套餐数据保持不变。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、原生 JavaScript、Pytest、Playwright Chromium、Docker Compose

---

### Task 1: 建立居中和无截图 Hero 失败契约

**Files:**
- Modify: `tests/test_swiss_ui.py`
- Modify: `tests/test_public_marketing_pages.py`

**Step 1: 增加套餐容器契约**

在 `tests/test_swiss_ui.py` 中读取共享营销网格规则，断言三类网格的公共规则不再包含 `margin: 0;`，并断言 `.marketing-steps` 有独立的外边距重置。该测试在当前 CSS 上必须失败，以证明偏移根因被覆盖。

```python
grid_rule = re.search(
    r"\.marketing-feature-grid,\s*\.marketing-steps,\s*\.marketing-plan-grid\s*\{(?P<body>[^}]*)\}",
    stylesheet,
)
assert grid_rule is not None
assert "margin: 0;" not in grid_rule.group("body")
assert "marketing-section-inner marketing-plan-grid" in pricing_template
```

**Step 2: 替换旧截图契约**

删除要求 `/static/images/yaoshu-console.webp` 和 `.marketing-product-visual` 存在的断言，改为要求：

```python
assert 'class="marketing-hero-scene"' in marketing_home
assert 'data-hero-scene' in marketing_home
assert 'aria-hidden="true"' in marketing_home
assert "/static/js/marketing.js" in marketing_base
assert "yaoshu-console.webp" not in marketing_home
assert "marketing-product-visual" not in marketing_home
```

在公开页面测试中删除 WebP 文件头测试和不再使用的 `Path` 导入，增加响应 HTML 不包含旧截图、但包含新 Hero 场景的断言。

**Step 3: 运行失败测试**

Run: `SWISS_TEST_LOG_DIR=$(mktemp -d) LOG_DIR="$SWISS_TEST_LOG_DIR" .venv/bin/python -m pytest tests/test_swiss_ui.py tests/test_public_marketing_pages.py -q`

Expected: FAIL，失败集中在公共网格仍重置外边距、新 Hero 结构和脚本尚不存在。

**Step 4: 提交失败契约**

```bash
git add tests/test_swiss_ui.py tests/test_public_marketing_pages.py
git commit -m "测试：约束套餐居中与Swiss Hero交互"
```

### Task 2: 修复套餐页居中

**Files:**
- Modify: `app/static/css/style.css`
- Test: `tests/test_swiss_ui.py`

**Step 1: 删除公共网格外边距覆盖**

从 `.marketing-feature-grid, .marketing-steps, .marketing-plan-grid` 公共规则中删除 `margin: 0;`，让同时具备 `.marketing-section-inner` 的价格页网格继续使用共享容器的 `margin: 0 auto`。

**Step 2: 单独重置有默认外边距的步骤列表**

增加详细中文注释，并只对有浏览器默认列表外边距的 `.marketing-steps` 重置：

```css
/* 只有有序列表需要清除浏览器默认外边距，价格网格必须保留内容容器的自动居中。 */
.marketing-steps {
  margin: 0;
}
```

**Step 3: 运行套餐居中静态契约**

Run: `LOG_DIR=$(mktemp -d) .venv/bin/python -m pytest tests/test_swiss_ui.py -q`

Expected: 新增套餐容器断言通过；Hero 相关断言仍失败。

### Task 3: 实现无截图 Swiss Hero 交互

**Files:**
- Modify: `app/templates/marketing_home.html`
- Modify: `app/templates/marketing_base.html`
- Modify: `app/static/css/style.css`
- Create: `app/static/js/marketing.js`
- Delete: `app/static/images/yaoshu-console.webp`
- Test: `tests/test_swiss_ui.py`
- Test: `tests/test_public_marketing_pages.py`

**Step 1: 替换 Hero 展示节点**

删除 `.marketing-product-visual` 与图片节点，增加一个不进入无障碍树的几何场景：

```html
<div class="marketing-hero-scene" data-hero-scene aria-hidden="true">
  <span class="marketing-hero-axis marketing-hero-axis-horizontal"></span>
  <span class="marketing-hero-axis marketing-hero-axis-vertical"></span>
  <span class="marketing-hero-signal"><span></span></span>
  <span class="marketing-hero-marker"></span>
  <span class="marketing-hero-index">01 / 03</span>
</div>
```

模板注释必须用中文说明场景只承担视觉和交互反馈，业务信息继续由正文表达。

**Step 2: 加载独立原生脚本**

在 `marketing_base.html` 的 `</body>` 前使用 `defer` 加载 `/static/js/marketing.js`。脚本只查询 `[data-hero-scene]`，不存在时立即结束，不影响登录、注册和价格页。

**Step 3: 实现受限指针反馈**

`app/static/js/marketing.js` 使用严格模式和详细中文注释：

```javascript
(() => {
  "use strict";

  const scene = document.querySelector("[data-hero-scene]");
  const hero = scene?.closest(".marketing-hero");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (!scene || !hero || reduceMotion.matches) return;

  const reset = () => {
    scene.style.setProperty("--hero-pointer-x", "0");
    scene.style.setProperty("--hero-pointer-y", "0");
  };

  hero.addEventListener("pointermove", (event) => {
    const bounds = hero.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width - 0.5) * 2;
    const y = ((event.clientY - bounds.top) / bounds.height - 0.5) * 2;
    scene.style.setProperty("--hero-pointer-x", Math.max(-1, Math.min(1, x)).toFixed(3));
    scene.style.setProperty("--hero-pointer-y", Math.max(-1, Math.min(1, y)).toFixed(3));
  });
  hero.addEventListener("pointerleave", reset);
})();
```

**Step 4: 用几何场景 CSS 替换截图样式**

删除 `.marketing-product-visual` 和图片透明度规则。场景使用绝对定位、纯色块、两条低对比轴线与 CSS 自定义属性。红色主块移动范围约 `12px`，蓝色标记反向移动约 `6px`；`.marketing-hero:focus-within` 提供键盘反馈；减少动态效果媒体查询关闭过渡和 transform。

**Step 5: 删除废弃图片资产**

删除 `app/static/images/yaoshu-console.webp`，并通过 `rg` 确认仓库不再保留该文件名或旧 `.marketing-product-visual` 调用链。

**Step 6: 运行定向测试**

Run: `SWISS_TEST_LOG_DIR=$(mktemp -d) LOG_DIR="$SWISS_TEST_LOG_DIR" .venv/bin/python -m pytest tests/test_swiss_ui.py tests/test_public_marketing_pages.py -q`

Expected: PASS。

**Step 7: 提交实现**

```bash
git add app/templates/marketing_home.html app/templates/marketing_base.html app/static/css/style.css app/static/js/marketing.js app/static/images/yaoshu-console.webp tests/test_swiss_ui.py tests/test_public_marketing_pages.py
git commit -m "界面：修复套餐居中并重构Swiss Hero交互"
```

### Task 4: 浏览器验收和响应式校准

**Files:**
- Modify when required: `app/static/css/style.css`
- Modify when required: `app/static/js/marketing.js`

**Step 1: 构建并差异替换应用容器**

Run: `docker compose build app`

Run: `docker compose up -d --no-deps app`

Expected: 只重建 `multi-bot-saas-app`，MariaDB 保持运行。

**Step 2: 验收套餐页**

在 `1536x960` 和 `390x844` 视口打开 `/pricing`，输出并检查：

- 根节点 `scrollWidth === clientWidth`；
- 桌面网格左右留白差值不超过 `1px`；
- 移动网格左右留白差值不超过 `1px`；
- 三张套餐卡片均在网格边界内；
- 推荐套餐标签、价格和按钮不越界。

**Step 3: 验收 Hero 交互**

在相同视口打开 `/`，输出并检查：

- 页面不请求 `yaoshu-console.webp`；
- Hero 正文、主信号块和标记边界不重叠；
- 指针从中心移动到右下角后，CSS 自定义属性从 `0` 变化；
- 指针离开后属性归零；
- 主要按钮键盘聚焦可见；
- `reduced_motion="reduce"` 环境下 transform 为静态值；
- 下一业务区在首屏底部保持可见。

**Step 4: 保存桌面和移动截图**

保存到 `.run/ui-audit/pricing-hero-after/`，人工检查 Swiss 构图、价格居中、文字层级和移动端遮挡。

**Step 5: 运行最终轻量检查**

Run: `SWISS_TEST_LOG_DIR=$(mktemp -d) LOG_DIR="$SWISS_TEST_LOG_DIR" .venv/bin/python -m pytest tests/test_swiss_ui.py tests/test_public_marketing_pages.py -q`

Run: `git diff --check`

Expected: PASS。

**Step 6: 提交浏览器校准**

仅在浏览器验收产生代码调整时提交：

```bash
git add app/static/css/style.css app/static/js/marketing.js
git commit -m "界面：完成套餐与Hero响应式校准"
```

### Task 5: 生产验证与推送

**Files:**
- No source changes expected

**Step 1: 验证生产接口和容器**

Run: `curl -fsS http://127.0.0.1:8280/healthz`

Run: `curl -fsS http://127.0.0.1:8280/readyz`

Run: `docker compose ps`

Expected: 健康与就绪接口成功，应用和 MariaDB 均为 healthy。

**Step 2: 比对容器和工作区静态文件**

对 `style.css` 与 `marketing.js` 分别运行本地和容器内 `sha256sum`，确认生产容器使用当前工作区版本。

**Step 3: 推送当前分支**

Run: `git push origin ui/scandinavian-refactor`

Expected: 远程分支指向最终提交，工作区仅保留用户自有 `.rcodex/`。

