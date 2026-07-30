# WorkWechat Channel Contacts Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add channel-level WorkWechat contacts that stay separate from group members and preserve deletion lifecycle state.

**Architecture:** Add two SQLAlchemy models, a contact service, channel-level routes, provider Hook proxy endpoints, and two templates. Runtime schema creation follows the existing `ensure_runtime_schema` pattern because the project does not use Alembic.

**Source Hook API:** Use `vendors/workwechat_provider/hook_runtime/API.docx` as the source document. Contact list sync calls Hook `11036` for internal contacts and Hook `11037` for external contacts. Contact detail calls Hook `11039`. Contact notifications handle Hook `11063`, `11076`, and `11077`; `11077` marks the contact and relation as deleted without physically deleting rows.

**Tech Stack:** FastAPI, SQLAlchemy ORM, Jinja2 templates, unittest/pytest.

---

### Task 1: Models And Runtime Schema

**Files:**
- Modify: `app/models/entities.py`
- Modify: `app/services/runtime.py`

**Steps:**
1. Add `WorkWechatContact` and `WorkWechatContactRelation` models.
2. Add both models to runtime imports.
3. Ensure both tables are created in `ensure_runtime_schema`.
4. Add tests through the service test in Task 2.

### Task 2: Contact Service

**Files:**
- Create: `app/services/workwechat_contacts.py`
- Test: `tests/test_workwechat_contacts.py`

**Steps:**
1. Write tests for create/update/list/delete/restore.
2. Implement normalization helpers for contact fields.
3. Implement `upsert_workwechat_contact`.
4. Implement `build_workwechat_contact_list`.
5. Implement `build_workwechat_contact_detail`.
6. Implement `mark_workwechat_contact_deleted` and `restore_workwechat_contact`.

### Task 3: Routes

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_workwechat_contacts.py`

**Steps:**
1. Import contact service helpers.
2. Add list, create form, detail, create, edit, mark-deleted, restore routes under `/channels/{channel_id}/workwechat/contacts`.
3. Restrict routes to `channel_type == "workwechat"`.
4. Record audit events for create/edit/delete/restore.

### Task 4: Templates

**Files:**
- Create: `app/templates/workwechat_contact_list.html`
- Create: `app/templates/workwechat_contact_detail.html`
- Modify: `app/templates/channel_detail.html`

**Steps:**
1. Add channel detail contact summary card.
2. Add contact list with filters and create action.
3. Add contact detail/edit form.
4. Keep UI in the existing admin style.

### Task 5: Provider Hook Sync

**Files:**
- Modify: `vendors/workwechat_provider/app.py`
- Modify: `app/services/workwechat_contacts.py`
- Test: `tests/test_workwechat_provider.py`
- Test: `tests/test_workwechat_contacts.py`

**Steps:**
1. Add provider `/contacts/list` backed by Hook `11036` and `11037`.
2. Add provider `/contacts/detail` backed by Hook `11039`.
3. Normalize Hook notifications `11063`, `11076`, and `11077`.
4. Sync provider contacts into the channel-level contact tables.
5. Mark Hook `11077` as `deleted_by_contact`.

### Task 6: Verification

**Commands:**
- `pytest tests/test_workwechat_contacts.py -q`
- `pytest tests/test_workwechat_chatrooms.py tests/test_workwechat_provider.py -q`

**Expected:**
- New contact tests pass.
- Existing WorkWechat group/provider tests still pass.
