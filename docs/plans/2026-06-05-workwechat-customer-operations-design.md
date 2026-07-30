# WorkWechat Customer Operations Design

## Goal

Provide one business-facing customer operation entrypoint for WorkWechat proactive customer messaging.

## Boundary

The business system decides whether a customer can be contacted and what should be sent. MBA does not own business eligibility, user lifecycle rules, campaign selection, copywriting, mini program metadata, product links, order links, or media asset business meaning.

MBA owns the technical delivery boundary:

- validate the channel and WorkWechat contact state;
- reject contacts that are missing or already marked deleted;
- resolve the direct conversation ID from channel-level contact data;
- enqueue delivery through the existing outbound channel queue;
- record idempotency, request state, job linkage, message linkage, and adapter result.

Business systems must not call the low-level WorkWechat Hook API directly. They call MBA, and MBA calls the channel adapter/provider.

## Public API

The first version exposes one endpoint:

`POST /api/workwechat/customer-operations`

External integration documentation: `docs/workwechat_customer_operations_api.md`.

The request is authorized with `X-Customer-Operation-Token`. The expected token is configured on the channel adapter config as `customer_operation_token` or in headers as `X-Customer-Operation-Token`.

Example:

```json
{
  "request_id": "order-123-send-1",
  "channel_id": 3,
  "target": {
    "external_userid": "wm_xxx",
    "userid": "788130xxx",
    "contact_external_id": "wm_xxx"
  },
  "scene": "order_followup",
  "operation_type": "send_message",
  "content": {
    "type": "text",
    "text": "您好，您的订单已经处理完成"
  },
  "biz_payload": {
    "biz_user_id": "u123",
    "order_id": "o456"
  }
}
```

The API returns `queued`, `rejected`, or `duplicate`. A repeated `request_id` under the same channel returns the existing request and does not enqueue another job.

## Content Types

Business systems pass fully assembled content. MBA does not maintain a media/template center in the first version.

- `text`: business passes `text`.
- `image`: business passes `image_url`, `image_path`, or image base64 fields.
- `file`: business passes `file_url` or `file_path`.
- `link_card`: business passes `url`, `title`, `desc`, and optional `image_url`.
- `mini_program`: business passes `username`, `appid`, `appname`, `appicon`, `title`, `page_path`, `file_id`, `aes_key`, `md5`, and `size`.

`link_card` maps to Hook `11159`. `mini_program` maps to Hook `11162`. The mini program cover depends on C2cCDN metadata; business provides those fields in the first version.

## Data Model

`workwechat_contacts` gains `conversation_external_id`, sourced from `API.docx` contact list/detail field `conversation_id`.

`customer_operation_requests` stores:

- `channel_id`, `contact_id`, `conversation_id`, `job_id`, `message_id`;
- `request_id` for channel-level idempotency;
- `scene`, `operation_type`, `content_type`;
- target identifiers from the business request;
- status: `queued`, `rejected`, `sent`, `failed`;
- status reason;
- content, business payload, raw request, adapter result JSON snapshots.

## Delivery Flow

1. Business calls `POST /api/workwechat/customer-operations`.
2. MBA checks request token and loads the channel.
3. MBA creates or reuses a `customer_operation_requests` row by `(channel_id, request_id)`.
4. MBA resolves the contact from `contact_external_id`, `external_userid`, or `userid`.
5. If the contact is missing or deleted, MBA marks the request rejected.
6. MBA creates or reuses a direct `Conversation` using `WorkWechatContact.conversation_external_id`.
7. MBA enqueues an `outbound_dispatch` job with the normalized content payload.
8. The background worker sends through the channel adapter and updates the operation request with `sent` or `failed`.

## Non-Goals

- Do not expose low-level Hook type numbers to business systems.
- Do not build a campaign system, template center, or media center yet.
- Do not infer business eligibility in MBA.
- Do not mix customer contacts with group members.
