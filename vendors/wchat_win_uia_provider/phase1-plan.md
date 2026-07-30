# WeChat Windows UIA Provider 一期方案

## 1. 一期目标

一期只解决两件事：

1. 做出一个可独立运行的 Windows 微信 UIA Provider
2. 跑通两条稳定能力链
   - 左侧会话摘要 watcher
   - 当前打开会话采集

一句话定义：

> 一期是“会话列表 watcher + 当前会话采集器”，不是“全量正文后台监听器”。

---

## 2. 一期能力范围

### 2.1 纳入一期

- 发现并连接微信主窗口
- 读取左侧 `session_list`
- 提取会话项：
  - `title`
  - `preview`
  - `unread_hint`
  - `timestamp`
  - `raw_text`
  - `auto_id`
  - `rect`
- 周期轮询左侧会话列表
- 对比会话摘要变化并产出事件流
- 读取当前打开会话：
  - 标题
  - 人数 hint
  - 当前可见消息列表
- 提取当前可见消息：
  - 文本消息
  - 文件消息
  - 基础消息类型
- 输出标准 JSON 文件
- 暴露独立 Provider API
- 提供基础健康检查与运行状态查询

### 2.2 一期不做

- 无点击监听所有会话完整正文
- 全会话正文后台 watcher
- 纯 UIA callback 事件驱动
- 复杂消息完整还原
  - 引用消息
  - 卡片消息
  - 语音识别
  - 小程序卡片
- 多微信实例调度
- 联系人/群成员全量同步
- 强依赖主项目的数据库或插件体系

### 2.3 一期可保留占位

- 切换指定会话
- 发送文本
- 发送文件/图片
- Webhook 回调主服务

这些能力可以保留接口，但不作为一期验收阻塞项。

---

## 3. 一期架构

```text
FastAPI Provider
  -> Runtime
  -> Agent
     -> WindowFinder
     -> SessionListCollector
     -> CurrentChatCollector
     -> SessionListWatcher
  -> State Store
     -> agent_snapshot.json
     -> session_previews.json
     -> watcher_state.json
     -> session_events.jsonl
```

### 3.1 模块职责

#### `window_finder.py`
- 查找微信主窗口
- 判断窗口是否可访问
- 提供窗口连接重试入口

#### `session_list.py`
- 读取左侧会话列表
- 解析每个会话项的结构化字段

#### `current_chat.py`
- 读取当前聊天标题
- 读取人数 hint
- 读取当前可见消息列表

#### `watcher.py`
- 周期采集左侧会话列表
- 比较变化
- 输出事件流

#### `runtime.py`
- 统一调度采集、watcher、状态查询
- 暴露给 API 层

#### `snapshot_store.py` / `event_store.py`
- 负责 JSON 文件持久化

---

## 4. 一期输出定义

### 4.1 `agent_snapshot.json`

主输出文件，聚合当前快照：

- `provider`
- `generated_at`
- `conversation`
- `capabilities`
- `chats`
- `messages`
- `summary`
- `meta`

### 4.2 `session_previews.json`

保存本轮左侧会话列表快照：

- `items`

### 4.3 `watcher_state.json`

保存 watcher 上一轮指纹，用于 diff。

### 4.4 `session_events.jsonl`

保存事件流，最少支持：

- `session_seen`
- `session_changed`
- `session_preview_changed`
- `session_disappeared`

---

## 5. 一期 API

### 5.1 必做接口

- `GET /health`
- `GET /runtime`
- `GET /snapshot`
- `GET /sessions`
- `GET /current-chat`
- `GET /watcher/status`
- `POST /watcher/start`
- `POST /watcher/stop`
- `GET /events/recent`

### 5.2 预留接口

- `POST /actions/switch-session`
- `POST /actions/send-text`
- `POST /actions/send-file`

---

## 6. 一期验收标准

满足以下条件即可认为一期完成。

### 6.1 Provider 可运行

- Provider 可独立启动
- 健康检查接口可访问
- 运行状态接口可访问

### 6.2 左侧 watcher 可运行

- 能读取左侧会话列表
- 能连续运行至少 30 分钟
- 不点击 UI
- 不切换会话
- 不抢焦点
- 能产出会话变化事件

### 6.3 当前会话采集可运行

- 能识别当前聊天标题
- 能识别人数 hint
- 能读取当前可见消息
- 能提取文本消息
- 能提取文件消息
- 能输出统一快照 JSON

### 6.4 文件输出稳定

- `agent_snapshot.json` 正常生成
- `session_events.jsonl` 正常追加
- `session_previews.json` 正常覆盖更新
- `watcher_state.json` 正常保存
- UTF-8 文件编码稳定

---

## 7. 一期测试清单

### 7.1 基础连接

- 微信窗口正常打开时能找到主窗口
- 微信窗口失焦时仍可找到主窗口
- 微信窗口尺寸变化后仍可采集
- 微信重新打开后可恢复连接

### 7.2 左侧 watcher

- 能读取至少 10 个会话项
- 新消息到达后 preview 变化可捕获
- 未读数变化可捕获
- 时间字段变化可捕获
- 连续变化不会明显漏报
- 运行 30 分钟无崩溃

### 7.3 当前聊天采集

- 私聊场景可采集
- 群聊场景可采集
- 文本消息可提取
- 文件消息可提取
- 标题与消息区内容一致

### 7.4 编码与落盘

- 中文标题不乱码
- 中文 preview 不乱码
- `jsonl` 文件追加正常
- 重启 provider 后状态文件仍可读取

---

## 8. 一期风险

### 8.1 主窗口连接不稳定

风险：
- 偶发 `WECHAT_WINDOW_NOT_FOUND`

处理：
- 重试
- 延迟探测
- 多轮查找窗口

### 8.2 会话列表解析漂移

风险：
- 微信版本变更导致控件层级变化

处理：
- 保留 `auto_id / class / rect / raw_text`
- 把解析逻辑集中在 collector 层

### 8.3 正文采集受窗口状态影响

风险：
- 最小化、遮挡、布局变化导致正文采集失败

处理：
- 将“当前会话采集”与“左侧 watcher”分开验收
- 允许正文采集失败时仍保留 watcher 服务可用

---

## 9. 一期研发顺序

1. 完成 `window_finder`
2. 完成 `session_list` 采集
3. 完成 `session_previews.json` 与 `watcher_state.json`
4. 完成 `watcher.py` 差异检测与事件流
5. 完成 `current_chat.py`
6. 完成 `agent_snapshot.json`
7. 跑通 `/snapshot`、`/sessions`、`/current-chat`
8. 跑一期验收测试

---

## 10. 一期完成后的定位

一期完成后，这个 Provider 可以被描述为：

> 一个独立运行的 Windows 微信 UIA Provider，能够稳定监听左侧会话摘要变化，并采集当前打开会话的结构化快照。

不要描述为：

> 可以后台持续读取所有会话完整正文。
