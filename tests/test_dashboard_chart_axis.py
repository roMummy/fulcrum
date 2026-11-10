"""趋势图宏：Y 轴刻度、X 轴时间刻度和数值标注都必须出现在输出里。"""
from jinja2 import Environment, FileSystemLoader

_env = Environment(loader=FileSystemLoader("app/templates"))
_tpl = _env.from_string("{% from '_dashboard_chart.html' import trend_chart %}{{ trend_chart(chart, '测试') }}")


def _chart(values):
    peak = max(values)
    n = len(values)
    return {
        "max_value": peak,
        "points": " ".join(f"{i / max(n - 1, 1) * 100},{100 - v / peak * 100 if peak else 100}" for i, v in enumerate(values)),
        "items": [{"label": f"{i:02d}:00", "value": v} for i, v in enumerate(values)],
    }


def test_axis_ticks_and_labels_rendered():
    html = _tpl.render(chart=_chart([5, 324, 3, 88]))
    # Y 轴：最大值、中值、0；X 轴：4 个点全部标出
    assert "<span>324</span><span>162</span><span>0</span>" in html
    for label in ("00:00", "01:00", "02:00", "03:00"):
        assert f"<span>{label}</span>" in html
    # 最大值点与末点各有一个数值标注
    assert html.count("chart-point-label") == 2


def test_all_zero_chart_still_has_readable_scale():
    html = _tpl.render(chart=_chart([0, 0, 0]))
    assert "<span>1</span><span>0.5</span><span>0</span>" in html


def test_x_ticks_sampled_to_five_when_many_points():
    html = _tpl.render(chart=_chart(list(range(1, 25))))
    assert html.count('class="chart-xaxis"') == 1
    xaxis = html.split('class="chart-xaxis"')[1].split("</div>")[0]
    assert xaxis.count("<span>") == 5
