# WorkWechat Cloud WS Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Change WorkWechat delivery from cloud-to-enterprise direct HTTP to an enterprise-provider WebSocket session, using a one-time authorization code exchanged for a persistent local client token.

**Architecture:** Keep `TaskJob` as the queue, but use a dedicated `workwechat_cloud_outbound` job type for enterprise-client delivery. The provider registers with an authorization code, stores `client_token` locally, keeps a WebSocket session to cloud for real-time wakeups/events, and uses HTTP for register, fallback poll, complete, and fail. The provider client owns local task idempotency with lock files and a processed-job cache.

**Client config rule:** The enterprise client only needs `cloud_base_url` and one-time `authorization_code` on first setup. It must not require channel ID, bot ID, or provider API Base; the cloud resolves the channel from the authorization code.

**Tech Stack:** FastAPI, WebSocket, SQLAlchemy, existing `TaskJob`, existing `Channel.adapter_config_json`, Python provider, `websocket-client`, PySide2 launcher, unittest.

---

### Task 1: Cloud Auth Config Service

**Status:** Completed in commit `d970670 feat: 增加企微主动拉取配置和授权码服务`.

**Files:**
- Created: `app/services/workwechat_cloud_poll.py`
- Created: `tests/test_workwechat_cloud_poll.py`

**Covered behavior:**
- `workwechat_delivery_mode == "cloud_poll"` config parsing.
- Authorization code generation.
- One-time authorization code exchange.
- `client_token` hashing and verification.

### Task 2: Add Cloud Provider API and WS Registry

**Files:**
- Create: `app/api/workwechat_provider_cloud.py`
- Create: `app/services/workwechat_ws_sessions.py`
- Modify: `app/api/__init__.py`
- Modify: `app/main.py`
- Test: `tests/test_workwechat_cloud_poll.py`

**Step 1: Write tests**

Cover:
- `POST /api/workwechat/provider/register` accepts a valid authorization code and returns `client_token`.
- Register does not require `channel_id`; authorization code resolves the channel.
- Invalid authorization code returns an error without issuing token.
- `POST /api/workwechat/provider/poll` without `X-WorkWechat-Client-Token` returns unauthorized.
- WS registry can register, unregister, and no-op notify when no client is connected.

**Step 2: Implement router**

Add endpoints:

```text
POST /api/workwechat/provider/register
GET  /api/workwechat/provider/ws
POST /api/workwechat/provider/poll
POST /api/workwechat/provider/jobs/{job_id}/complete
POST /api/workwechat/provider/jobs/{job_id}/fail
```

Rules:
- Register uses `authorization_code`.
- Register request does not accept or require client-supplied `channel_id`.
- All other HTTP endpoints use `X-WorkWechat-Client-Token`.
- WS uses `client_token` query param initially: `/ws?client_token=...`.
- Never log plaintext authorization code or client token.

**Step 3: Implement WS registry**

`app/services/workwechat_ws_sessions.py`:
- In-memory `channel_id -> set[WebSocket]`.
- `register(channel_id, websocket)`.
- `unregister(channel_id, websocket)`.
- `notify_job_available(channel_id, job_id)` sends `{"event_type": "job_available", "job_id": job_id}`.
- Add comments that multi-process deployment needs Redis/pubsub later.

**Step 4: Include router**

Export from `app/api/__init__.py` and include in `app/main.py`.

**Step 5: Run tests**

Run: `.venv311/bin/python -m unittest tests.test_workwechat_cloud_poll -v`

**Step 6: Commit**

```bash
git add app/api/workwechat_provider_cloud.py app/services/workwechat_ws_sessions.py app/api/__init__.py app/main.py tests/test_workwechat_cloud_poll.py
git commit -m "feat: 增加企微Provider云端注册和WS接口"
```

### Task 3: Dedicated Cloud Outbound Job Type

**Files:**
- Modify: `app/services/task_queue.py`
- Modify: `app/services/workwechat_cloud_poll.py`
- Modify: `app/main.py`
- Modify: `app/services/outbound_batch.py`
- Modify: `app/services/customer_operations.py`
- Modify: `app/services/plugin_directory.py`
- Test: `tests/test_workwechat_cloud_poll.py`
- Test: existing outbound tests as needed

**Step 1: Write tests**

Cover:
- WorkWechat cloud mode enqueues `workwechat_cloud_outbound`.
- Direct mode still enqueues `outbound_dispatch`.
- Cloud job enqueue notifies WS registry when possible.

**Step 2: Add queue constant**

In `app/services/task_queue.py`:

```python
WORKWECHAT_CLOUD_OUTBOUND_JOB = "workwechat_cloud_outbound"
```

**Step 3: Add resolver**

In `app/services/workwechat_cloud_poll.py`:

```python
def resolve_outbound_job_type(channel: Channel) -> str:
    return WORKWECHAT_CLOUD_OUTBOUND_JOB if is_cloud_poll_enabled(channel) else OUTBOUND_DISPATCH_JOB
```

**Step 4: Replace enqueue sites**

Use `resolve_outbound_job_type(channel)` anywhere WorkWechat outbound jobs are created:
- `/channels/{channel_id}/outbound`
- customer operations
- plugin auto outbound
- outbound batch service

**Step 5: Notify WS after cloud enqueue**

If job type is `WORKWECHAT_CLOUD_OUTBOUND_JOB`, call `notify_job_available(channel.id, job.id)`. If no WS client is connected, the job remains pending for fallback HTTP poll.

**Step 6: Run tests**

Run targeted tests plus existing outbound/customer-operation tests.

**Step 7: Commit**

```bash
git add app/services/task_queue.py app/services/workwechat_cloud_poll.py app/main.py app/services/outbound_batch.py app/services/customer_operations.py app/services/plugin_directory.py tests
git commit -m "feat: 区分企微主动拉取出站任务类型"
```

### Task 4: Cloud Job Claim, Complete, and Fail

**Files:**
- Modify: `app/services/workwechat_cloud_poll.py`
- Modify: `app/api/workwechat_provider_cloud.py`
- Test: `tests/test_workwechat_cloud_poll.py`

**Step 1: Write claim tests**

Setup:
- Channel has valid `client_token_hash`.
- Conversation exists.
- `TaskJob(job_type="workwechat_cloud_outbound", status="pending")` exists.

Assert:
- `claim_cloud_jobs(db, channel, batch_size=5)` returns job details.
- Job becomes `running`.
- Attempts increments.
- Jobs from other channels are ignored.

**Step 2: Implement claim**

Claim only:
- `job_type == WORKWECHAT_CLOUD_OUTBOUND_JOB`
- `status == "pending"`
- `available_at <= now`
- payload `channel_id == channel.id`

Return:
- `job_id`
- `channel_id`
- `conversation_id`
- `conversation_external_id`
- `payload`
- `payload_hash`

**Step 3: Write complete/fail tests**

Cover:
- Complete marks job completed and stores adapter result.
- Repeated complete on completed job is idempotent.
- Fail retries while attempts `< max_attempts`, otherwise fails.
- Token channel cannot complete another channel's job.

**Step 4: Implement complete/fail**

Use existing `complete_job` and `fail_job`. For complete, create/update outbound `Message` equivalent to existing dispatch result path. If `customer_operation_request_id` exists, update customer operation state.

**Step 5: Wire endpoints**

`/poll`, `/jobs/{job_id}/complete`, `/jobs/{job_id}/fail` use service helpers.

**Step 6: Run tests and commit**

```bash
.venv311/bin/python -m unittest tests.test_workwechat_cloud_poll -v
git add app/services/workwechat_cloud_poll.py app/api/workwechat_provider_cloud.py tests/test_workwechat_cloud_poll.py
git commit -m "feat: 增加企微Provider云端任务领取和回传"
```

### Task 5: Provider Client Idempotency Store

**Files:**
- Create: `vendors/workwechat_provider/cloud_jobs.py`
- Test: `tests/test_workwechat_provider.py`

**Step 1: Write tests**

Cover:
- First `try_acquire_job_lock(job_id, payload_hash)` succeeds.
- Duplicate lock before timeout fails.
- `mark_job_processed(job_id, payload_hash, result)` records completion.
- `is_job_processed(job_id, payload_hash)` prevents replay.
- Same `job_id` with different `payload_hash` is rejected.

**Step 2: Implement local store**

Files:

```text
data/cloud_jobs/locks/job-{job_id}.lock
data/cloud_jobs/processed.json
```

Rules:
- Use atomic `open(path, "x")` for lock acquisition.
- Lock includes `job_id`, `event_id`, `payload_hash`, `started_at`.
- Processed cache includes `job_id`, `payload_hash`, `completed_at`, `provider_message_id`.
- Keep recent 5000 processed entries or recent 7 days.
- If lock is stale, allow recovery only after cloud status confirmation.

**Step 3: Commit**

```bash
git add vendors/workwechat_provider/cloud_jobs.py tests/test_workwechat_provider.py
git commit -m "feat: 增加企微Provider任务幂等锁"
```

### Task 6: Provider Cloud HTTP Client

**Files:**
- Modify: `vendors/workwechat_provider/app.py`
- Modify: `vendors/workwechat_provider/config.example.json`
- Test: `tests/test_workwechat_provider.py`

**Step 1: Add config fields**

```python
cloud_poll_enabled: bool = False
cloud_base_url: str = ""
authorization_code: str = ""
client_token: str = ""
cloud_channel_id: int | None = None
provider_name: str = ""
poll_interval_seconds: int = 30
poll_batch_size: int = 5
```

**Step 2: Implement config persistence**

`save_config_updates(updates)`:
- Read `config.json`.
- Merge updates.
- Write `ensure_ascii=False`, indent 2.
- Mask token in logs.

**Step 3: Implement HTTP helpers**

- `cloud_post_json(path, body, token="")`
- `register_cloud_client_if_needed()`
- `poll_cloud_jobs_once()`
- `process_cloud_job(job)`
- `report_cloud_job_complete(job_id, result)`
- `report_cloud_job_fail(job_id, error)`

`process_cloud_job` must use the local idempotency store before calling Hook.

**Step 4: Run tests and commit**

```bash
.venv311/bin/python -m unittest tests.test_workwechat_provider -v
git add vendors/workwechat_provider/app.py vendors/workwechat_provider/config.example.json tests/test_workwechat_provider.py
git commit -m "feat: 增加企微Provider云端HTTP客户端"
```

### Task 7: Provider WebSocket Client and Fallback Poll

**Files:**
- Modify: `vendors/workwechat_provider/app.py`
- Modify: `vendors/workwechat_provider/requirements.txt`
- Test: `tests/test_workwechat_provider.py`

**Step 1: Add dependency**

```text
websocket-client>=1.8.0
```

**Step 2: Implement WS loop**

Use a daemon thread:
- Start after Hook runtime startup in lifespan.
- Register if needed.
- Connect to `/api/workwechat/provider/ws?client_token=...`.
- On `job_available`, call `/poll` immediately.
- On full `job` event, still pass through idempotency store before execution.
- On disconnect, reconnect with backoff.
- Run fallback `/poll` every `poll_interval_seconds`.
- Keep direct `/send-message` endpoint working.

**Step 3: Health status**

`/health` includes:

```json
"cloud_poll": {
  "enabled": true,
  "ws_connected": true,
  "last_ws_event_at": "...",
  "last_poll_at": "...",
  "last_error": ""
}
```

**Step 4: Commit**

```bash
git add vendors/workwechat_provider/app.py vendors/workwechat_provider/requirements.txt tests/test_workwechat_provider.py
git commit -m "feat: 启动企微Provider云端WS任务会话"
```

### Task 8: Launcher Authorization UI

**Files:**
- Modify: `vendors/workwechat_provider/launcher_app.py`
- Modify: `vendors/workwechat_provider/launcher_state.py`
- Test: `tests/test_workwechat_provider_launcher.py`

**Step 1: Write tests**

Cover:
- Saving `cloud_base_url` and `authorization_code` into config.
- UI has no channel ID input.
- `LauncherStatus` renders cloud auth error in Chinese.

**Step 2: Add compact controls**

Add operational controls only:
- 云端地址
- 授权码
- 保存授权
- 连接状态

Do not add channel ID, bot ID, or API Base inputs.

No instructional paragraphs in the app UI.

**Step 3: Save behavior**

When saving authorization:
- Set `cloud_poll_enabled=true`.
- Save `cloud_base_url`.
- Save `authorization_code`.
- If authorization code changes, clear `client_token`.
- Restart provider.

**Step 4: Commit**

```bash
git add vendors/workwechat_provider/launcher_app.py vendors/workwechat_provider/launcher_state.py tests/test_workwechat_provider_launcher.py
git commit -m "feat: 增加启动器企微云端授权配置"
```

### Task 9: Admin Authorization Code UI

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/channel_detail.html`
- Test: `tests/test_workwechat_cloud_poll.py`

**Step 1: Add route**

```text
POST /channels/{channel_id}/workwechat/cloud-poll/authorization-code
```

Requires ops role.

**Step 2: Template**

For WorkWechat channel detail:
- Show delivery mode.
- Button: 生成本地客户端授权码.
- Show one-time code after generation.
- Show masked registration status and last poll time.

**Step 3: Commit**

```bash
git add app/main.py app/templates/channel_detail.html tests/test_workwechat_cloud_poll.py
git commit -m "feat: 增加企微客户端授权码管理入口"
```

### Task 10: Docs and Packaging

**Files:**
- Modify: `vendors/workwechat_provider/README.md`
- Modify: `vendors/workwechat_provider/build-provider-launcher.bat`
- Modify: `docs/workwechat_provider.md`

Document:
- Direct HTTP mode.
- Cloud WS mode.
- Fallback polling.
- Authorization code exchange.
- Local `client_token` storage.
- Client-side idempotency lock and replay handling.
- Token rotation/revocation.

Commit:

```bash
git add vendors/workwechat_provider/README.md vendors/workwechat_provider/build-provider-launcher.bat docs/workwechat_provider.md
git commit -m "docs: 更新企微Provider云端WS部署说明"
```

### Task 11: Final Verification, Tag, Push

Run:

```bash
.venv311/bin/python -m unittest tests.test_workwechat_cloud_poll tests.test_workwechat_provider tests.test_workwechat_provider_launcher -v
.venv311/bin/python -m unittest tests.test_workwechat_contacts tests.test_workwechat_chatrooms tests.test_customer_operations -v
git diff --check
git status --short
```

Windows manual verification:

1. Build launcher.
2. Generate authorization code in cloud channel page.
3. Start provider launcher.
4. Enter cloud URL and authorization code.
5. Confirm `client_token` saved locally and authorization code cleared.
6. Queue an outbound message from cloud.
7. Confirm provider receives WS `job_available` and sends through Hook.
8. Confirm cloud job completes and message is recorded.
9. Replay the same job notification and confirm provider does not send twice.

Tag and push:

```bash
git tag -a workwechat-cloud-ws-v2026.06.07 -m "企业微信Provider云端WS任务会话版本"
git push origin main
git push origin workwechat-cloud-ws-v2026.06.07
```
