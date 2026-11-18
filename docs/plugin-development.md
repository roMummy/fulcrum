# Fulcrum plugin development

This is the reference for directory plugins, their settings and business UI.
Read [the shared development contract](../AGETNS-DEV.md) for project-wide rules
and [the UI guidelines](ui/soft-ui-guidelines.md) before changing templates.

## Package and starting point

Copy [hello_plugin](plugin-template/hello_plugin), or download its installable ZIP
from the signed-in documentation center at `/developer/docs/plugins`. Rename the
directory and manifest code, then keep only the capabilities your plugin needs.

```text
plugins/example_plugin/
  config.toml
  plugin.py
  service.py        # Optional business helpers
  assets/           # Optional licensed resources
  README.md         # English usage and limitations
```

The directory and `[plugin].code` must match `[a-z][a-z0-9_]{1,63}`. Directories
starting with `_` are shared code, not plugins. Use project services or your own
helpers; do not import another plugin's internals or historical `base`, `db` and
`utils` packages. Follow the helper import rules in
[`verify_plugin_migration.py`](../scripts/verify_plugin_migration.py).

Shipped plugins must be added to that script's approved catalog and corresponding
expectations. Independently installed valid ZIPs need not be in the shipped set.
Do not edit the dispatcher merely to register a directory plugin.

## Manifest

[`plugin_manifest.py`](../app/services/plugin_manifest.py) parses TOML declarations.
Invalid manifests are reported on their own catalog entries. A minimal manifest:

```toml
[plugin]
code = "example_plugin"
display_name = "示例回复"
plugin_type = "command"
version = "1.0.0"
author = "Example Developer"
description = "按配置回复问候"
default_enabled = false

[capability]
channel_types = ["wechat_webridge", "telegram"]
chat_types = ["direct", "group"]
message_types = ["text"]
priority = 90
consume_policy = "continue"
timeout_ms = 10000

[matcher]
commands = ["hello"]
keywords = []
require_mention = false

[runtime]
greeting = "你好"

[[config.fields]]
key = "greeting"
label = "问候语"
kind = "text"
default = "你好"
group = "回复"
```

Lower priority runs first. Use `continue` by default, or `stop` when successful
execution must consume the event. Advertise only supported channel/message
combinations and choose a bounded timeout. Editable `commands`, `keywords` and
`require_mention` fields use those exact keys and match the matcher defaults.

Optional `[ai]` metadata uses `routable`, `risk`, `summary`, `examples` and
`needs_args`. Only low-risk routable actions qualify for direct AI dispatch;
declaring metadata does not replace authorization or action validation.

### Configuration and secrets

Declare every editable setting in `[[config.fields]]`; ordinary configuration
uses the shared form, not raw JSON editors or plugin-specific parsers.

| Kind | Value / additional declaration |
| --- | --- |
| `text`, `textarea`, `url` | String; use examples in `placeholder` where helpful |
| `number` | Numeric default; appropriate `min` and `max` |
| `bool` | Boolean default |
| `select` | `options` with strings or `{ value = "fast", label = "快速" }`; default must be an option |
| `tags` | List of strings |
| `time` | `HH:MM` |
| `secret` | Encrypted binding value; no default |

Keys are unique, start with a lowercase letter and use lowercase letters, digits and
underscores, at most 64 characters. Supply a Chinese `label`, appropriate kind,
default and group. Add hints only when they explain a meaningful constraint.
`depends_on` must reference a boolean field; it controls presentation, not
server-side validation. `label_en` is reserved metadata, not an English UI.

The platform assembles ordinary settings in this order:

```text
manifest defaults -> robot binding settings -> applicable group overrides
```

Decrypted binding secrets are merged separately. Read `request.config`; do not
rebuild merging inside plugins. Group overrides cannot store secrets or override
binding identity. Keep shared reset/default behavior through
[`plugin_schemas.py`](../app/services/plugin_schemas.py) and
[`group_plugin_settings.py`](../app/services/group_plugin_settings.py).

Use `kind = "secret"` without a default. If also declaring `[secrets].fields`,
the delivery checker requires explicitly empty matching `[runtime]` values.
An empty secret input preserves the stored value; clearing uses the explicit
clear control. Reuse [`plugin_secrets.py`](../app/services/plugin_secrets.py).
Never echo secrets into HTML, JSON, logs, task payloads or state records.
Validate persisted types, bounds and ownership again when executing. Counters
and large records belong in state/business tables, not binding configuration.

## Execution and replies

Read [`contracts.py`](../app/services/plugin_runtime/contracts.py),
[`executors.py`](../app/services/plugin_runtime/executors.py) and
[`service.py`](../app/services/plugin_runtime/service.py).

| Module-level entry point | Responsibility |
| --- | --- |
| `execute_action(request)` | Stateless action and explicit `reload` health handling |
| `execute_message_action(db, request)` | Message work using the supplied tenant session |
| `execute_scheduled_action(db, request)` | Declared scheduled actions |
| `matches_event(context)` | Cheap, side-effect-free custom matching |
| `handle_event(context)` | Directory observer behavior |

Message work prefers `execute_message_action` when present, otherwise
`execute_action`. Shipped catalog checks require one of these execution entries;
observer support alone does not satisfy them. Handle unknown actions explicitly.
Reloading must not send messages, charge points or create business records.

`request` contains `action`, merged `config`, business `payload` and trusted
`PluginExecutionContext`. Common payload keys include `command_name`,
`command_args`, `content`, `matched_keyword` and `conversation_external_id`.
Plugins with separately configured command groups may list their runtime keys
in `[matcher].additional_command_fields`. The dispatcher and group settings
combine these lists with `commands` before applying group aliases.
Authority comes from context, never payload. Custom matchers instead receive
`DirectoryPluginContext`, including message properties and `binding_config`;
read its actual definition before using fields.

```python
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.plugin_runtime.replies import text_reply

def execute_action(request):
    # 重载只检查入口，不发送消息或产生业务记录。
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="ready", message="插件已就绪")
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message="不支持此操作")
    greeting = request.config.get("greeting", "你好")
    if not isinstance(greeting, str) or not greeting.strip():
        return PluginExecutionResult(ok=False, status="invalid_config", message="请配置问候语")
    return PluginExecutionResult(
        ok=True, status="completed", message="回复已生成",
        output={"replies": [text_reply(greeting)]},
    )
```

Use `output["reply_text"]` or `output["replies"]`. Helpers in
[`replies.py`](../app/services/plugin_runtime/replies.py) include `text_reply`,
`image_reply`, `file_reply`, `link_reply`, `video_reply` and `music_reply`.
Top-level `message` is execution feedback, not a chat reply. Let the platform
choose channel delivery; do not construct protocol XML or call channel clients.
Distinguish missing context/configuration, invalid input, empty results,
upstream failure and timeout; failed side effects must not report success.

## Common services and tenant boundaries

Use the platform tenant session and trusted context for business data. New
tenant-owned models inherit `TenantOwnedMixin`; use Alembic for schema changes.
Helpers follow the caller's transaction boundary and do not close its session.

| Need | Shared facility |
| --- | --- |
| Bounded HTTP | [`PluginHttpClient`](../app/services/plugin_runtime/http_client.py) |
| Small JSON state | [`PluginStateStore`](../app/services/plugin_runtime/state_store.py): tenant + binding, namespace/key, 64 KiB per record, optional expiry |
| Rate limits | [`PluginRateLimiter`](../app/services/plugin_runtime/rate_limit.py): appropriate user/conversation scope |
| Conversation history | [`PluginMessageReader`](../app/services/plugin_runtime/messages.py) |
| Long-term AI memory | [`ai_memory.py`](../app/services/ai_memory.py), existing chat/digest integration; see [Hindsight contract](architecture.md#hindsight) |
| Web/HTML and Markdown screenshots | [`web_screenshots.py`](../app/services/web_screenshots.py), [`markdown_to_image.py`](../app/core/markdown_to_image.py); see [Kitesurf contract](architecture.md#kitesurf) |
| Text generation | [`PluginTextGenerationClient`](../app/services/plugin_runtime/text_generation.py) |
| Member display names | [`member_names`](../app/services/plugin_runtime/member_names.py) |
| Authorized media/resources | [`media.py`](../plugins/_shared/media.py), [`resources.py`](../plugins/_shared/resources.py) |
| Proactive delivery | [`enqueue_outbound_job`](../app/services/outbound_batch.py) |

`PluginHttpClient` bounds transport and validates URL syntax; it is not a complete
SSRF barrier. Untrusted public-resource URLs also need the applicable validation
from [`safe_fetch.py`](../app/services/safe_fetch.py) and media/screenshot services.
Do not bypass those boundaries with direct HTTP or arbitrary filesystem access.

### Outbound proxy

Each plugin binding has a `direct`/`proxy` mode on the plugin configuration page;
`direct` is the default. Proxy mode uses `proxy_url`, `proxy_username`,
`proxy_bypass` (host suffixes, comma separated) and the encrypted secret
`proxy_password`. The address and credential fields are only shown when proxy
mode is selected. Supported schemes are `http://`, `socks5://` and `socks5h://`;
HTTP proxy URLs for HTTPS targets use CONNECT.

A group's plugin settings have the same two modes: `group_proxy_mode` is
`direct` or `proxy`. When a group has no saved proxy setting it follows the
binding proxy (and the page shows proxy mode when the binding has one); saving
`direct` bypasses the binding proxy for that group. Proxy groups use
`group_proxy_url` and `group_proxy_bypass` only; group-level proxies do not store
credentials. Authenticated proxies belong on the binding and can be followed by
groups. Group settings take precedence once saved. Legacy stored values `custom`
and `inherit` are read as `proxy` and “follow the binding proxy”.

The runtime applies the effective proxy to every `PluginHttpClient` request in
that binding/conversation, including redirects that are not bypassed, and removes
all `proxy_*` and `group_proxy_*` keys before the plugin sees `request.config`.
Plugins must not implement their own proxy handling or read environment proxy
variables.

Store stable member IDs and resolve names when rendering with
`member_names(db, channel_id=..., group_external_id=..., user_ids=[...])`.
For large groups, split state by member instead of exceeding a single state record.
Never derive authority from cached display names or another tenant's credentials.

### Web and HTML screenshots

[`capture_screenshot`](../app/services/web_screenshots.py) is the production entry
point. Plugins must not import browser automation libraries, launch browsers or
declare engine/CDP/browser-path settings. Static checks cover helper modules;
they do not create an operating-system sandbox.

```python
from app.services.web_screenshots import ScreenshotRequest, capture_screenshot

# 真实插件入口已建立可信租户作用域；返回标准图片回复交给平台发送。
image = capture_screenshot(ScreenshotRequest(html="<h1>统计日报</h1>"))
output = {"replies": [image.as_image_reply()]}
```

Provide exactly one of `url` and `html`. Each call uses a separate browser context
with no shared cookies. Catch `ScreenshotError` and return its safe code/message;
do not silently switch engines. Async callers use the existing threadpool helper.

HTML is limited to 1 MiB; width to 320–2048, viewport height to 200–4096, full-page
height to 8192, pixels to 16M and output to 12 MB. `wait_ms` is 0–3000 within the
total timeout. Oversized full-page captures fail instead of silently clipping.
Use available fonts and accessible public/data resources; relative paths do not
refer to the Fulcrum filesystem.

The platform configures `KITESURF_WS_URL`, `KITESURF_TIMEOUT_SECONDS` and
`KITESURF_MAX_CONCURRENCY`. The HTTP tool endpoint `POST /api/tool/screenshots`
uses `X-API-Key` and `X-Tenant-Code`, accepts screenshot options and returns image
bytes with `X-Screenshot-Width`, `X-Screenshot-Height` and
`X-Screenshot-Elapsed-Ms`. It does not send a message. Typical errors are
`screenshot_not_configured` (503), `screenshot_busy` (429), `screenshot_timeout`
(504), `screenshot_too_large` (413), `screenshot_url_invalid` (400),
`screenshot_engine_error` (502) and invalid options (422). See
[engine deployment](../deploy/kitesurf/README.md) for operator setup.

## Scheduling and proactive sends

Declare periodic work in the manifest; do not add cron loops, threads or branches
to the older per-plugin scheduler list.

```toml
[[schedule]]
action = "daily_push"
kind = "daily"
time_key = "send_time"
default_time = "20:00"
enabled_key = "scheduled_enabled"
priority = 70
high_side_effect = true
grace_seconds = 120
```

Declare the referenced settings as configuration fields. `daily` requires a
default time and a 30–3600-second grace window; missed windows are skipped.
`interval` uses `interval_key`, `default_interval`, `min_interval` and
`max_interval` instead of time fields, and runs in time buckets. Follow
[`scheduler.py`](../app/services/plugin_runtime/scheduler.py) and
[`app/core/time.py`](../app/core/time.py) for deduplication and Shanghai time.

Scheduled context identifies tenant, binding and bot, usually no conversation.
Group-level overrides of schedule fields produce additional jobs tagged with
`conversation_id`; the binding-level job still runs. A scheduled plugin must skip
groups that own a schedule override in its global run and deduplicate per group.
Resolve owned targets and check binding/bot/channel availability, enabled groups
and current plugin scope before calling `enqueue_outbound_job` with `plugin_code`.
Use `high_side_effect = true` for sends/external changes and avoid automatic
replay of uncertain side effects. The template demonstrates scheduling; queuing
is not proof of external receipt. Self-sent inbound messages remain archive-only.

### Automatic reply recall

This is a platform reply policy, default off, configured per robot binding with
group overrides. Plugins must not declare duplicate fields or start timers.

```json
{"reply_auto_revoke": {"enabled": true, "min_seconds": 60, "max_seconds": 80}}
```

Delays are integers from 1 to 110 seconds, minimum no greater than maximum.
Each successful reply chooses its own delay; equal bounds give a fixed delay.
Group settings inherit robot defaults; restoring defaults removes the override.
Standard replies and `outbound_batch` use the common policy. Reply payloads
cannot override it.

Countdowns start only after a successful send receipt with a native message ID.
Failed sends, missing IDs and unsupported channels do not start one. Current
adapters support WeBridge and Guga Agent subject to device capabilities; only a
confirmed recall marks the message recalled. Countdowns live in process memory,
are lost on restart and are not shared across replicas. Implementation details
belong in [`plugin_auto_revoke.py`](../app/services/plugin_auto_revoke.py).

## Business administration UI

Ordinary settings use manifest fields and the shared form. Rich collections,
uploads or workflows need explicit routes, a business service and business tables.
Use [`app/web/guess_image.py`](../app/web/guess_image.py),
[`guess_image_bank.py`](../app/services/guess_image_bank.py) and
[`guess_image_bank.html`](../app/templates/guess_image_bank.html) as the implemented
reference. Preserve tenant roles, resource ownership, plugin governance, CSRF,
audit behavior, shared page shell and a clear return path.

`[admin]` metadata describes an entry; it neither installs routes nor grants
permission. There is no generic plugin route registry/shell to call, and a ZIP
cannot register arbitrary FastAPI routes or load a separate frontend framework.
Keep business records out of `BotPlugin.config_json`. Do not restore removed
business backends from old proposals. Follow the shared UI guidelines.

## Packaging and validation

The ZIP has `config.toml` and `plugin.py` at its root or inside one plugin directory.
[`plugin_packages.py`](../app/services/plugin_packages.py) allows at most 20 MiB
compressed, 50 MiB unpacked and 500 files. Encrypted entries, symlinks, absolute
paths and `..` traversal are rejected. Include required source and licensed assets
only, preserving attribution obligations. All Python files must parse.

Platform administrators install/upgrade packages. New packages start platform-only
until visibility is configured. Increase `version` on releases. Upgrades archive
the prior package, retaining up to ten versions for rollback. Package rollback
does not undo business data, migrations or external effects. Verify actual reload
behavior when helper modules change; do not assume entry-file reload refreshes all
Python imports.

Use [the testing guide](../tests/README.md) and
[`test_plugin_template.py`](../tests/test_plugin_template.py). Check the main action,
non-matching input, invalid/missing configuration, upstream failure and affected
ownership/idempotency boundaries with fictional data and mocked external services.
The isolated runner expects a list file, not a Python test file.

Run the relevant manifest/package/catalog checks for shipped changes. Private
plugins can be excluded from catalog expectations through `FULCRUM_PRIVATE_PLUGINS`,
but still require explicit Git exclusions, publication review and the shared
screenshot policy. Follow the root contract's minimal public-file rules.
