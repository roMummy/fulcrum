# 平台 SaaS 数据大盘 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把平台概览升级为支持时间切换、消息与用户趋势、租户采用漏斗、渠道健康、租户排行和订阅经营分析的 SaaS 数据大盘。

**Architecture:** 新增独立的跨租户只读聚合服务，平台路由只解析规范时间范围并渲染结果。趋势读取现有小时聚合表，唯一触发用户和租户排行执行有 30 天上限的消息查询；新增组合索引支撑平台分析，不建立旧统计兼容链路或第二套快照表。

**Tech Stack:** FastAPI、SQLAlchemy 2、Jinja2、Alembic、原生 CSS、Pytest、Playwright

---

### Task 1: 增加平台分析组合索引

**Files:**
- Modify: `app/models/entities.py`
- Modify: `app/models/saas.py`
- Create: `migrations/versions/20260827_0008_platform_analytics_indexes.py`
- Test: `tests/test_alembic_lifecycle.py`

**Step 1: 编写索引契约检查**

在 Alembic 生命周期测试中检查以下索引存在：

```python
expected_indexes = {
    "messages": "ix_messages_platform_analytics",
    "message_activity_hourly": "ix_message_activity_hourly_platform_analytics",
    "tenant_sessions": "ix_tenant_sessions_platform_presence",
    "channels": "ix_channels_platform_health",
    "billing_orders": "ix_billing_orders_platform_revenue",
    "tenant_subscriptions": "ix_tenant_subscriptions_platform_health",
    "bot_bindings": "ix_bot_bindings_platform_usage",
    "provider_nodes": "ix_provider_nodes_platform_presence",
}
```

**Step 2: 运行检查并确认失败**

Run: `.venv/bin/pytest tests/test_alembic_lifecycle.py::test_alembic_upgrades_empty_database_to_simplified_baseline -q`

Expected: FAIL，原因是新索引尚不存在。

**Step 3: 增加模型索引和迁移**

模型使用 `Index` 声明与数据库一致的组合索引。迁移 `20260827_0008` 只创建当前大盘需要的索引，`downgrade()` 按逆序删除，不修改业务字段和历史数据。

**Step 4: 验证迁移**

Run: `.venv/bin/pytest tests/test_alembic_lifecycle.py::test_alembic_upgrades_empty_database_to_simplified_baseline -q`

Expected: PASS

**Step 5: 提交**

```bash
git add app/models/entities.py app/models/saas.py migrations/versions/20260827_0008_platform_analytics_indexes.py tests/test_alembic_lifecycle.py
git commit -m "数据库：增加平台分析组合索引"
```

### Task 2: 实现平台聚合服务

**Files:**
- Create: `app/services/platform_analytics.py`
- Create: `tests/test_platform_analytics.py`

**Step 1: 编写失败测试**

测试构造两个租户及确定时间内外的数据，覆盖：

- `24h`、`7d`、`30d` 和非法范围归一化；
- 小时聚合消息总数、入站、出站和活跃租户；
- 入站 `sender_id` 按 `(tenant_id, sender_id)` 去重；
- 触发用户和活跃租户趋势；
- 通用账号绑定、在线渠道和渠道类型汇总；
- 租户排行、额度使用率和最近活跃时间；
- 订阅试用、付费、即将到期、失效、待审核订单和审核金额；
- 当前员工会话、执行客户端和渠道状态；
- 租户健康状态优先级。

**Step 2: 运行测试并确认失败**

Run: `.venv/bin/pytest tests/test_platform_analytics.py -q`

Expected: FAIL，原因是 `app.services.platform_analytics` 尚不存在。

**Step 3: 实现范围和时间桶**

在 `platform_analytics.py` 中定义：

```python
ANALYTICS_RANGES = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "day"),
    "30d": (timedelta(days=30), "day"),
}

def normalize_analytics_range(value: str | None) -> str:
    return value if value in ANALYTICS_RANGES else "7d"
```

所有公开函数书写详细中文注释，并允许测试传入固定 `now`。

**Step 4: 实现核心聚合**

实现单一入口：

```python
def build_platform_analytics(
    db: Session,
    *,
    range_key: str = "7d",
    now: datetime | None = None,
) -> dict[str, object]:
    ...
```

返回：`range`、`range_options`、`core_metrics`、`message_trend`、`activity_trend`、`adoption_funnel`、`channel_summary`、`tenant_rankings`、`tenant_health`、`subscription_summary`、`presence_summary`。

**Step 5: 运行服务测试**

Run: `.venv/bin/pytest tests/test_platform_analytics.py -q`

Expected: PASS

**Step 6: 提交**

```bash
git add app/services/platform_analytics.py tests/test_platform_analytics.py
git commit -m "分析：实现平台SaaS聚合服务"
```

### Task 3: 升级平台概览页面

**Files:**
- Modify: `app/web/platform_admin.py`
- Modify: `app/templates/platform_dashboard.html`
- Modify: `app/static/css/style.css`
- Modify: `tests/test_platform_admin.py`
- Modify: `tests/test_scandinavian_ui.py`

**Step 1: 编写失败页面测试**

平台页面测试需要确认：

- `/platform/dashboard?range=24h` 把规范范围传给聚合服务；
- 非法范围收敛为 `7d`；
- 页面包含时间分段控件、核心指标、消息趋势、活跃趋势、使用漏斗、渠道运行、租户排行、租户健康、订阅经营和在线状态；
- 页面不出现消息正文、`sender_id`、设备 ID 或 Provider 专属统计文案。

界面契约检查需要确认大盘继续使用单层工作表面、稳定图表尺寸和移动端单列布局。

**Step 2: 运行页面测试并确认失败**

Run: `.venv/bin/pytest tests/test_platform_admin.py tests/test_scandinavian_ui.py -q`

Expected: FAIL，原因是平台路由和模板仍只渲染四项统计。

**Step 3: 接入聚合服务**

平台路由读取 `request.query_params.get("range")`，调用 `build_platform_analytics()`，并把单一 `analytics` 结构传入模板。删除原 `dashboard_stats()` 调用和模板 `stats` 数据路径，不保留双轨统计。

**Step 4: 重构平台模板**

模板使用：

- `analytics-range-control` 分段时间控件；
- `platform-analytics-kpis` 连续指标带；
- 两个 `platform-analytics-chart` 趋势表面；
- `platform-adoption-funnel` 使用漏斗；
- 渠道、排行、健康、订阅和在线状态的表格或紧凑列表。

图表 SVG 使用服务端生成的点位，轴标签使用首、中、末三个时间点。页面不添加研发说明文案。

**Step 5: 增加响应式样式**

桌面使用明确网格轨道和固定图表纵横约束；移动端切换单列，表格在自身容器滚动，页面不得横向溢出。所有新增样式增加详细中文注释。

**Step 6: 运行页面定向测试**

Run: `.venv/bin/pytest tests/test_platform_admin.py tests/test_scandinavian_ui.py -q`

Expected: PASS

**Step 7: 提交**

```bash
git add app/web/platform_admin.py app/templates/platform_dashboard.html app/static/css/style.css tests/test_platform_admin.py tests/test_scandinavian_ui.py
git commit -m "界面：上线平台SaaS数据大盘"
```

### Task 4: 记录、验证和部署

**Files:**
- Modify: `CHANGELOG.md`

**Step 1: 记录实际变更和验证**

记录平台大盘指标、组合索引、实际执行的定向测试和浏览器视口，不表述为全量回归。

**Step 2: 运行轻量验证**

Run:

```bash
.venv/bin/pytest tests/test_platform_analytics.py tests/test_platform_admin.py tests/test_scandinavian_ui.py tests/test_alembic_lifecycle.py -q
.venv/bin/python -m py_compile app/services/platform_analytics.py app/web/platform_admin.py
git diff --check
```

Expected: 全部通过。

**Step 3: 使用真实浏览器检查**

检查 `1536x864` 和 `390x844` 下的平台大盘：

- 三个时间范围可访问；
- 图表非空白且尺寸稳定；
- 表格和状态列表不遮挡；
- 移动端没有整页横向溢出。

**Step 4: 提交变更记录**

```bash
git add CHANGELOG.md docs/plans/2026-08-27-platform-saas-analytics-dashboard-implementation.md
git commit -m "发布：记录平台数据大盘验证结果"
```

**Step 5: 推送并部署**

按最新 AGENTS.md 通过 SSH 登录 `192.168.2.142`，在远程工作区执行 `git pull`、应用镜像构建、`alembic upgrade head` 和应用容器差异重建。数据库迁移成功前不得替换旧应用。

**Step 6: 验证正式环境**

确认远端提交、数据库版本、容器健康、`/healthz`、`/readyz`、平台登录边界和真实平台大盘桌面/移动视口。
