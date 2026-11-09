/*
 * 全局下拉框增强:把页面里的原生 <select> 升级为与后台调性一致的自绘下拉。
 *
 * 原生下拉展开后的选项面板由浏览器绘制,CSS 改不了,所以这里用自绘面板替代。
 * 原 <select> 保留在 DOM 里(隐藏),表单提交、change 事件、required 校验、
 * 页面上其他脚本对 select.value/options 的读写都照常工作,页面模板无需改动。
 *
 * 键盘:Enter/空格/↓ 展开,↑↓ 移动,Enter 选中,Esc 关闭,输入首字母跳转。
 */
(() => {
  "use strict";

  const OPEN_CLASS = "ui-select-open";
  let openInstance = null;

  function closeOpen() {
    if (openInstance) openInstance.close();
  }

  document.addEventListener("click", (event) => {
    // 用 composedPath 判断:点击发生后目标节点可能已被重建(脱离 DOM),contains 会误判为外部点击。
    if (openInstance && !event.composedPath().includes(openInstance.root)) closeOpen();
  });
  window.addEventListener("resize", closeOpen);

  class UiSelect {
    constructor(select) {
      this.select = select;
      this.active = -1;
      this.typed = "";
      this.typedTimer = null;
      this.build();
      this.sync();
      // 其他脚本改了 value/选项(例如联动过滤)时,用观察器同步自绘部分。
      new MutationObserver(() => this.sync()).observe(select, {
        childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "hidden", "selected"],
      });
      select.addEventListener("change", () => this.sync());
      // 表单 reset 或脚本直接赋值 value 不会触发 change,事件循环末再对齐一次。
      select.form?.addEventListener("reset", () => setTimeout(() => this.sync(), 0));
    }

    build() {
      const select = this.select;
      const root = document.createElement("div");
      root.className = "ui-select";
      if (select.classList.contains("ui-select-wide")) root.classList.add("ui-select-wide");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ui-select-trigger";
      button.setAttribute("aria-haspopup", "listbox");
      button.setAttribute("aria-expanded", "false");
      const label = document.createElement("span");
      label.className = "ui-select-label";
      const caret = document.createElement("span");
      caret.className = "ui-select-caret";
      caret.setAttribute("aria-hidden", "true");
      button.append(label, caret);
      const panel = document.createElement("ul");
      panel.className = "ui-select-panel";
      panel.setAttribute("role", "listbox");
      panel.hidden = true;
      root.append(button, panel);
      select.parentNode.insertBefore(root, select);
      root.appendChild(select);
      select.classList.add("ui-select-native");
      select.tabIndex = -1;
      select.setAttribute("aria-hidden", "true");
      Object.assign(this, { root, button, label, panel });

      button.addEventListener("click", () => (this.isOpen() ? this.close() : this.open()));
      button.addEventListener("keydown", (event) => this.onKey(event));
      panel.addEventListener("mousedown", (event) => event.preventDefault());
      panel.addEventListener("click", (event) => {
        // 下拉常被包在 <label> 里,浏览器会把 label 内的点击转发给它的第一个可交互控件
        // (即触发按钮),导致刚选完面板又被重新打开,这里阻止这次转发。
        event.preventDefault();
        const item = event.target.closest("[data-index]");
        if (item && !item.classList.contains("is-disabled")) this.choose(Number(item.dataset.index));
      });
      // 必填校验失败时浏览器要聚焦原生控件,这里把焦点转给触发按钮。
      select.addEventListener("focus", () => button.focus());
    }

    isOpen() { return !this.panel.hidden; }

    visibleOptions() {
      return Array.from(this.select.options).map((option, index) => ({ option, index })).filter(({ option }) => !option.hidden);
    }

    sync() {
      const select = this.select;
      this.button.disabled = select.disabled;
      const selected = select.selectedOptions[0];
      const text = selected ? selected.textContent.trim() : "";
      this.label.textContent = text || select.getAttribute("placeholder") || "请选择";
      // 选中的是“空值”选项(如“全部”“请选择”)时用弱化颜色,和占位符一致。
      this.root.classList.toggle("is-placeholder", !selected || selected.value === "");
      this.root.classList.toggle("is-disabled", select.disabled);
      this.panel.replaceChildren(
        ...this.visibleOptions().map(({ option, index }) => {
          const item = document.createElement("li");
          item.className = "ui-select-option";
          item.setAttribute("role", "option");
          item.dataset.index = String(index);
          item.textContent = option.textContent.trim() || "\u00a0";
          if (option.disabled) item.classList.add("is-disabled");
          if (option.selected) { item.classList.add("is-selected"); item.setAttribute("aria-selected", "true"); }
          return item;
        }),
      );
    }

    open() {
      if (this.select.disabled) return;
      closeOpen();
      this.panel.hidden = false;
      this.root.classList.add(OPEN_CLASS);
      this.button.setAttribute("aria-expanded", "true");
      openInstance = this;
      // 靠近视口底部时向上展开,避免面板被截断。
      const rect = this.button.getBoundingClientRect();
      const room = window.innerHeight - rect.bottom;
      this.root.classList.toggle("ui-select-up", room < 260 && rect.top > room);
      const selectedItem = this.panel.querySelector(".is-selected") || this.panel.querySelector(".ui-select-option:not(.is-disabled)");
      this.setActive(selectedItem);
    }

    close() {
      this.panel.hidden = true;
      this.root.classList.remove(OPEN_CLASS, "ui-select-up");
      this.button.setAttribute("aria-expanded", "false");
      if (openInstance === this) openInstance = null;
    }

    setActive(item) {
      this.panel.querySelectorAll(".is-active").forEach((el) => el.classList.remove("is-active"));
      if (!item) return;
      item.classList.add("is-active");
      item.scrollIntoView({ block: "nearest" });
    }

    choose(index) {
      const select = this.select;
      // 先关面板再改值:改值触发的 change 会让页面脚本(或表单自动提交)运行,
      // 面板必须已经收起,否则 sync() 重建选项后会留下一个悬空的展开态。
      this.close();
      if (select.selectedIndex !== index) {
        select.selectedIndex = index;
        // 与原生行为一致:先 input 再 change,页面里的 onchange / 联动脚本照常触发。
        select.dispatchEvent(new Event("input", { bubbles: true }));
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      this.sync();
      this.button.focus();
    }

    move(step) {
      const items = Array.from(this.panel.querySelectorAll(".ui-select-option:not(.is-disabled)"));
      if (!items.length) return;
      const current = items.indexOf(this.panel.querySelector(".is-active"));
      this.setActive(items[Math.min(Math.max(current + step, 0), items.length - 1)]);
    }

    onKey(event) {
      const key = event.key;
      if (!this.isOpen()) {
        if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") {
          event.preventDefault();
          this.open();
        }
        return;
      }
      if (key === "Escape") { event.preventDefault(); this.close(); }
      else if (key === "ArrowDown") { event.preventDefault(); this.move(1); }
      else if (key === "ArrowUp") { event.preventDefault(); this.move(-1); }
      else if (key === "Enter" || key === " ") {
        event.preventDefault();
        const active = this.panel.querySelector(".is-active");
        if (active) this.choose(Number(active.dataset.index));
      } else if (key === "Tab") { this.close(); }
      else if (key.length === 1) {
        // 连续输入字符按前缀跳转到匹配项。
        clearTimeout(this.typedTimer);
        this.typed += key.toLowerCase();
        this.typedTimer = setTimeout(() => { this.typed = ""; }, 600);
        const hit = Array.from(this.panel.querySelectorAll(".ui-select-option:not(.is-disabled)"))
          .find((el) => el.textContent.trim().toLowerCase().startsWith(this.typed));
        if (hit) this.setActive(hit);
      }
    }
  }

  function enhance(root) {
    root.querySelectorAll("select:not([multiple]):not([size]):not(.ui-select-native):not([data-native-select])").forEach((select) => {
      if (select.closest(".ui-select")) return;
      new UiSelect(select);
    });
  }

  function start() {
    enhance(document);
    // 脚本动态插入的下拉(如弹窗、联动生成)也自动升级。
    new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((node) => {
          if (node.nodeType !== 1) return;
          if (node.matches?.("select")) enhance(node.parentNode || document);
          else if (node.querySelector?.("select")) enhance(node);
        });
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
