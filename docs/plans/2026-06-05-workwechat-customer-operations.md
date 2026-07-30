# WorkWechat Customer Operations Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add one business-facing WorkWechat customer operation entrypoint that validates channel-level contacts and queues outbound delivery.

**Architecture:** Add a `CustomerOperationRequest` persistence table, a `customer_operations` service, one public token-protected API router, and provider support for link cards and mini program cards. The API does not expose low-level Hook details; it resolves channel contacts and sends through the existing outbound queue.

**Tech Stack:** FastAPI, SQLAlchemy ORM, Jinja2 admin discovery via plugin config, unittest, existing `task_jobs/outbound_dispatch` worker.

---

### Task 1: Data Model

**Files:**
- Modify: `app/models/entities.py`
- Modify: `app/services/runtime.py`
- Test: `tests/test_customer_operations.py`

**Steps:**
1. Add `conversation_external_id` to `WorkWechatContact`.
2. Add `CustomerOperationRequest` with channel-level `(channel_id, request_id)` uniqueness.
3. Add both runtime schema ensures.
4. Add tests that contact normalization stores `conversation_id`.

### Task 2: Customer Operation Service

**Files:**
- Create: `app/services/customer_operations.py`
- Test: `tests/test_customer_operations.py`

**Steps:**
1. Write tests for queued request, duplicate request, missing contact, and deleted contact.
2. Implement target resolution by `contact_external_id`, `external_userid`, or `userid`.
3. Implement content normalization for text, image, file, link card, and mini program.
4. Create/reuse a direct conversation from the contact `conversation_external_id`.
5. Enqueue `outbound_dispatch` with `customer_operation_request_id`.

### Task 3: API Router

**Files:**
- Create: `app/api/customer_operations.py`
- Modify: `app/api/__init__.py`
- Modify: `app/main.py`
- Test: `tests/test_customer_operations.py`

**Steps:**
1. Add `POST /api/workwechat/customer-operations`.
2. Authorize with `X-Customer-Operation-Token`.
3. Make only this public API path bypass cookie auth.
4. Return a stable response for queued, rejected, and duplicate requests.

### Task 4: Provider Card Support

**Files:**
- Modify: `vendors/workwechat_provider/app.py`
- Modify: `app/services/channel_adapters/generic.py`
- Test: `tests/test_workwechat_provider.py`

**Steps:**
1. Map `link_card`/`card` to Hook `11159`.
2. Map `mini_program`/`miniprogram` to Hook `11162`.
3. Pass card fields through the generic adapter body.
4. Add provider unit tests for both Hook commands.

### Task 5: Worker Result Backfill

**Files:**
- Modify: `app/services/async_workers.py`
- Test: `tests/test_customer_operations.py`

**Steps:**
1. After outbound dispatch completes, update the related `CustomerOperationRequest`.
2. Store `message_id`, adapter result JSON, `sent` or `failed` status, and status reason.

### Task 6: Plugin Declaration And Verification

**Files:**
- Create: `plugins/customer_operations/config.toml`
- Create: `plugins/customer_operations/plugin.py`
- Test: `tests/test_customer_operations.py`

**Steps:**
1. Add a lightweight plugin declaration so the capability is visible in plugin discovery.
2. Let plugin execution call the same `submit_customer_operation` service for internal use.
3. Run:
   - `.venv311/bin/python -m unittest tests.test_customer_operations -v`
   - `.venv311/bin/python -m unittest tests.test_workwechat_provider tests.test_workwechat_contacts tests.test_workwechat_chatrooms -v`
