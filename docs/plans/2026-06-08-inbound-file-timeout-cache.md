# Inbound File Timeout Cache Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make repeated WorkWechat inbound files resilient to CDN download timeout by retrying longer and reusing a previously downloaded identical file.

**Architecture:** Keep the fix in MBA's inbound attachment pipeline. When a WorkWechat file attachment arrives as failed/timeout, look for a downloaded attachment with the same CDN `md5` and `size`; if found, mark the new attachment as downloaded with the cached path and dispatch `file.received`.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, existing `MessageAttachment` and open webhook dispatch services.

---

### Task 1: Cache Reuse

**Files:**
- Modify: `app/services/channel_events.py`
- Test: `tests/test_channel_events.py` or existing WorkWechat event tests

**Steps:**
1. Add a test where a prior downloaded attachment has matching `cdn.md5` and `cdn.size`.
2. Ingest a later WorkWechat file event with `download_error=timeout` and the same hash/size.
3. Verify the later attachment is marked `downloaded`, points to the cached file, and dispatches `file.received`.

### Task 2: Timeout Resilience

**Files:**
- Modify: WorkWechat provider download code or config where timeout is chosen
- Test: `tests/test_workwechat_provider.py`

**Steps:**
1. Increase file download timeout from the short default to a safer value for Office files.
2. Add retry for transient timeout/network failures where the provider controls downloading.
3. Verify existing WorkWechat provider tests still pass.

### Task 3: Verification

Run:

```bash
/data/multi-bot-admin/.venv311/bin/python -m pytest tests/test_workwechat_provider.py tests/test_open_webhooks.py -q
```

If test discovery or unrelated dependencies block broader tests, report the exact blocker.
