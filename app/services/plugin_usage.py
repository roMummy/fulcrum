"""插件卡片批量统计：只读取可信范围的计数和时间，不返回执行正文或密钥。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, hour_bucket_shanghai, now_shanghai
from app.models.entities import PluginActivityHourly, PluginExecutionLog


PERIOD_DAYS = {'24h': 1, '7d': 7, '30d': 30}
METRICS = ('matched', 'success', 'error', 'skipped', 'blocked')


def build_plugin_usage(db: Session, plugin_codes: Iterable[str], *, period: str = '24h', now: datetime | None = None) -> dict:
    """两次聚合查询获取全部插件，时间桶补零与标签统一采用服务端业务时区。

    24h 展示包含当前小时的 24 个桶；7d／30d 包含今天，按自然日聚合。
    最近调用仅使用所选时间范围内真实 attempted 日志；排序用的最近活动不受
    图表时间范围影响，只计算实际命中或调用，未命中扫描不更新时间。
    企业调用方传入租户会话和可见插件，平台调用方必须先验证平台身份。
    """

    if period not in PERIOD_DAYS:
        raise ValueError('不支持的统计时间范围')
    current = ensure_shanghai_datetime(now or now_shanghai())
    current_hour = hour_bucket_shanghai(current)
    hourly = period == '24h'
    count = 24 if hourly else PERIOD_DAYS[period]
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    end_bucket = current_hour if hourly else current_hour.replace(hour=0)
    start = end_bucket - step * (count - 1)
    times = [start + step * index for index in range(count)]
    keys = [value.strftime('%Y-%m-%d %H:00:00' if hourly else '%Y-%m-%d') for value in times]
    indexes = {key: index for index, key in enumerate(keys)}
    codes = tuple(dict.fromkeys(plugin_codes))
    plugins = {code: {'totals': dict.fromkeys(METRICS, 0),
        'series': [dict.fromkeys(METRICS, 0) for _ in times], 'last_called_at': None,
        'last_called_label': '本期暂无调用', 'last_activity_at': None} for code in codes}
    payload = {'period': period, 'grain': 'hour' if hourly else 'day',
        'generated_at': current.isoformat(), 'generated_label': current.strftime('%H:%M:%S'),
        'buckets': [{'label': value.strftime('%H:%M' if hourly else '%m-%d'),
            'time': value.strftime('%m-%d %H:00') if hourly else value.strftime('%Y-%m-%d'),
            'start': value.isoformat()} for value in times], 'plugins': plugins}
    if not codes:
        return payload
    bucket = PluginActivityHourly.bucket_start if hourly else func.date(PluginActivityHourly.bucket_start)
    # 在数据库内完成按插件和时间聚合，避免把每个机器人／渠道的小时行逐一加载。
    rows = db.execute(select(bucket.label('bucket'), PluginActivityHourly.plugin_code,
        func.sum(PluginActivityHourly.matched_count), func.sum(PluginActivityHourly.success_count),
        func.sum(PluginActivityHourly.failed_count + PluginActivityHourly.timeout_count),
        func.sum(PluginActivityHourly.skipped_count), func.sum(PluginActivityHourly.blocked_count),
    ).where(PluginActivityHourly.plugin_code.in_(codes), PluginActivityHourly.bucket_start >= start,
        PluginActivityHourly.bucket_start <= current_hour).group_by(bucket, PluginActivityHourly.plugin_code)).all()
    for row in rows:
        key = ensure_shanghai_datetime(row[0]).strftime('%Y-%m-%d %H:00:00') if hourly else str(row[0])
        if key not in indexes:
            continue
        item = plugins[row[1]]
        for name, value in zip(METRICS, row[2:]):
            amount = max(int(value or 0), 0)
            item['series'][indexes[key]][name] += amount
            item['totals'][name] += amount
    # 一次日志聚合同时返回本期最近调用与历史最近活动，避免为每张卡片另查数据库。
    # 未命中扫描不能改变卡片顺序；命中后被规则拦截仍属于真实活动。
    latest = db.execute(select(PluginExecutionLog.plugin_code,
        func.max(case((and_(PluginExecutionLog.attempted.is_(True), PluginExecutionLog.created_at >= start),
            PluginExecutionLog.created_at))), func.max(PluginExecutionLog.created_at))
        .where(PluginExecutionLog.plugin_code.in_(codes), PluginExecutionLog.created_at <= current,
            or_(PluginExecutionLog.matched.is_(True), PluginExecutionLog.attempted.is_(True)))
        .group_by(PluginExecutionLog.plugin_code)).all()
    for code, timestamp, activity in latest:
        if activity:
            plugins[code]['last_activity_at'] = ensure_shanghai_datetime(activity).isoformat()
        if timestamp:
            value = ensure_shanghai_datetime(timestamp)
            plugins[code]['last_called_at'] = value.isoformat()
            plugins[code]['last_called_label'] = value.strftime('%m-%d %H:%M:%S')
    return payload
