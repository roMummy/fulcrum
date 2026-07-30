# WorkWechat Channel Contacts Design

## Goal

Add a channel-level WorkWechat contacts dimension without mixing it with group members.

## Scope

This feature adds standalone contact storage for `workwechat` channels. It does not change the meaning of `ChatroomMember`, and it does not derive contacts from group member snapshots. Group members remain a group-scoped identity. Contacts are a channel-scoped address book/customer-contact identity.

## Source Hook API

The source interface document is `vendors/workwechat_provider/hook_runtime/API.docx`.

- `11036`: get internal contact list. The response stores contacts in `data.user_list`.
- `11037`: get external contact list. The response stores contacts in `data.user_list`.
- `11039`: get contact detail by `data.user_id`.
- `11063`: contact request notification.
- `11076`: contact added notification.
- `11077`: contact deleted notification. This is treated as the other side deleting this account, so the contact is preserved and marked deleted.

The provider exposes these Hook calls through `/contacts/list` and `/contacts/detail`; the admin app stores the returned contacts at the channel level.

## Data Model

### `workwechat_contacts`

The contact table stores one contact identity under one channel.

- `channel_id`: owning channel.
- `contact_kind`: `internal_member`, `external_contact`, or `unknown`.
- `contact_external_id`: stable local key for the contact. For internal contacts this can be `userid`; for external contacts this can be `external_userid`.
- `conversation_external_id`: direct chat conversation ID from Hook `conversation_id`; customer operations use this value for one-to-one delivery.
- `external_userid`, `userid`, `open_userid`, `unionid`: WorkWechat identity fields.
- `name`, `display_name`, `alias`, `english_name`: display and lookup names.
- `avatar_url`, `gender`, `contact_type`: profile fields.
- `corp_name`, `corp_full_name`, `corp_id`, `position`: company profile fields.
- `mobile`, `email`, `biz_mail`, `telephone`, `address`: contact fields.
- `department_ids_json`, `main_department_id`, `is_leader_in_dept_json`, `direct_leader_json`: internal member department fields.
- `hide_mobile`, `enable`, `member_status`: internal member flags.
- `external_profile_json`, `extattr_json`, `raw_contact_json`: full source payload snapshots.
- `source`: `manual`, `provider_sync`, `webhook`, or `api`.
- `sync_status`: `active`, `deleted`, `missing`, or `unknown`.
- `first_seen_at`, `last_seen_at`, `last_synced_at`: sync timestamps.
- `deleted_at`, `delete_reason`, `delete_event_key`: soft-delete lifecycle fields.

Uniqueness is channel-level: `(channel_id, contact_external_id)`.

### `workwechat_contact_relations`

The relation table stores the relationship between a contact and one owner user/account in the same channel. This is where deletion by the customer is represented.

- `channel_id`, `contact_id`: relation ownership.
- `owner_userid`, `owner_name`: the staff/account that owns the relation.
- `relationship_status`: `active`, `deleted_by_contact`, `deleted_by_staff`, `transferred`, or `unknown`.
- `is_deleted`, `deleted_at`, `delete_reason`, `delete_event_key`: relation deletion fields.
- `remark`, `description`, `remark_corp_name`, `remark_mobiles_json`, `tags_json`: customer-contact relation annotations.
- `add_way`, `state`, `oper_userid`, `createtime`: WorkWechat customer-contact metadata.
- `last_seen_at`, `last_synced_at`, `raw_follow_user_json`: sync fields.

If a user deletes this account, the contact row is preserved. Only the relation row is marked as `deleted_by_contact`.

## UI

Contacts live under the channel:

- `GET /channels/{channel_id}/workwechat/contacts`: channel-level contact list.
- `GET /channels/{channel_id}/workwechat/contacts/new`: create form.
- `GET /channels/{channel_id}/workwechat/contacts/{contact_id}`: detail and edit form.
- `POST /channels/{channel_id}/workwechat/contacts`: create.
- `POST /channels/{channel_id}/workwechat/contacts/{contact_id}/edit`: update.
- `POST /channels/{channel_id}/workwechat/contacts/{contact_id}/mark-deleted`: mark contact deleted.
- `POST /channels/{channel_id}/workwechat/contacts/{contact_id}/restore`: restore contact.
- `POST /channels/{channel_id}/workwechat/sync-contacts`: sync contacts from the provider Hook API.

The channel detail page shows a contacts card only for `workwechat` channels.

## Non-Goals

- Do not bind contacts to `ChatroomMember`.
- Do not show contacts inside the group member table.
- Do not delete contact rows physically when the other side deletes this account.
