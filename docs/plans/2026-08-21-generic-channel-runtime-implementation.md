# 曜枢 AI 通用渠道运行 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 恢复企业微信、微信 iPad、Windows 微信和 Telegram 的租户渠道入口及真实运行链路，并使用统一渠道目录和租户隔离协调器替代企微硬编码。

**Architecture:** 新增集中式渠道目录，页面、Pydantic 校验、适配器注册和后台运行都从目录读取渠道类型与能力。后台主动任务先枚举租户，再进入可信租户上下文调用现有 Telegram 和微信协议服务；企业微信执行端仍保留为 `workwechat` 的专项远端执行方式。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.x、Jinja2、Pytest、MariaDB/SQLite。

---

### Task 1: 建立统一渠道目录和适配器契约

**Files:**
- Create: `app/services/channel_catalog.py`
- Modify: `app/services/channel_labels.py`
- Modify: `app/services/channel_adapters/base.py`
- Modify: `app/services/channel_adapters/registry.py`
- Modify: `app/services/channel_adapters/generic.py`
- Modify: `app/services/channel_adapters/simulator.py`
- Modify: `app/services/channel_adapters/telegram.py`
- Modify: `app/services/channel_adapters/wechat_ipad.py`
- Modify: `app/services/channel_adapters/wechat_winauto.py`
- Modify: `app/services/channel_adapters/workwechat.py`
- Modify: `app/services/channel_adapters/service.py`
- Modify: `app/schemas/forms.py`
- Modify: `app/services/open_api_apps.py`
- Create: `tests/test_channel_catalog.py`
- Delete: `tests/test_workwechat_only_scope.py`

**Step 1: 编写失败测试**

测试必须断言：

```python
def test_public_channel_catalog_contains_real_supported_channels():
    assert list_supported_channel_types() == (
        "workwechat",
        "wechat_ipad",
        "wechat_winauto",
        "telegram",
    )


def test_unknown_channel_type_is_rejected_without_generic_fallback():
    with pytest.raises(ChannelTypeNotSupported, match="channel_type_not_supported"):
        get_channel_adapter(Channel(channel_type="unknown"))


@pytest.mark.parametrize("channel_type", list_supported_channel_types())
def test_channel_schema_accepts_supported_channel_types(channel_type):
    assert ChannelCreate(bot_id=1, name="渠道", channel_type=channel_type).channel_type == channel_type
```

同时断言适配器标准结果使用 `adapter_type`，不再输出通用业务字段 `provider`。

**Step 2: 运行测试确认失败**

Run: `pytest -q tests/test_channel_catalog.py`

Expected: FAIL，原因是渠道目录和异常类型尚未实现。

**Step 3: 实现渠道目录**

在 `channel_catalog.py` 定义不可变结构：

```python
@dataclass(frozen=True)
class ChannelConfigField:
    name: str
    label: str
    input_type: str = "text"
    placeholder: str = ""
    required_when_enabled: bool = False
    secret: bool = False


@dataclass(frozen=True)
class ChannelTypeDefinition:
    code: str
    label: str
    summary: str
    runtime_mode: str
    capabilities: frozenset[str]
    config_fields: tuple[ChannelConfigField, ...]
    adapter_factory: Callable[[], BaseChannelAdapter]
    requires_executor_binding: bool = False
```

只注册 `workwechat`、`wechat_ipad`、`wechat_winauto` 和 `telegram`。提供：

- `list_channel_type_definitions()`
- `list_supported_channel_types()`
- `get_channel_type_definition()`
- `require_supported_channel_type()`
- `channel_supports()`

删除 `SAAS_ENABLED_CHANNEL_TYPES` 和 `require_saas_enabled_channel_type()`。`channel_labels.py` 只从目录生成显示名称，不再维护第二份渠道清单。

**Step 4: 收紧适配器注册和标准结果**

`registry.py` 通过渠道目录创建适配器，未知类型抛出 `ChannelTypeNotSupported`。删除未知类型自动返回 `GenericHttpAdapter` 的降级路径。

把通用适配器结果中的：

```python
{"provider": self.provider}
```

统一替换为：

```python
{"adapter_type": self.adapter_type}
```

具体适配器只设置 `adapter_type`。日志读取标准字段时同步改为 `adapter_type`。

**Step 5: 更新输入模型和开放接口校验**

`ChannelCreate` 和 `ChannelUpdate` 使用 `require_supported_channel_type()`。开放接口应用允许授权所有正式渠道，仍从可信租户会话校验渠道归属。

**Step 6: 运行定向测试**

Run: `pytest -q tests/test_channel_catalog.py tests/test_open_api_apps.py tests/test_open_api.py`

Expected: PASS。

**Step 7: 提交**

```bash
git add app/services/channel_catalog.py app/services/channel_labels.py app/services/channel_adapters app/schemas/forms.py app/services/open_api_apps.py tests/test_channel_catalog.py tests/test_workwechat_only_scope.py
git commit -m "渠道：建立统一类型目录和适配器契约"
```

---

### Task 2: 恢复多渠道创建编辑入口

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/channels.html`
- Modify: `app/templates/channel_detail.html`
- Modify: `app/services/provider_nodes.py`
- Modify: `tests/test_provider_node_pages.py`
- Create: `tests/test_multi_channel_pages.py`

**Step 1: 编写失败页面测试**

测试四种正式渠道均出现在类型选择器，并分别提交最小配置：

```python
@pytest.mark.parametrize("channel_type", ("wechat_ipad", "wechat_winauto", "telegram"))
def test_tenant_admin_can_create_direct_channel_without_executor_binding(...):
    response = client.post(
        "/channels",
        data={
            "bot_id": bot_id,
            "name": channel_type,
            "channel_type": channel_type,
            "api_base": "http://127.0.0.1:9999" if channel_type != "telegram" else "",
            "bot_token": "telegram-token" if channel_type == "telegram" else "",
            "csrf_token": csrf_token,
            "is_enabled": "false",
        },
    )
    assert response.status_code == 303
```

另行断言只有 `workwechat` 要求执行端节点和账号，非企微渠道提交这些字段时不会形成绑定。

**Step 2: 运行测试确认失败**

Run: `pytest -q tests/test_multi_channel_pages.py tests/test_provider_node_pages.py`

Expected: FAIL，页面仍固定提交 `workwechat`。

**Step 3: 让列表和详情读取渠道目录**

- `/channels` 展示当前租户全部正式渠道。
- `/channels/{id}` 允许访问目录中注册的渠道。
- 模板上下文传入序列化后的渠道定义。
- 创建表单使用类型选择器和 `data-channel-types` 控制字段可见性。
- 编辑表单显示固定渠道类型，不允许修改 `channel_type`。

**Step 4: 抽象执行端绑定校验**

将 `validate_workwechat_provider_binding()` 改为 `validate_channel_executor_binding()`：

- 渠道定义要求执行端绑定时，执行现有租户、节点、账号、在线状态和唯一占用校验。
- 渠道定义不要求执行端绑定时，返回 `(None, None)`，并确保不会保存提交的节点和账号主键。

删除旧函数名和旧测试引用，不保留别名。

**Step 5: 服务端校验启用配置**

根据渠道定义检查启用状态下的必填字段。缺少配置时返回明确提示；停用渠道允许先保存配置草稿。

**Step 6: 运行定向测试**

Run: `pytest -q tests/test_channel_catalog.py tests/test_multi_channel_pages.py tests/test_provider_node_pages.py`

Expected: PASS。

**Step 7: 提交**

```bash
git add app/main.py app/templates/channels.html app/templates/channel_detail.html app/services/provider_nodes.py tests/test_provider_node_pages.py tests/test_multi_channel_pages.py
git commit -m "渠道：恢复微信与Telegram租户入口"
```

---

### Task 3: 通用化企业首页和公开描述

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/marketing_home.html`
- Modify: `tests/test_public_marketing_pages.py`
- Create: `tests/test_generic_channel_dashboard.py`

**Step 1: 编写失败测试**

断言企业首页：

- 包含“在线渠道”和“渠道”。
- 不包含“在线企业微信账号”或机器人表头“企业微信账号”。
- 风险提示依据所有启用渠道状态计算。

断言公开首页明确包含“微信、企业微信、Telegram”，且不把其中任一渠道写成唯一主线。

**Step 2: 运行测试确认失败**

Run: `pytest -q tests/test_generic_channel_dashboard.py tests/test_public_marketing_pages.py`

Expected: FAIL，当前企业首页仍含企微专属指标。

**Step 3: 修改查询与模板**

将 `online_workwechat` 改为统计全部 `status == "online"` 且已启用的正式渠道。机器人矩阵继续统计绑定渠道数量，但表头使用“渠道”。风险提示只描述在线渠道和异常渠道。

公开首页能力说明改为：

```text
支持微信、企业微信、Telegram 等渠道，通过统一适配能力管理机器人账号和运行状态。
```

**Step 4: 运行定向测试并提交**

Run: `pytest -q tests/test_generic_channel_dashboard.py tests/test_public_marketing_pages.py`

Expected: PASS。

```bash
git add app/main.py app/templates/dashboard.html app/templates/marketing_home.html tests/test_generic_channel_dashboard.py tests/test_public_marketing_pages.py
git commit -m "界面：统一全渠道首页与产品描述"
```

---

### Task 4: 实现租户隔离渠道运行协调器

**Files:**
- Create: `app/services/channel_runtime.py`
- Modify: `app/services/async_workers.py`
- Modify: `app/services/telegram_polling.py`
- Modify: `app/services/wechat_ipad/service.py`
- Create: `tests/test_channel_runtime.py`

**Step 1: 编写租户隔离失败测试**

建立两个租户及各自渠道，替换协议调用函数记录当前租户：

```python
def test_runtime_coordinator_executes_each_channel_inside_its_tenant_scope(...):
    executed = []

    def fake_runner(db, channel):
        executed.append((current_tenant_id(), channel.tenant_id, channel.channel_type))
        return {"worked": True}

    result = run_due_channel_tasks(now_monotonic=10.0, runners={"telegram_poll": fake_runner})

    assert executed == [(tenant_a, tenant_a, "telegram"), (tenant_b, tenant_b, "telegram")]
    assert result.worked is True
```

同时测试一个渠道失败不会阻止另一个租户执行，且企业微信不会进入平台轮询。

**Step 2: 运行测试确认失败**

Run: `pytest -q tests/test_channel_runtime.py`

Expected: FAIL，协调器尚未实现。

**Step 3: 实现协调器**

`channel_runtime.py` 负责：

- 使用平台会话读取租户主键。
- 对每个租户进入 `tenant_scope` 与 `tenant_session`。
- 查询当前租户中已启用的 `platform_polling` 渠道。
- 根据能力调用 Telegram 拉取、微信 iPad 消息同步、心跳和自动恢复。
- 以 `(tenant_id, channel_id, task_name)` 保存进程内到期时间，避免不同租户的相同主键互相覆盖。
- 捕获单渠道异常并记录租户、渠道和任务信息。

**Step 4: 删除旧全局轮询函数**

从 `async_workers.py` 删除会自行打开 `SessionLocal()` 的 `_run_due_channel_pollers()`、`_run_due_wechat_ipad_message_sync()`、`_run_due_wechat_ipad_heartbeats()` 和 `_run_due_wechat_ipad_auto_recover()`。Worker 主循环只调用新的 `run_due_channel_tasks()`。

插件定时任务不属于渠道适配恢复范围，本轮不重新启用旧的全局插件轮询器。

**Step 5: 运行定向测试**

Run: `pytest -q tests/test_channel_runtime.py tests/test_channel_catalog.py`

Expected: PASS。

**Step 6: 提交**

```bash
git add app/services/channel_runtime.py app/services/async_workers.py app/services/telegram_polling.py app/services/wechat_ipad/service.py tests/test_channel_runtime.py
git commit -m "运行时：恢复租户隔离的多渠道调度"
```

---

### Task 5: 收尾检查和研发记录

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Step 1: 更新文档**

README 列出四种正式渠道和各自运行方式，删除“当前只有企业微信执行端可运行”的误导性描述。CHANGELOG 记录渠道入口、运行调度和仪表盘通用化。

**Step 2: 执行轻量验证**

Run:

```bash
pytest -q tests/test_channel_catalog.py tests/test_multi_channel_pages.py tests/test_provider_node_pages.py tests/test_generic_channel_dashboard.py tests/test_public_marketing_pages.py tests/test_channel_runtime.py
python -m py_compile app/services/channel_catalog.py app/services/channel_runtime.py app/services/async_workers.py app/main.py
git diff --check
```

Expected: 所有定向测试通过，语法检查和差异检查无输出。

**Step 3: 提交**

```bash
git add README.md CHANGELOG.md
git commit -m "文档：更新曜枢AI多渠道运行说明"
```

## 交付边界

- 不执行完整历史测试或完整 Playwright。
- 不请求真实 Telegram、微信或企业微信接口。
- 不修改或提交 `.rcodex/`。
- 不修改上层 `multi-bot-admin` 的未提交文件。
- 完成后报告实际测试结果和未验证的真实渠道联调项。
