# 开放 API 按 874 能力补全设计

## 背景

当前开放文档中心已经提供 `/api/open/*` 的 HMAC 鉴权、群和会话查询、消息与文件发送、任务查询以及 Webhook 说明。实际开放接口仍只有少量路由，而微信 iPad 874 客户端已经覆盖登录以外的消息、群聊、联系人、用户资料和媒体工具能力。

本设计把已经具备的 874 非登录能力转换为稳定的业务 API，让注册并购买有效套餐的租户可以自行创建开放应用、授权渠道并开发自己的订单、客服、通知和自动化系统。外部业务系统不直接依赖 874 原始字段、设备状态或本地代理登录流程。

## 目标

1. 在现有 `/api/open/*` 入口下补齐 874 的非登录业务能力。
2. 让企业主或企业管理员可以自助管理开放应用、密钥、授权渠道和 Webhook。
3. 用订阅状态和套餐额度控制开放能力，幂等重试不能重复扣费。
4. 使用统一资源字段、错误码、任务对象和 Webhook 事件，屏蔽 874 的协议差异。
5. 保持现有接口可继续使用；破坏性变化通过新的版本路径发布。

## 非目标

- 不开放微信扫码、874 登录心跳、登录缓存、设备唤醒、登出或 `Tools/setproxy`。
- 不让外部系统提交本地 SOCKS5、代理凭证或 Provider 地址。
- 不把 874 原始请求和响应做成无约束的透传接口。
- 不把平台运营账号、平台代理池和协议服务配置暴露给租户。

## 当前实现基线

- 874 固定端点契约位于 `app/services/wechat_ipad/contract.py`。
- 874 客户端已经提供消息、群聊、联系人、用户资料、媒体下载和朋友圈上传方法。
- 当前开放 API 位于 `app/api/open_api.py`，已有群、会话、消息、文件和任务接口。
- 开放应用密钥和渠道授权模型位于 `OpenApiApp`、`OpenApiAppChannel`；当前服务层已有保存密钥和同步授权渠道能力，但企业自助管理页面尚未形成完整闭环。
- 当前套餐订单需要提交付款凭证后由平台审核；要实现购买后立即自助使用，必须增加支付成功回调和自动履约。

## 对外 API 分层

### 认证与租户边界

所有 `/api/open/*` 请求继续使用 `X-App-Key`、`X-Timestamp`、`X-Signature`，必要时携带 `X-Tenant-Code`。签名原文仍为 `timestamp + "\\n" + 原始请求体`，时间戳窗口和 HMAC-SHA256 规则保持不变。

认证成功后，服务端得到租户和开放应用身份，后续所有资源查询都必须限制在开放应用已授权的渠道范围内。客户端提交的 `channel_id` 不能绕过 `OpenApiAppChannel` 授权关系。

### 账号和能力发现

- `GET /api/open/accounts`
- `GET /api/open/accounts/{account_id}`
- `GET /api/open/accounts/{account_id}/capabilities`

账号接口只返回已经接入账号的业务状态、显示资料、渠道类型、最近在线时间和可用能力。扫码登录、代理、设备指纹和 874 内部凭证不出现在响应中。

`capabilities` 返回按账号和渠道计算的能力集合，例如 `message.text`、`message.voice`、`group.members.read`、`contact.read`、`media.upload`。业务系统据此决定是否展示功能，避免把不支持的 874 操作写死在客户端。

### 联系人和好友

- `GET /api/open/contacts`
- `GET /api/open/contacts/{external_id}`
- `POST /api/open/friend-requests/{external_id}/accept`

联系人查询支持关键字、游标和更新时间过滤。好友申请处理是异步任务，使用 `request_id` 幂等；平台只返回统一联系人标识和业务状态。

### 群和成员

- `GET /api/open/groups`
- `GET /api/open/groups/{external_id}`
- `GET /api/open/groups/{external_id}/members`
- `GET /api/open/groups/{external_id}/qrcode`
- `POST /api/open/groups/{external_id}/members`

群资源使用渠道和群 `external_id` 定位，响应可以包含群名称、备注、状态、成员数量和成员外部标识。新增或邀请成员是异步任务，具体动作由渠道能力矩阵决定。

### 消息

现有 `POST /api/open/messages` 扩展 `message_type`，支持：

- `text`
- `image`
- `voice`
- `video`
- `emoji`
- `link`
- `card`
- `mini_program`
- `app`

补充以下接口：

- `POST /api/open/messages/{message_id}/revoke`
- `GET /api/open/messages/sync`

所有写操作返回统一任务对象。消息发送必须保存稳定的 `request_id`；网络重试复用同一个键时返回原任务，不重复创建消息或重复扣减额度。

### 媒体

- `POST /api/open/media`
- `GET /api/open/media/{media_id}`

媒体上传先进入平台受控存储，再由消息任务引用媒体 ID。下载接口返回短时有效地址或流式响应，不暴露服务器绝对路径。文件大小、类型、保存时间和月度流量受套餐额度限制。

### 任务

- `GET /api/open/jobs/{request_id}`

任务响应统一包含 `request_id`、任务 ID、状态、创建时间、完成时间、结果摘要和可重试信息。874 原始响应只进入内部日志和适配层，不直接返回给外部系统。

## Webhook 事件

开放应用可配置一个 Webhook 地址和密钥。平台异步投递以下事件：

- `message.received`
- `message.sent`
- `message.failed`
- `account.online`
- `account.offline`
- `account.reconnected`
- `job.completed`
- `job.failed`

事件使用统一信封：

```json
{
  "event_id": "evt_message_received_123_1762560000000",
  "event_type": "message.received",
  "occurred_at": "2026-11-08T12:00:00+08:00",
  "account": {"id": 3, "channel_id": 8},
  "conversation": {"external_id": "group:demo", "chat_type": "group"},
  "message": {"external_id": "msg-1", "type": "text", "content": "hello"},
  "job": null
}
```

投递请求带时间戳、签名和 `X-MBA-Event-ID`。业务系统必须先校验签名，再按 `event_id` 幂等处理。平台只在后台任务中投递 Webhook，外部系统慢或暂时失败不能阻塞渠道入站落库。

## 自助权限和套餐额度

企业主或企业管理员登录后可以：

1. 创建、停用和删除开放应用。
2. 生成、轮换和撤销 `app_secret`。
3. 选择已接入渠道并同步授权范围。
4. 配置 Webhook 地址、密钥和事件类型。
5. 查看当前套餐、已用额度、剩余额度和失败任务。

套餐额度首期包括：

- 开放应用数量。
- 授权渠道数量。
- 月度 API 调用次数。
- 月度消息发送次数。
- 月度媒体流量。

请求进入任务队列前预扣额度；同一开放应用和 `request_id` 的幂等重试不重复扣减。任务在明确失败且可安全判定未发送时返还预扣额度。订阅过期返回 `subscription_required`，额度耗尽返回 `quota_exceeded`，频率超限返回 `rate_limited`。

当前人工付款审核不能满足“买套餐后立即自助”。后续需要接入支付网关异步回调，校验支付订单、金额和签名后自动创建或续期 `TenantSubscription`，并写入平台审计记录。

## 统一错误和版本

错误响应固定为：

```json
{
  "ok": false,
  "error": {
    "code": "channel_not_allowed",
    "message": "当前开放应用没有授权该渠道",
    "request_id": "order-20261108-0001"
  }
}
```

状态码约定：`400` 参数错误、`401` 签名错误、`402` 订阅或额度不可用、`403` 权限不足、`404` 资源不存在、`409` 状态或幂等冲突、`429` 频率限制、`503` 渠道暂不可用。

当前 `/api/open/*` 作为第一版稳定入口，新增字段保持兼容。未来出现破坏性变更时使用 `/api/open/v2/*`，不增加隐式别名、双写或协议降级路径。

## 实施阶段和验收

### 阶段一：读能力和消息能力

扩展账号、能力、联系人、群、消息类型、撤回、同步和任务响应，保留现有消息与文件接口兼容行为。

### 阶段二：群管理和联系人写操作

增加好友申请、群成员详情、群二维码、成员新增/邀请，并为不支持的渠道返回能力错误。

### 阶段三：媒体和 Webhook

增加统一媒体资源、下载安全控制、账号状态事件、发送结果事件和投递重试。

### 阶段四：自助开放应用、额度和支付

增加租户自助开放应用管理、额度账本、订阅门禁、自动支付回调和账户页用量展示。

每个阶段必须完成：

1. Open API 鉴权、租户边界和渠道授权定向测试。
2. 请求参数、响应模板、错误码和幂等测试。
3. 874 适配器模拟测试，确认原始错误不会穿透到外部响应。
4. 201 环境健康检查和关键接口冒烟验证。
5. 更新 `docs/bridge-api.md`，同时记录未完成的真实微信账号或第三方系统验收边界。
