(function () {
  "use strict";

  // 企业与平台共用绘图和请求逻辑；接口范围由服务端身份控制，前端不传租户编号。
  var metrics = ["success", "matched", "error"];
  var number = new Intl.NumberFormat("zh-CN");
  var ns = "http://www.w3.org/2000/svg";
  function count(value) { return Number.isFinite(value) ? Math.max(0, value) : 0; }
  function svgNode(name, attributes) {
    var node = document.createElementNS(ns, name);
    Object.keys(attributes).forEach(function (key) { node.setAttribute(key, attributes[key]); });
    return node;
  }

  document.querySelectorAll("[data-plugin-usage]").forEach(function (root) {
    var cards = Array.from(root.querySelectorAll("[data-usage-plugin]"));
    var updated = root.querySelector("[data-usage-updated]");
    var period = "24h", controller = null, sequence = 0;
    var chartStates = new WeakMap();

    function sortCards(plugins) {
      var grid = root.querySelector("[data-usage-grid]");
      if (!grid) return;
      // 活动时间独立于图表范围；同一时间或无活动时按初始位置稳定排序。
      // 移动已有节点保留开关、弹窗和筛选状态，仅移动顺序发生变化的卡片。
      cards.map(function (card, index) {
        return {node: card.closest("[data-usage-card]"), index: index,
          activity: Date.parse(plugins[card.dataset.usagePlugin].last_activity_at) || 0};
      }).sort(function (first, second) {
        return second.activity - first.activity || first.index - second.index;
      }).forEach(function (item, index) {
        if (grid.children[index] !== item.node) grid.insertBefore(item.node, grid.children[index] || null);
      });
    }

    function readPoint(card, index) {
      var state = chartStates.get(card);
      if (!state) return;
      index = Math.max(0, Math.min(state.series.length - 1, index));
      state.index = index;
      var point = state.series[index], x = index * 300 / Math.max(state.series.length - 1, 1);
      state.marker.setAttribute("x1", x);
      state.marker.setAttribute("x2", x);
      state.svg.setAttribute("aria-valuenow", index + 1);
      var label = state.buckets[index].time + " · 命中 " + number.format(count(point.matched)) +
        " · 正常 " + number.format(count(point.success)) + " · 异常 " + number.format(count(point.error));
      if (point.blocked) label += " · 规则拦截 " + number.format(count(point.blocked));
      card.querySelector("[data-usage-readout]").textContent = label;
      state.svg.setAttribute("aria-valuetext", label);
    }

    function render(card, data, buckets) {
      // 缺失的插件数据不能冒充零调用，可能是统计请求期间可见范围已发生变化。
      if (!data || !Array.isArray(data.series) || data.series.length !== buckets.length || !buckets.length) {
        throw new Error("统计数据不完整");
      }
      card.querySelectorAll("[data-usage-value]").forEach(function (node) {
        node.textContent = number.format(count(data.totals[node.dataset.usageValue]));
      });
      var last = card.querySelector("[data-usage-last]");
      last.textContent = data.last_called_label;
      if (data.last_called_at) last.setAttribute("datetime", data.last_called_at);
      else last.removeAttribute("datetime");
      var svg = card.querySelector("svg"), maximum = 1;
      var hasUsage = metrics.some(function (key) { return count(data.totals[key]) > 0; }) ||
        data.series.some(function (point) { return metrics.some(function (key) { return count(point[key]) > 0; }); });
      if (!hasUsage) {
        // 本期没有任何调用时隐藏空折线图，仅保留紧凑统计数值与最近调用时间，减少卡片占用空间。
        svg.replaceChildren();
        chartStates.delete(card);
        card.classList.add("is-idle");
        card.querySelector("[data-usage-plot]").hidden = true;
        card.setAttribute("aria-busy", "false");
        card.querySelector("[data-usage-readout]").textContent = "本期暂无调用";
        return;
      }
      card.classList.remove("is-idle");
      data.series.forEach(function (point) {
        metrics.forEach(function (key) { maximum = Math.max(maximum, count(point[key])); });
      });
      svg.replaceChildren();
      [2, 40, 78].forEach(function (y) {
        svg.appendChild(svgNode("line", {x1: 0, y1: y, x2: 300, y2: y, "class": "plugin-usage-gridline"}));
      });
      metrics.forEach(function (key) {
        // 没有发生过的指标保留零计数，不绘制醒目的有色零线来暗示存在异常。
        if (!data.series.some(function (point) { return count(point[key]) > 0; })) return;
        var points = data.series.map(function (point, index) {
          return (index * 300 / Math.max(data.series.length - 1, 1)) + "," + (78 - count(point[key]) / maximum * 76);
        }).join(" ");
        svg.appendChild(svgNode("polyline", {points: points, "class": "plugin-usage-line plugin-usage-" + key}));
      });
      var marker = svgNode("line", {x1: 300, y1: 0, x2: 300, y2: 80, "class": "plugin-usage-marker"});
      svg.appendChild(marker);
      svg.setAttribute("aria-valuemax", buckets.length);
      card.querySelector("[data-usage-max]").textContent = number.format(maximum);
      var ticks = card.querySelectorAll(".plugin-usage-xaxis span");
      [0, Math.floor((buckets.length - 1) / 2), buckets.length - 1].forEach(function (index, i) { ticks[i].textContent = buckets[index].label; });
      chartStates.set(card, {series: data.series, buckets: buckets, svg: svg, marker: marker, index: buckets.length - 1});
      card.querySelector("[data-usage-plot]").hidden = false;
      card.setAttribute("aria-busy", "false");
      readPoint(card, buckets.length - 1);
    }

    cards.forEach(function (card) {
      var svg = card.querySelector("svg");
      function pointer(event) {
        var state = chartStates.get(card);
        if (!state) return;
        var rect = svg.getBoundingClientRect();
        if (rect.width) readPoint(card, Math.round((event.clientX - rect.left) / rect.width * (state.series.length - 1)));
      }
      // 只观察指针，不阻止手机纵向滑动；点击、悬停和键盘读取同一时间桶。
      svg.addEventListener("pointermove", pointer);
      svg.addEventListener("pointerdown", pointer);
      svg.addEventListener("keydown", function (event) {
        var state = chartStates.get(card);
        if (!state) return;
        var index = state.index;
        if (event.key === "ArrowLeft" || event.key === "ArrowDown") index -= 1;
        else if (event.key === "ArrowRight" || event.key === "ArrowUp") index += 1;
        else if (event.key === "Home") index = 0;
        else if (event.key === "End") index = state.series.length - 1;
        else return;
        event.preventDefault();
        readPoint(card, index);
      });
    });

    async function load() {
      var request = ++sequence;
      if (controller) controller.abort();
      controller = new AbortController();
      updated.textContent = "正在更新统计…";
      root.querySelectorAll("[data-usage-period]").forEach(function (button) { button.setAttribute("aria-pressed", String(button.dataset.usagePeriod === period)); });
      cards.forEach(function (card) {
        chartStates.delete(card);
        card.setAttribute("aria-busy", "true");
        card.querySelector("[data-usage-plot]").hidden = true;
        card.querySelectorAll("[data-usage-value]").forEach(function (node) { node.textContent = "—"; });
        card.querySelector("[data-usage-last]").textContent = "—";
        card.querySelector("[data-usage-last]").removeAttribute("datetime");
        card.querySelector("[data-usage-readout]").textContent = "正在加载调用统计…";
      });
      try {
        var response = await fetch(root.dataset.usageUrl + "?period=" + encodeURIComponent(period), {
          signal: controller.signal, credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}
        });
        if (!response.ok || !String(response.headers.get("content-type")).includes("application/json")) throw new Error("统计请求失败");
        var data = await response.json();
        // 即使旧请求已经到达服务器，后返回的旧范围也不能覆盖用户的新选择。
        if (request !== sequence) return;
        cards.forEach(function (card) { render(card, data.plugins[card.dataset.usagePlugin], data.buckets); });
        sortCards(data.plugins);
        updated.textContent = "更新于 " + data.generated_label;
      } catch (error) {
        if (request !== sequence || error.name === "AbortError") return;
        updated.textContent = "统计加载失败，可点击刷新重试";
        cards.forEach(function (card) {
          chartStates.delete(card);
          card.setAttribute("aria-busy", "false");
          card.querySelector("[data-usage-plot]").hidden = true;
          card.querySelectorAll("[data-usage-value]").forEach(function (node) { node.textContent = "—"; });
          card.querySelector("[data-usage-readout]").textContent = "统计暂不可用";
          card.querySelector("[data-usage-last]").textContent = "—";
          card.querySelector("[data-usage-last]").removeAttribute("datetime");
        });
      }
    }
    root.querySelectorAll("[data-usage-period]").forEach(function (button) {
      button.addEventListener("click", function () { period = button.dataset.usagePeriod; load(); });
    });
    root.querySelector("[data-usage-refresh]").addEventListener("click", load);
    load();
  });
}());
