"""群聊周报长图模板。

渲染器只接收 ``MessageSummaryReport`` 这种已经过校验的服务端对象，使用固定的
HTML/CSS 模板和无文字 SVG 连接线生成米白背景、深绿色标题和四段数据图表，
最后统一交给项目公共 Kitesurf 截图服务。模型永远不能直接提供 HTML、CSS 或 SVG。
"""

from __future__ import annotations

import html
import importlib.util
import io
import json
import logging
import sys
import base64
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.safe_fetch import open_public_url
from app.services.web_screenshots import ScreenshotRequest, ScreenshotResult, capture_screenshot


_report_spec = importlib.util.spec_from_file_location(
    f"{__name__}_report",
    Path(__file__).with_name("report.py"),
)
if _report_spec is None or _report_spec.loader is None:
    raise ImportError("群总结报告模块不可用")
_report_module = importlib.util.module_from_spec(_report_spec)
sys.modules[_report_spec.name] = _report_module
_report_spec.loader.exec_module(_report_module)
MessageSummaryReport = _report_module.MessageSummaryReport
report_to_text = _report_module.report_to_text


CANVAS_WIDTH = 1080
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_PIXELS = 4_000_000
# 页面头像实际显示为 42px，缓存为两倍尺寸即可保留清晰度并控制截图 HTML 体积。
MAX_AVATAR_SIDE = 84
DEFAULT_AVATAR_SIDE = 84
_logger = logging.getLogger(__name__)
_PALETTE = ("#2d6556", "#d98e55", "#7b9e88", "#d8b66d", "#7e6a99", "#b97870", "#4d8c9a", "#bba06c")
_ASSET_DIR = Path(__file__).with_name("assets")
try:
    _VISUALIZATION_RUNTIME = (_ASSET_DIR / "visualization-runtime.js").read_text(encoding="utf-8")
except OSError:
    _VISUALIZATION_RUNTIME = ""


def render_summary_html(report: MessageSummaryReport) -> str:
    """生成受控长图 HTML；所有动态文字都在进入模板前做 HTML 转义。"""

    title = _escape(report.conversation_title)
    headline = _escape(report.headline)
    intro = _escape(report.intro)
    period = _escape(report.period_label)
    analytics = report.analytics
    member_total = "—" if analytics.group_member_count is None else str(analytics.group_member_count)
    active_ratio = "—" if analytics.active_ratio_percent is None else f"{analytics.active_ratio_percent:.1f}%"
    change_text = f"加入 {analytics.member_changes.joined_count} · 退出 {analytics.member_changes.left_count}"
    visualization_data = _visualization_data(report)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>{_styles()}</style>
  <script>{_VISUALIZATION_RUNTIME}</script>
</head>
<body>
  <main class="sheet">
    <header class="hero">
      <div class="eyebrow"><span class="brand-name">{title}</span><span class="data-label">WEEKLY / DATA</span></div>
      <div class="hero-grid">
        <div>
          <div class="period">{period}</div>
          <h1>{headline}</h1>
          <p class="intro">{intro or "本期群聊的关键讨论与活跃趋势。"}{_intro_avatars(report)}</p>
        </div>
        <div class="hero-stats" aria-label="群聊统计">
          <div><strong>{analytics.message_count}</strong><span>消息</span></div>
          <div><strong>{analytics.active_member_count}</strong><span>活跃成员</span></div>
          <div><strong>{member_total}</strong><span>群成员</span></div>
          <div><strong>{active_ratio}</strong><span>活跃比例</span></div>
        </div>
      </div>
      <div class="hero-foot"><span>{title}</span><span>{_escape(change_text)}</span></div>
    </header>

    <section id="hot-topics" class="section hot-section">
      <div class="section-heading"><span class="section-number">01</span><div><p class="section-kicker">CONVERSATION MAP</p><h2>群聊热点</h2></div><span class="section-note">按相关消息数量</span></div>
      <div class="hot-layout"><div class="bubble-field">{_hot_bubbles(report)}</div><div class="hot-legend">{_hot_legend(report)}</div></div>
    </section>

    <section id="top-topics" class="section">
      <div class="section-heading"><span class="section-number">02</span><div><p class="section-kicker">EDITOR'S PICK</p><h2>本期重点话题</h2></div><span class="section-note">Top 3</span></div>
      <div class="topic-cards">{_topic_cards(report)}</div>
    </section>

    <section id="contribution-donut" class="section contribution-section">
      <div class="section-heading"><span class="section-number">03</span><div><p class="section-kicker">WHO SPOKE</p><h2>群友发言贡献</h2></div><span class="section-note">消息分布</span></div>
      <div class="contribution-layout"><div class="donut-wrap">{_donut_chart(report)}</div><div class="contribution-list">{_contribution_list(report)}</div></div>
    </section>

    <section id="daily-activity" class="section daily-section">
      <div class="section-heading"><span class="section-number">04</span><div><p class="section-kicker">RHYTHM OF THE GROUP</p><h2>每日消息量与活跃比例</h2></div><span class="section-note">本地统计</span></div>
      {_daily_chart(report)}
    </section>

    <footer class="footer"><span>FULCRUM · GROUP SUMMARY</span><span>数字由本地消息与群成员目录计算</span></footer>
  </main>
  <script>window.__FULCRUM_SUMMARY__ = {_script_json(visualization_data)};{_visualization_bootstrap()}</script>
</body>
</html>"""


def capture_summary_image(report: MessageSummaryReport) -> ScreenshotResult:
    """使用项目共享 Kitesurf 服务生成完整长图。"""

    return capture_screenshot(
        ScreenshotRequest(
            html=render_summary_html(_prepare_report_avatars(report)),
            width=CANVAS_WIDTH,
            height=900,
            full_page=True,
            format="png",
            wait_ms=350,
        )
    )


def summary_text(report: MessageSummaryReport) -> str:
    """暴露同源文本降级入口，供截图服务不可用时使用。"""

    return report_to_text(report)


def _visualization_data(report: MessageSummaryReport) -> dict[str, Any]:
    """把报告转换成图表专用数据，避免把头像或原始消息注入浏览器脚本。"""

    daily = []
    for item in report.analytics.daily_activity:
        weekday, date_label = _daily_label(item.date_key)
        daily.append(
            {
                "date": item.date_key,
                "weekday": weekday,
                "dateLabel": date_label,
                "messageCount": item.message_count,
                "activeRatio": item.active_ratio_percent,
            }
        )
    contributions = [
        {
            "name": item.name,
            "value": item.message_count,
            "share": item.share_percent,
        }
        for item in report.analytics.contributions[:6]
    ]
    total = report.analytics.message_count or 1
    remaining = max(0, report.analytics.message_count - sum(item["value"] for item in contributions))
    if remaining and contributions:
        contributions.append(
            {
                "name": "其余",
                "value": remaining,
                "share": round(remaining / total * 100, 1),
            }
        )
    return {
        "theme": {
            "palette": list(_PALETTE),
            "green": "#2d7f6c",
            "orange": "#df9863",
            "blue": "#1d579d",
            "ink": "#304943",
            "muted": "#6d7f78",
            "grid": "#d6ddd6",
            "paper": "#f7f3eb",
        },
        "totalMessages": report.analytics.message_count,
        "topics": [
            {
                "name": topic.title,
                "value": topic.message_count,
                "messageCount": topic.message_count,
            }
            for topic in report.topics[:16]
        ],
        "contributions": contributions,
        "daily": daily,
    }


def _daily_label(value: str) -> tuple[str, str]:
    """生成参考图中的星期和月日双行标签，解析失败时保留原始日期。"""

    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return "", text
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return weekdays[parsed.weekday()], parsed.strftime("%m/%d")


def _script_json(value: Any) -> str:
    """安全嵌入脚本的数据，阻止报告文字结束当前 script 标签。"""

    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _visualization_bootstrap() -> str:
    """初始化所有图表；脚本不依赖悬停交互，截图即为完整信息。"""

    return r"""
(function () {
  const data = window.__FULCRUM_SUMMARY__;
  const runtime = window.FulcrumViz;
  if (!data || !runtime || !runtime.echarts) return;
  const echarts = runtime.echarts;
  const theme = data.theme;
  const fontFamily = "sans-serif";
  const markReady = (node) => {
    if (node) node.dataset.chartReady = "true";
  };
  const palette = theme.palette;

  const cloud = document.getElementById("hot-word-cloud");
  if (cloud && data.topics.length && runtime.cloudLayout) {
    const width = cloud.clientWidth;
    const height = cloud.clientHeight;
    const values = data.topics.map((item) => item.value);
    const minValue = Math.min(...values);
    const maxValue = Math.max(...values);
    let randomSeed = data.topics.reduce((sum, item) => sum + item.name.length * 17 + item.value, 11);
    const random = () => {
      randomSeed = (randomSeed * 9301 + 49297) % 233280;
      return randomSeed / 233280;
    };
    const fontSize = (value) => {
      if (maxValue === minValue) return 48;
      return 24 + ((value - minValue) / (maxValue - minValue)) * 56;
    };
    runtime.cloudLayout()
      .size([width, height])
      .words(data.topics.map((item, index) => ({text: item.name, value: item.value, index})))
      .padding(8)
      .font(fontFamily)
      .fontWeight(750)
      .fontSize((item) => fontSize(item.value))
      .rotate((item) => (item.index % 3 === 0 ? -12 : item.index % 3 === 1 ? 0 : 12))
      .random(random)
      .on("end", () => {
        const slots = [
          [50, 43, -6], [30, 32, 10], [70, 32, -8], [22, 52, -12],
          [78, 52, 12], [39, 68, 0], [62, 68, 0], [50, 22, 0],
          [50, 81, 0], [15, 72, 8], [85, 72, -8], [31, 83, 0],
          [69, 83, 0], [16, 34, 0], [84, 34, 0], [50, 58, 0],
        ];
        cloud.querySelectorAll(".cloud-word").forEach((node) => node.remove());
        data.topics.forEach((item, index) => {
          const slot = slots[index % slots.length];
          const text = document.createElement("span");
          text.className = "cloud-word";
          text.textContent = item.name;
          text.style.left = `${slot[0]}%`;
          text.style.top = `${slot[1]}%`;
          text.style.color = palette[index % palette.length];
          text.style.fontFamily = fontFamily;
          const size = fontSize(item.value);
          text.style.fontSize = `${Math.max(22, Math.min(62, size))}px`;
          text.style.fontWeight = "750";
          text.style.transform = `translate(-50%, -50%) rotate(${slot[2]}deg)`;
          cloud.appendChild(text);
        });
        markReady(cloud);
      })
      .start();
  }

  const contribution = document.getElementById("contribution-chart");
  if (contribution && data.contributions.length) {
    const chart = echarts.init(contribution, null, {renderer: "svg"});
    chart.setOption({
      animation: false,
      color: palette,
      tooltip: {show: false},
      legend: {show: false},
      series: [{
        type: "pie",
        radius: ["56%", "76%"],
        center: ["38%", "50%"],
        avoidLabelOverlap: true,
        itemStyle: {borderColor: theme.paper, borderWidth: 3},
        label: {show: false},
        labelLine: {show: false},
        data: data.contributions.map((item) => ({name: item.name, value: item.value})),
      }],
    });
    markReady(contribution);
  }

  const daily = document.getElementById("daily-activity-chart");
  if (daily && data.daily.length) {
    const ratios = data.daily.map((item) => item.activeRatio).filter((item) => typeof item === "number");
    const hasRatios = ratios.length > 0;
    const maxRatio = hasRatios ? Math.max(...ratios) : 0;
    const ratioMax = Math.min(100, Math.max(20, Math.ceil(maxRatio / 5) * 5));
    const maxMessages = Math.max(1, ...data.daily.map((item) => item.messageCount));
    const maxIndex = data.daily.findIndex((item) => item.messageCount === maxMessages);
    const chart = echarts.init(daily, null, {renderer: "canvas"});
    const series = [{
      name: "消息量（条）",
      type: "bar",
      barMaxWidth: 58,
      yAxisIndex: 0,
      data: data.daily.map((item, index) => ({
        value: item.messageCount,
        itemStyle: {color: index === maxIndex ? theme.orange : theme.green, borderRadius: [8, 8, 0, 0]},
      })),
      label: {show: true, position: "top", color: theme.ink, fontSize: 12, fontWeight: 700},
    }];
    if (hasRatios) {
      series.push({
        name: "活跃群友占比（%）",
        type: "line",
        yAxisIndex: 1,
        smooth: 0.18,
        connectNulls: false,
        symbol: "circle",
        symbolSize: 9,
        lineStyle: {width: 4, color: theme.blue},
        itemStyle: {color: theme.blue, borderColor: theme.paper, borderWidth: 2},
        label: {show: true, position: "top", color: theme.blue, fontSize: 12, fontWeight: 800, formatter: (params) => params.value == null ? "" : params.value + "%"},
        data: data.daily.map((item) => item.activeRatio),
      });
    }
    chart.setOption({
      animation: false,
      aria: {enabled: true, decal: {show: false}},
      color: [theme.green, theme.blue],
      tooltip: {show: false},
      legend: {show: false},
      grid: {left: 66, right: hasRatios ? 66 : 24, top: 54, bottom: 54, containLabel: true},
      xAxis: {
        type: "category",
        data: data.daily.map((item) => item.weekday ? item.weekday + "\n" + item.dateLabel : item.dateLabel),
        axisLine: {lineStyle: {color: theme.grid}},
        axisTick: {show: false},
        axisLabel: {show: false},
      },
      yAxis: [
        {type: "value", splitNumber: 4, min: 0, max: maxMessages, axisLabel: {color: theme.muted, fontFamily, fontSize: 11}, axisLine: {show: false}, axisTick: {show: false}, splitLine: {lineStyle: {color: theme.grid, type: "dashed"}}},
        {type: "value", min: 0, max: ratioMax, interval: ratioMax / 4, axisLabel: {show: hasRatios, color: theme.blue, fontFamily, fontSize: 11, formatter: "{value}%"}, axisLine: {show: false}, axisTick: {show: false}, splitLine: {show: false}},
      ],
      series,
    });
    markReady(daily);
  }
})();
"""


def _styles() -> str:
    return """
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: #eee9de; }
    body { color: #1e3d34; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
    /* Kitesurf 单张长图在当前运行时的实际高度上限约为 2560px。
       画布保持 1080px 的输出宽度，同时用布局缩放把完整报告压入上限，
       避免底部图表被引擎静默裁切后再被公共截图校验拒绝。 */
    .sheet { width: 1260px; zoom: .85; min-height: 900px; padding: 48px 60px 34px; background: #f7f3eb; }
    .hero { border-bottom: 1px solid #cfc9bb; padding-bottom: 30px; }
    .eyebrow, .hero-foot, .section-heading, .contribution-layout, .hot-layout { display: flex; align-items: center; }
    .eyebrow { gap: 12px; color: #6d7e70; font-size: 12px; letter-spacing: 2px; font-weight: 700; }
    .brand-name { color: #173e34; font-size: 24px; letter-spacing: 1.5px; font-weight: 760; }
    .data-label { margin-left: auto; color: #c57947; }
    .hero-grid { display: block; margin-top: 38px; }
    .period { color: #c57947; font-size: 14px; letter-spacing: 1px; font-weight: 700; }
    h1 { max-width: 900px; margin: 20px 0 16px; color: #173e34; font-size: 54px; line-height: 1.08; letter-spacing: -1.7px; font-weight: 850; }
    .intro { max-width: 930px; margin: 0; color: #29463d; font-size: 18px; line-height: 1.7; }
    .intro .avatar { display: inline-grid; width: 39px; height: 39px; margin: 0 4px -8px; vertical-align: baseline; font-size: 12px; }
    .hero-stats { display: none; }
    .hero-stats div { padding: 18px 20px 18px 0; border-bottom: 1px solid #d9d3c8; }
    .hero-stats div:nth-child(odd) { border-right: 1px solid #d9d3c8; margin-right: 20px; }
    .hero-stats strong { display: block; color: #1f4d40; font-size: 34px; line-height: 1; }
    .hero-stats span { display: block; margin-top: 7px; color: #7d8a80; font-size: 12px; letter-spacing: 1px; }
    .hero-foot { justify-content: space-between; margin-top: 29px; color: #7d8a80; font-size: 12px; letter-spacing: .6px; }
    .section { margin-top: 36px; }
    .section-heading { gap: 16px; border-bottom: 1px solid #d7d0c4; padding-bottom: 14px; }
    .section-number { min-width: 42px; color: #c57947; font-family: Georgia, serif; font-size: 24px; font-weight: 700; }
    .section-kicker { margin: 0 0 3px; color: #a28f78; font-size: 10px; letter-spacing: 2px; font-weight: 800; }
    h2 { margin: 0; color: #1f4d40; font-size: 28px; letter-spacing: -.4px; }
    .section-note { margin-left: auto; color: #89958c; font-size: 12px; }
    .hot-layout { align-items: stretch; gap: 24px; min-height: 670px; padding: 18px 0 0; }
    .bubble-field { position: relative; flex: 1; min-height: 660px; overflow: hidden; background: #f7f3eb; }
    .hot-cloud { position: relative; width: 100%; height: 660px; overflow: hidden; }
    .hot-cloud svg { width: 100%; height: 100%; }
    .cloud-word { position: absolute; z-index: 1; display: block; white-space: nowrap; line-height: 1; transform-origin: center; }
    .hot-legend { flex: 0 0 190px; display: block; padding: 32px 0 0 8px; }
    .legend-row { display: flex; gap: 9px; align-items: flex-start; margin-bottom: 13px; color: #56665c; font-size: 12px; line-height: 1.45; }
    .legend-dot { flex: 0 0 auto; width: 8px; height: 8px; margin-top: 4px; border-radius: 50%; }
    .topic-cards { display: grid; gap: 15px; padding-top: 22px; }
    .topic-card { display: grid; grid-template-columns: 48px 1fr; gap: 12px; min-height: 175px; padding: 22px 24px 20px; border-radius: 12px; background: #e4eee8; }
    .topic-card:nth-child(2n) { background: #eee8dc; }
    .topic-rank { color: #c57947; font-family: Georgia, serif; font-size: 34px; font-weight: 700; }
    .topic-card h3 { margin: 0 0 7px; color: #214d40; font-size: 22px; }
    .topic-card p { margin: 0; color: #29463d; font-size: 15px; line-height: 1.55; }
    .topic-meta { grid-column: 2; color: #748177; font-size: 15px; line-height: 1.65; }
    .topic-meta strong { display: inline; color: #214d40; font-size: 17px; }
    .topic-avatars { float: right; display: flex; flex-direction: row-reverse; }
    .topic-avatars .avatar { width: 42px; height: 42px; margin-left: -6px; border: 2px solid #e4eee8; font-size: 11px; }
    .topic-card:nth-child(2n) .topic-avatars .avatar { border-color: #eee8dc; }
    .chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
    .chip { padding: 7px 12px; border-radius: 99px; color: #fff9e8; background: #183e34; font-size: 14px; }
    .quote { margin-top: 12px !important; color: #486259 !important; font-style: normal; }
    .contribution-layout { gap: 34px; align-items: flex-start; padding-top: 18px; }
    .donut-wrap { flex: 0 0 330px; }
    .donut-chart { width: 320px; height: 250px; }
    .donut-center-value, .donut-center-caption { position: absolute; z-index: 2; left: 38%; width: 100px; transform: translate(-50%, -50%); text-align: center; pointer-events: none; }
    .donut-center-value { top: 46%; color: #1f4d40; font-size: 26px; font-weight: 800; line-height: 1; }
    .donut-center-caption { top: 59%; color: #7c897f; font-size: 12px; }
    .contribution-list { flex: 1; }
    .person-row { display: grid; grid-template-columns: 50px 1fr 94px; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid #d8d2c8; }
    /* 参考图使用正方形圆角头像；统一应用到导语、话题卡和贡献列表。 */
    .avatar { display: grid; place-items: center; width: 42px; height: 42px; overflow: hidden; border-radius: 10px; color: #f8f3e9; background: #557a6c; font-weight: 800; }
    .avatar img { width: 100%; height: 100%; object-fit: cover; }
    .person-name { color: #345b4c; font-size: 14px; font-weight: 700; }
    .person-count { color: #7d8a80; font-size: 12px; text-align: right; }
    .person-bar { height: 5px; margin-top: 7px; overflow: hidden; border-radius: 99px; background: #d9e2da; }
    .person-bar i { display: block; height: 100%; border-radius: inherit; background: #c57947; }
    .daily-chart { position: relative; min-height: 420px; padding-top: 18px; }
    /* 运行时 Canvas 对中文字体支持不稳定，趋势图的说明文字统一放在 HTML 层。 */
    .daily-chart-meta { position: absolute; z-index: 2; top: 18px; left: 66px; right: 66px; display: flex; align-items: center; justify-content: space-between; color: #6d7f78; font-size: 12px; line-height: 1; pointer-events: none; }
    .daily-chart-key { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
    .daily-chart-key i { display: inline-block; width: 12px; height: 10px; border-radius: 3px; background: #2d7f6c; }
    .daily-chart-key.ratio { color: #1d579d; }
    .daily-chart-key.ratio i { width: 12px; height: 4px; border-radius: 99px; background: #1d579d; }
    .daily-chart-key.ratio i::after { display: block; width: 7px; height: 7px; margin: -2px 0 0 3px; border: 2px solid #f7f3eb; border-radius: 50%; background: #1d579d; content: ""; }
    .daily-x-labels { position: absolute; z-index: 2; right: 24px; bottom: 16px; left: 66px; display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; text-align: center; pointer-events: none; }
    .daily-x-labels.has-ratio { right: 66px; }
    .daily-x-label { display: flex; flex-direction: column; gap: 5px; color: #5d7067; font-size: 11px; line-height: 1.15; }
    .daily-x-label strong { color: #304943; font-size: 11px; font-weight: 700; }
    .daily-x-label small { color: #89958c; font-size: 10px; }
    .daily-chart > .viz-fallback { position: absolute; inset: 150px 0 auto; color: #89958c; font-size: 13px; text-align: center; }
    .daily-svg { width: 100%; height: 235px; }
    .axis-label { fill: #89958c; font-size: 11px; text-anchor: middle; }
    .chart-grid { stroke: #d8d2c8; stroke-width: 1; }
    .bar { fill: #4f9a83; }
    .bar-label { fill: #527060; font-size: 10px; text-anchor: middle; }
    .active-line { fill: none; stroke: #1e5594; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
    .active-dot { fill: #1e5594; }
    .chart-legend { display: flex; gap: 18px; justify-content: flex-end; color: #89958c; font-size: 11px; }
    .chart-key { display: inline-block; width: 10px; height: 10px; margin-right: 5px; border-radius: 2px; vertical-align: -1px; background: #86a995; }
    .chart-key.line { border-radius: 50%; background: #c57947; }
    .viz-fallback { color: #89958c; font-size: 13px; text-align: center; }
    .chart-container { position: relative; width: 100%; }
    .chart-container[data-chart-ready="true"] .viz-fallback { display: none; }
    #hot-word-cloud { width: 100%; height: 660px; }
    #hot-word-cloud canvas { display: block; width: 100% !important; height: 100% !important; }
    #contribution-chart { width: 320px; height: 250px; }
    #daily-activity-chart { width: 100%; height: 420px; }
    .footer { display: flex; justify-content: space-between; margin-top: 54px; padding-top: 18px; border-top: 1px solid #d7d0c4; color: #a09c91; font-size: 10px; letter-spacing: 1px; }
    """


def _hot_bubbles(report: MessageSummaryReport) -> str:
    if not report.topics:
        return '<div class="empty-note">本期没有可展示的结构化热点。</div>'
    return (
        '<div id="hot-word-cloud" class="hot-cloud chart-container" role="img" '
        'aria-label="群聊热点词云"><span class="viz-fallback">正在生成热点词云</span></div>'
    )


def _hot_legend(report: MessageSummaryReport) -> str:
    if not report.topics:
        return '<div class="legend-row">暂无热点</div>'
    rows = []
    for index, topic in enumerate(report.topics[:6]):
        rows.append(
            f'<div class="legend-row"><i class="legend-dot" style="background:{_PALETTE[index % len(_PALETTE)]}"></i><span>{_escape(topic.title)}<br><b>{topic.message_count}</b> 条消息</span></div>'
        )
    return "".join(rows)


def _topic_cards(report: MessageSummaryReport) -> str:
    topics = report.top_topics[:3]
    if not topics:
        return '<div class="empty-card">本期没有可展示的重点话题。</div>'
    cards = []
    for index, topic in enumerate(topics, start=1):
        chips = "".join(
            f'<span class="chip">{_escape(tag.label)}{_change_suffix(tag.change_pct)}</span>' for tag in topic.market_tags
        )
        quote = f'<p class="quote">“{_escape(topic.quotes[0])}”</p>' if topic.quotes else ""
        cards.append(
            f'<article class="topic-card"><div class="topic-rank">0{index}</div><div><h3>{_escape(topic.title)}{_avatar_stack(topic.participant_avatar_urls, topic.participant_names)}</h3>'
            f'<div class="topic-meta"><strong>{topic.message_count}</strong> 条相关消息 · {len(topic.participant_names)} 位群友参与</div>'
            f'<p>{_escape(topic.summary)}</p>{quote}<div class="chip-row">{chips}</div></div></article>'
        )
    return "".join(cards)


def _donut_chart(report: MessageSummaryReport) -> str:
    if not report.analytics.contributions:
        return '<div class="empty-card">暂无入站成员发言。</div>'
    return (
        '<div id="contribution-chart" class="donut-chart chart-container" role="img" '
        'aria-label="成员发言贡献"><span class="donut-center-value">'
        f'{report.analytics.message_count}</span><span class="donut-center-caption">条消息</span>'
        '<span class="viz-fallback">正在生成贡献图</span></div>'
    )


def _contribution_list(report: MessageSummaryReport) -> str:
    contributions = list(report.analytics.contributions[:5])
    total = report.analytics.message_count or 1
    remaining = max(0, total - sum(item.message_count for item in contributions))
    if remaining:
        first = contributions[0] if contributions else None
        contributions.append(type(first)("remaining", "其余", "", remaining, round(remaining / total * 100, 1)) if first else None)
    rows: list[str] = []
    for item in [value for value in contributions if value is not None]:
        initial = _escape((item.name or "成")[:1].upper())
        avatar = _avatar_markup(item.avatar_url, item.name, initial=initial)
        rows.append(
            f'<div class="person-row"><div>{avatar}</div><div><div class="person-name">{_escape(item.name)}</div><div class="person-bar"><i style="width:{min(100, max(0, item.share_percent)):.1f}%"></i></div></div><div class="person-count">{item.message_count} 条 · {item.share_percent:.1f}%</div></div>'
        )
    return "".join(rows) or '<div class="empty-card">暂无成员贡献数据。</div>'


def _daily_chart(report: MessageSummaryReport) -> str:
    if not report.analytics.daily_activity:
        return '<div class="empty-card">暂无每日统计数据。</div>'
    daily_items = tuple(report.analytics.daily_activity)
    has_ratios = any(item.active_ratio_percent is not None for item in daily_items)
    ratio_class = " has-ratio" if has_ratios else ""
    labels = []
    for item in daily_items:
        weekday, date_label = _daily_label(item.date_key)
        labels.append(
            f'<span class="daily-x-label"><strong>{_escape(weekday)}</strong>'
            f'<small>{_escape(date_label)}</small></span>'
        )
    meta = (
        '<div class="daily-chart-meta" aria-hidden="true">'
        '<span class="daily-chart-key"><i></i>消息量（条）</span>'
    )
    if has_ratios:
        meta += '<span class="daily-chart-key ratio"><i></i>活跃群友占比（%）</span>'
    meta += "</div>"
    return (
        f'<div class="daily-chart">{meta}<div id="daily-activity-chart" class="chart-container" '
        'role="img" aria-label="每日消息量与活跃比例"><span class="viz-fallback">'
        f'正在生成每日趋势</span></div><div class="daily-x-labels{ratio_class}" '
        f'aria-hidden="true">{"".join(labels)}</div></div>'
    )


def _intro_avatars(report: MessageSummaryReport) -> str:
    """把导语中的活跃成员头像作为信息流的一部分，复刻参考图的内嵌头像口径。"""

    avatars = list(report.analytics.contributions[:5])
    if not avatars:
        return ""
    return " " + " ".join(_avatar_markup(item.avatar_url, item.name) for item in avatars[:4])


def _avatar_stack(urls: tuple[str, ...], names: tuple[str, ...]) -> str:
    values = []
    for index, name in enumerate(names[:5]):
        url = urls[index] if index < len(urls) else ""
        values.append(_avatar_markup(url, name))
    return f'<span class="topic-avatars">{"".join(values)}</span>' if values else ""


def _avatar_markup(url: str, name: str, *, initial: str | None = None) -> str:
    """统一头像渲染：目录缺失时显示本地默认头像，避免写入不可信 URL。"""

    safe_name = _escape(name or "成员")
    # Kitesurf 页面不能依赖外部头像地址；报告进入截图前会把公网头像转成
    # data URI。直接渲染缓存报告或头像下载失败时，统一使用本地默认头像。
    if url.startswith("data:image/"):
        return f'<span class="avatar"><img src="{_escape(url)}" alt="{safe_name}" /></span>'
    fallback = _default_avatar_uri()
    if fallback:
        return f'<span class="avatar"><img src="{_escape(fallback)}" alt="{safe_name}" /></span>'
    return f'<span class="avatar" aria-label="{safe_name}">{_escape(initial or (name or "成")[:1])}</span>'


class _PublicAvatarOpener:
    """让头像下载复用平台公网地址校验，重定向的每一跳也会重新检查。"""

    def open(self, request, *, timeout):
        return open_public_url(request.full_url, timeout=timeout)


@lru_cache(maxsize=256)
def _avatar_data_uri(url: str) -> str:
    """把成员头像规范化为小尺寸 PNG，失败时返回本地默认头像。"""

    normalized_url = str(url or "").strip()
    if not normalized_url:
        return _default_avatar_uri()
    if normalized_url.startswith("data:image/"):
        return normalized_url
    try:
        response = PluginHttpClient(opener_factory=lambda _: _PublicAvatarOpener()).request(
            "GET",
            normalized_url,
            connect_timeout_seconds=2,
            total_timeout_seconds=3,
            max_response_bytes=MAX_AVATAR_BYTES,
            retry_count=0,
        )
        if not 200 <= response.status_code < 300:
            raise ValueError("头像下载失败")
        with Image.open(io.BytesIO(response.body)) as picture:
            if picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("头像格式不受支持")
            if picture.width * picture.height > MAX_AVATAR_PIXELS:
                raise ValueError("头像像素超过限制")
            picture.thumbnail((MAX_AVATAR_SIDE, MAX_AVATAR_SIDE))
            normalized = picture.convert("RGBA")
            normalized.info.clear()
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception as exc:
        _logger.warning("群总结头像加载失败，已使用默认头像", extra={"error_type": type(exc).__name__})
        return _default_avatar_uri()


@lru_cache(maxsize=1)
def _default_avatar_uri() -> str:
    """返回插件自带的默认头像，确保缺失资料时仍保持参考图的头像层级。"""

    try:
        with Image.open(_ASSET_DIR / "default_avatar.png") as picture:
            picture.thumbnail((DEFAULT_AVATAR_SIDE, DEFAULT_AVATAR_SIDE))
            normalized = picture.convert("RGBA")
            normalized.info.clear()
            output = io.BytesIO()
            normalized.save(output, format="PNG", optimize=True)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
    except OSError:
        return ""
    return "data:image/png;base64," + encoded


def _prepare_report_avatars(report: MessageSummaryReport) -> MessageSummaryReport:
    """截图前只准备页面可见头像，避免浏览器访问外部地址或撑大 HTML。"""

    analytics = replace(
        report.analytics,
        contributions=tuple(
            replace(item, avatar_url=_avatar_data_uri(item.avatar_url)) if index < 5 else item
            for index, item in enumerate(report.analytics.contributions)
        ),
    )
    # 话题云和右侧热点图例不显示头像；只处理前三张重点话题卡，
    # 并与贡献列表的可见上限一致，避免把未展示的公网头像全部内嵌到 HTML。
    topics = report.topics
    top_topics = tuple(
        replace(
            topic,
            participant_avatar_urls=tuple(_avatar_data_uri(url) for url in topic.participant_avatar_urls),
        )
        if index < 3 else topic
        for index, topic in enumerate(report.top_topics)
    )
    return replace(report, analytics=analytics, topics=topics, top_topics=top_topics)


def _change_suffix(value: float | None) -> str:
    if value is None:
        return ""
    return f" {value:+.2f}%"


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)
