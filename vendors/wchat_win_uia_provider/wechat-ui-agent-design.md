# Windows 微信 UIA Agent 设计与研发文档

> 目的：把当前 PoC 的有效结论、UIA 探测结果、文件输出结构、调用方式、重构建议整理成一份可交给 Codex/Claude Code 继续工程化实现的设计文档。

---

## 1. 项目目标

为 **Windows 微信桌面版** 构建一个基于 **Windows UI Automation (UIA)** 的本地 Agent，能力目标如下：

1. 枚举左侧会话列表
2. 判断会话是否有新消息/未读提示
3. 提取会话摘要（preview）
4. 识别当前聊天标题、类型（私聊/群聊）
5. 切换到指定会话
6. 读取当前聊天可见消息列表
7. 将采集结果稳定落地到 JSON 文件
8. 后续基于这些 JSON 对接 provider / adapter（参考 `multi-bot-admin` 中 `wechat_ipad` 的结构）
9. 实现一个**无点击 watcher**，持续监听左侧会话摘要变化，并输出事件流

---

## 2. 当前 PoC 已确认能力

### 2.1 已验证通过

- **微信主窗口可被 UIA 识别**
  - 顶层类名：`mmui::MainWindow`
- **当前聊天标题可识别**
  - 关键节点：`current_chat_name_label`
- **群聊人数 hint 可识别**
  - 关键节点：`current_chat_count_label`
- **消息列表可识别**
  - 关键节点：`chat_message_list`
  - 页面区域：`chat_message_page`
- **输入框可识别**
  - 关键节点：`chat_input_field`
  - 类名：`mmui::ChatInputField`
- **左侧会话列表可识别**
  - 关键节点：`session_list`
  - 会话项类名：`mmui::ChatSessionCell`
- **会话切换可达**
  - 已通过人工验证切换到 `BOT-测试` 后，当前聊天与消息区采集正常
- **消息内容可提取**
  - 文本消息已可提取
  - 文件消息已可提取
- **摘要预览可提取**
  - 包括未读提示 `[1条]` / `[2条]`
  - 以及 preview 文本，例如 `水牛: 干活干活，测试一下`
- **文件输出链路已跑通**
  - 能稳定写出 `agent_snapshot.json` / `current_messages.json` / `chats_snapshot.json` 等
- **无点击 Session Watcher 已通过实测**
  - 能在不点击 UI / 不切换会话 / 不抢焦点的前提下，持续读取左侧会话摘要并捕获变化事件

### 2.2 当前成功样例

#### 当前聊天（已成功切换并采集）
- 标题：`BOT-测试`
- 类型：`group`
- 人数 hint：`(3)`

#### 当前消息样例
- `文件\n测试excel.xls\n微信电脑版`
- `哈哈[嘿哈]`
- `嘿嘿[愉快]`
- `干活干活，测试一下`

#### 左侧摘要样例
- 会话：`BOT-测试`
- 未读：`[2条]`
- 摘要：`水牛: 干活干活，测试一下`
- 时间：`20:56`

#### Watcher 捕获到的真实变化样例
会话 `BOT-测试` 在 45 秒 smoke test 内连续发生变化：
- `水牛: 干活干活，测试一下` → `水牛: 还行吧，测试一下`
- 未读 `null` → `[1条]`
- 随后又变为 `水牛: 你在么？测试一下`，未读变为 `[2条]`
- 随后又变为 `水牛: [链接] 小雨淅淅-点击直接观看`，未读变为 `[3条]`

---

## 3. 当前部署/运行环境

### 3.1 目标机器
- Windows 主机 IP：`192.168.2.212`
- SSH：已可登录
- 用户：`Liu_WIN10`
- 微信桌面版：已登录、可打开
- 桌面会话：存在交互式桌面（通过本地 PowerShell 触发脚本）

### 3.2 代码目录
当前 PoC 代码部署在：

```text
F:\wechat-ui-agent
```

### 3.3 依赖
当前只验证了：

```text
pywinauto
```

运行使用：

```text
Python 3.11 (通过 py -3.11 调用)
```

---

## 4. 当前项目文件结构（PoC）

```text
F:\wechat-ui-agent
  agent\
    __init__.py
    settings.py
    models.py
    wechat_uia.py
    normalizer.py
    snapshot.py
    session_watcher.py
  output\
    raw_current_chat.json
    normalized_current_chat.json
    chats_snapshot.json
    current_messages.json
    session_previews.json
    session_events.jsonl
    watcher_state.json
    collect_meta.json
    agent_snapshot.json
    collect_snapshot.log
    collect_snapshot.status.txt
    export_session_previews.log
    export_session_previews.status.txt
    switch_collect_bot_test.log
    switch_collect_bot_test.status.txt
    watch_session_list.log
    watch_session_list.stdout.log
    watch_session_list.status.txt
    watch_session_list_smoke_test.stdout.log
    watch_session_list_smoke_test.status.txt
  collect_snapshot.py
  export_session_previews.py
  switch_and_collect_bot_test.py
  watch_session_list.py
  watch_session_list_smoke_test.py
  run_collect_snapshot.cmd
  run_export_session_previews.cmd
  run_switch_collect_bot_test.cmd
  run_watch_session_list.cmd
  run_watch_session_list_smoke_test.cmd
  requirements.txt
  README.md
```

---

## 5. 当前输出文件定义

## 5.1 主输出：`agent_snapshot.json`

建议后续 provider 主要消费这个文件。

结构示例：

```json
{
  "ok": true,
  "provider": "wechat_windows_uia",
  "generated_at": "2026-03-30T21:15:17.761202",
  "conversation": {
    "external_id": "uia:chat:BOT-测试",
    "title": "BOT-测试",
    "chat_type": "group",
    "member_count_hint": "(3)"
  },
  "capabilities": {
    "session_list_detected": true,
    "message_list_detected": true,
    "input_box_detected": true
  },
  "active_session_preview": "BOT-测试\\n水牛: 干活干活，测试一下\\n20:56\\n",
  "summary": {
    "chat_count": 11,
    "message_count": 4,
    "has_current_chat": true
  },
  "chats": [...],
  "messages": [...],
  "meta": {
    "raw_chat_name": "BOT-测试",
    "raw_chat_count_hint": "(3)",
    "message_candidates": 54,
    "session_items": 11,
    "switch_result": null
  }
}
```

---

## 5.2 当前聊天输出：`normalized_current_chat.json`

聚焦当前聊天的结构化输出。

关键字段：
- `conversation`
- `chats`
- `messages`
- `capabilities`
- `rects`

---

## 5.3 会话列表输出：`chats_snapshot.json`

结构：

```json
{
  "items": [
    {
      "external_id": "uia:chat:BOT-测试",
      "title": "BOT-测试",
      "preview": "水牛: 干活干活，测试一下",
      "unread_hint": null,
      "has_new": false,
      "timestamp": "20:56",
      "raw_text": "BOT-测试\\n水牛: 干活干活，测试一下\\n20:56\\n",
      "auto_id": "session_item_BOT-测试",
      "rect": [1800, 248, 2040, 312],
      "chat_type_hint": "direct"
    }
  ]
}
```

> 注意：`chat_type_hint` 目前只是启发式结果，不能当强真值。

---

## 5.4 当前消息输出：`current_messages.json`

结构：

```json
{
  "items": [
    {
      "external_message_id": "uia:msg:...",
      "message_type": "text",
      "content": "嘿嘿[愉快]",
      "sender_name": null,
      "sender_id": null,
      "details": {
        "class": "mmui::ChatTextItemView",
        "friendly": "ListItem",
        "auto_id": "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view",
        "rect": [2041, 591, 2524, 669],
        "source": "windows_uia"
      }
    }
  ]
}
```

---

## 5.5 左侧摘要导出：`session_previews.json`

这是目前最稳定的一条能力链，适合做未读监控和新消息摘要检测。

结构示例：

```json
{
  "items": [
    {
      "title": "BOT-测试",
      "chat_type_hint": "direct",
      "unread_hint": "[2条]",
      "has_new": true,
      "preview": "水牛: 干活干活，测试一下",
      "timestamp": "20:56",
      "raw_text": "BOT-测试\\n[2条] \\n水牛: 干活干活，测试一下\\n20:56\\n",
      "auto_id": "session_item_BOT-测试",
      "rect": [1765, 308, 2005, 372]
    }
  ]
}
```

---

## 5.6 Watcher 事件流：`session_events.jsonl`

这是 Session Watcher 的主输出之一，用于记录左侧会话摘要变化事件。

结构示例：

```json
{"ts":"2026-03-30T21:44:09.271985","event":"session_preview_changed","chat":"BOT-测试","preview":"水牛: 还行吧，测试一下","unread_hint":"[1条]","timestamp":"21:44"}
```

常见事件类型：
- `session_seen`
- `session_changed`
- `session_preview_changed`
- `session_disappeared`

---

## 5.7 Watcher 状态文件：`watcher_state.json`

用于保存 watcher 上一次扫描得到的会话指纹，以便下一轮做 diff。

结构示例：

```json
{
  "updated_at": "2026-03-30T21:44:44.408553",
  "fingerprint": {
    "BOT-测试": {
      "preview": "水牛: [链接] 小雨淅淅-点击直接观看",
      "unread_hint": "[3条]",
      "timestamp": "21:44",
      "chat_type_hint": "direct"
    }
  },
  "count": 11
}
```

---

## 6. UIA 关键控件与节点

以下是当前已确认可用的关键节点：

### 顶层窗口
- 类名：`mmui::MainWindow`
- 标题：`微信`

### 当前聊天标题
- auto_id 后缀：`current_chat_name_label`

### 当前聊天人数提示
- auto_id 后缀：`current_chat_count_label`

### 当前消息页区域
- auto_id：`chat_message_page`
- 类名：`mmui::ChatMessagePage`

### 当前消息列表
- auto_id：`chat_message_list`
- 控件列表项多表现为：
  - `mmui::ChatTextItemView`
  - `mmui::ChatBubbleItemView`

### 输入框
- auto_id：`chat_input_field`
- 类名：`mmui::ChatInputField`
- friendly class：`Edit`

### 左侧会话列表
- auto_id：`session_list`
- 会话项类名：`mmui::ChatSessionCell`
- 常见会话项 auto_id 格式：
  - `session_item_水牛`
  - `session_item_BOT-测试`

---

## 7. 当前 Python 模块职责

## 7.1 `agent/wechat_uia.py`
负责：
- 连接微信主窗口
- 遍历 UIA descendants
- 提取当前聊天标题 / 人数 hint / 左侧会话列表 / 当前消息候选
- 切换指定会话

核心函数：
- `find_main_window()`
- `get_current_chat_title()`
- `collect_raw_snapshot()`
- `switch_to_chat(chat_name)`

---

## 7.2 `agent/normalizer.py`
负责：
- 把 raw snapshot 归一化成 provider 更容易消费的结构
- 构造：
  - `conversation`
  - `chats`
  - `messages`
- 过滤 UI 噪音
- 粗分类消息类型
- 构造派生 external ids

当前已做：
- 过滤左侧 session item 混入当前消息
- 过滤标题栏/按钮/容器噪音
- 提取文本/文件消息

---

## 7.3 `agent/snapshot.py`
负责：
- 调用 raw collector
- 调用 normalizer
- 写出多个 JSON 文件
- 组装 `agent_snapshot.json`

核心函数：
- `run_collect()`
- `run_switch_and_collect(chat_name)`

---

## 7.4 `agent/session_watcher.py`
负责：
- 周期读取左侧 `session_list`
- 解析出 `title / preview / unread_hint / timestamp`
- 将本轮结果与 `watcher_state.json` 做差异比较
- 将变化写入 `session_events.jsonl`
- 更新 `session_previews.json` 与 `watcher_state.json`

当前定位：
- **无点击会话级 watcher**
- 不负责切会话
- 不负责读取任意未打开会话的正文

---

## 7.5 `agent/models.py`
当前模型：
- `ConversationPayload`
- `MessagePayload`

这层建议后续继续扩展，不要过早耦合 provider 细节。

---

## 8. 当前 ID 策略（临时版）

由于 UIA 暂时拿不到类似 `wxid` / 原生 message id，当前采用派生 ID：

### 会话 ID
```text
uia:chat:<chat_name>
```

例如：
- `uia:chat:BOT-测试`
- `uia:chat:水牛`

### 消息 ID
基于以下字段求 md5：
- `chat_name`
- `rect`
- `text`
- `class`

输出：
```text
uia:msg:<12位hash>
```

> 这是过渡方案，足够做当前去重与文件输出。

---

## 9. 当前消息类型分类（第一版）

已支持：
- `text`
- `file`
- `link`
- `system`
- `unknown`

当前规则主要基于：
- 控件类名
- 文本后缀/关键词
- 消息文本内容

### 当前确认有效的样例
- `ChatTextItemView` → `text`
- `ChatBubbleItemView` + 文件名/后缀 → `file`

---

## 10. 当前已知问题 / 边界

## 10.1 当前聊天采集有时会失败
有时会出现：
- `WECHAT_WINDOW_NOT_FOUND`

已观察到：
- 这和窗口状态、前台/最小化/缩放状态可能有关
- 缩小窗口后再执行，成功率更高

### 建议
后续重构时加入：
- 重试
- 延迟探测
- 主窗口恢复/置前
- 多轮 `Desktop(...).windows()` 轮询

---

## 10.2 会话切换并非每次都能靠 `invoke()` 成功
虽然经过人工验证“切换会话可达”，但自动化层面还应继续强化。

当前已尝试策略：
- `invoke()`
- `click_input()`
- 中心点击
- 双击
- 聚焦左侧列表后键盘导航

### 建议
Codex 重构时可加入：
- 点击后轮询 `current_chat_name_label`
- 明确把每次点击前后标题写入日志
- 尝试利用搜索框切换会话

---

## 10.3 `chat_type_hint` 仍是启发式
当前规则已经比前一版更保守，但仍不是强语义。

### 当前问题
- 左侧会话预览中的 `昵称: 内容` 可能来自群聊，也可能是某些特殊场景
- 标题不带“群”字时，仅靠 preview 很难绝对准确判断

### 建议
后续分层：
- `chat_type_hint`：启发式
- `conversation.chat_type`：当前打开会话、结合人数 hint 的较强判断

---

## 10.4 Watcher 当前是 polling + diff，不是纯 UIA callback
当前 watcher 采用的是：
- 周期轮询左侧列表
- 保存上次状态
- 做差异比较
- 输出事件流

这条路线已经经过实测验证可行。

### 已验证成立
- 左侧会话级 watcher
- 新消息摘要变化检测
- 未读变化检测
- 事件流输出

### 尚未验证 / 不能直接宣称
- 不切会话就能读取所有会话完整正文
- 任意会话全文后台监听
- 纯 UIA callback 事件模式在微信上长期稳定可用

推荐当前能力表述：

> **已验证：Session List Watcher 可行；当前聊天正文 watcher 可继续验证；全量正文无点击监听暂不下结论。**

---

## 10.5 Windows 控制台 / stdout 存在中文编码问题
在 `watch_session_list_smoke_test.stdout.log` 中，部分中文摘要曾出现乱码；但直接读取 `session_events.jsonl` 与 `session_previews.json` 时，UTF-8 内容正常。

因此判断：
- watcher 逻辑本身没有问题
- 问题主要出在 Windows 控制台/stdout 编码链路

### 建议
- 以 JSON 文件落盘为主，不依赖 stdout 作为主结果
- 明确统一 UTF-8 写文件
- 如必须打印控制台，显式设置编码或减少中文 stdout 输出

---

## 11. 当前运行命令

## 11.1 标准采集
在 Windows 本地 PowerShell：

```powershell
cd F:\wechat-ui-agent
.\run_collect_snapshot.cmd
```

作用：
- 安装依赖（如未安装）
- 采集当前聊天
- 写出：
  - `agent_snapshot.json`
  - `normalized_current_chat.json`
  - `current_messages.json`
  - `chats_snapshot.json`

---

## 11.2 左侧摘要导出
```powershell
cd F:\wechat-ui-agent
.\run_export_session_previews.cmd
```

作用：
- 专门导出左侧会话列表摘要
- 输出：`session_previews.json`

---

## 11.3 切换到 `BOT-测试` 并采集
```powershell
cd F:\wechat-ui-agent
.\run_switch_collect_bot_test.cmd
```

作用：
- 切换到 `BOT-测试`
- 重新采集当前聊天
- 输出日志：
  - `switch_collect_bot_test.log`
  - `switch_collect_bot_test.status.txt`

---

## 11.4 运行 Session Watcher
```powershell
cd F:\wechat-ui-agent
.\run_watch_session_list.cmd
```

作用：
- 在不点击微信的前提下，持续约 120 秒读取左侧会话列表
- 输出：
  - `session_previews.json`
  - `session_events.jsonl`
  - `watcher_state.json`
  - `watch_session_list.log`

---

## 11.5 运行 Watcher Smoke Test
```powershell
cd F:\wechat-ui-agent
.\run_watch_session_list_smoke_test.cmd
```

作用：
- 跑一轮约 45 秒的 watcher 验证
- 自动统计是否读取到微信、是否捕获到变化
- 输出：
  - `watch_session_list_smoke_test.stdout.log`
  - `watch_session_list_smoke_test.status.txt`

---

## 12. 与 `multi-bot-admin / wechat_ipad` 的对齐建议

参考项目：
- `workspace/projects/multi-bot-admin`
- 已参考结构：
  - `app/services/channel_adapters/wechat_ipad.py`
  - `app/services/wechat_ipad/*`

### 建议对齐点

#### 12.1 Provider 命名
建议新建：
- `wechat_windows_uia`

不要混入 `wechat_ipad`。

#### 12.2 输出口径
建议继续对齐：
- `conversation`
- `message`
- `details`
- `message_type`

#### 12.3 主输入文件
后续 provider 对接建议优先读取：
- `agent_snapshot.json`

#### 12.4 保留 UIA 原始信息
UIA 版必须保留更多 debug/detail 字段，例如：
- `class`
- `friendly`
- `auto_id`
- `rect`
- `source`

因为这类桌面自动化后续调试会高度依赖这些细节。

#### 12.5 Watcher 输出建议作为 provider 的辅助输入
后续 provider / adapter 还应把以下文件视为辅助输入：
- `session_previews.json`
- `session_events.jsonl`
- `watcher_state.json`

这些文件适合支撑：
- 未读检测
- 新消息提醒
- 左侧会话摘要变更流

---

## 13. 建议 Codex 的重构方向

## Phase A：清理当前 agent 结构

### 目标
把现有 PoC 从“脚本集合”整理成更清晰的模块化工程。

### 建议目录
```text
wechat-ui-agent/
  agent/
    __init__.py
    settings.py
    models.py
    wechat_uia.py
    normalizer.py
    snapshot.py
    session_watcher.py
    state_store.py
    diff_engine.py
    event_sink.py
    exporters.py
    actions.py
    utils.py
  output/
  scripts/
    collect_snapshot.py
    export_session_previews.py
    switch_chat.py
    watch_session_list.py
    watch_session_list_smoke_test.py
  README.md
  requirements.txt
```

---

## Phase B：增强 UI 操作可靠性

### 建议任务
1. 给 `find_main_window()` 加重试和多轮探测
2. 给 `switch_to_chat()` 加标题确认机制
3. 给窗口前台化/恢复状态加统一封装
4. 记录结构化 action log

---

## Phase C：把采集和动作分层

建议拆成：
- `collector`：只负责采集
- `navigator`：只负责切换/聚焦/点击
- `normalizer`：只负责输出规范化
- `exporter`：只负责写文件
- `watcher`：只负责持续监听 + 事件输出

---

## Phase D：Session Watcher 工程化

### 目标
将当前 smoke test 级 watcher 重构为正式模块。

### 核心要求
1. 不执行鼠标点击
2. 不切换微信会话
3. 不抢占焦点
4. 长驻读取 `session_list`
5. 比较 `title / preview / unread_hint / timestamp` 变化
6. 稳定输出 `session_previews.json` / `session_events.jsonl` / `watcher_state.json`
7. 支持自动重连、去重、节流、长时间运行

### 推荐 watcher 主循环
```python
prev = load_state()
while True:
    current = collect_session_previews_once()
    events = diff(prev, current)
    append_events(events)
    save_state(current)
    prev = current
    sleep(2)
```

---

## Phase E：provider 对接前准备

### 最终建议主输入
让 provider 优先读：
- `agent_snapshot.json`

### provider 需要的最小字段
```json
{
  "provider": "wechat_windows_uia",
  "conversation": {...},
  "chats": [...],
  "messages": [...],
  "summary": {...},
  "meta": {...}
}
```

### provider 可选辅助输入
```json
{
  "session_previews": [...],
  "session_events": [...]
}
```

---

## 14. 建议 Codex 下一步直接做的任务清单

1. **重构当前 agent 文件结构**
2. **把运行入口从根目录脚本搬到 `scripts/`**
3. **统一 `agent_snapshot.json` 为主输出**
4. **增强 `find_main_window()` 容错**
5. **增强 `switch_to_chat()` 确认机制**
6. **把 `session_previews.json` 也纳入主汇总或标准辅助输出**
7. **补充 sender / direction 提取（如果 UIA 后续能拿到）**
8. **准备 provider adapter 输入映射层**
9. **将 `session_watcher.py` 正式纳入工程结构**
10. **把 `session_previews.json` / `session_events.jsonl` / `watcher_state.json` 定义为标准 watcher 输出**
11. **修复 stdout 中文编码问题，保证日志可读**
12. **将首次基线扫描与后续变化事件分离**
13. **加入窗口丢失后的自动重连和容错重试**
14. **支持更长时间常驻运行，而不是只跑 smoke test**
15. **为后续“当前聊天正文 watcher”预留并行接口**
16. **不要把 watcher 与切换会话逻辑强耦合**

---

## 15. 当前阶段最终结论

这条路线已被验证为 **可行**：

- 微信主窗口可被 UIA 识别
- 会话列表可提取
- 会话摘要可提取
- 当前聊天可识别
- 指定会话可切换（已人工确认能力可达）
- 当前聊天消息可提取
- 结果可稳定写入文件
- `agent_snapshot.json` 已可作为后续 provider 对接的主输入候选
- **无点击 Session Watcher 已通过实测**
- **左侧会话摘要变化与未读变化可被持续捕获并写成事件流**

### 当前建议
Codex 不需要从零探索这件事了，而应该：

> **基于现有 PoC 结果，直接进入“结构重构 + 稳定性增强 + watcher 工程化 + provider 对接准备”阶段。**

---

## 16. 补充：当前已验证成功的真实数据样例

### conversation
```json
{
  "external_id": "uia:chat:BOT-测试",
  "title": "BOT-测试",
  "chat_type": "group",
  "member_count_hint": "(3)"
}
```

### message samples
```json
[
  {
    "message_type": "file",
    "content": "文件\\n测试excel.xls\\n微信电脑版"
  },
  {
    "message_type": "text",
    "content": "哈哈[嘿哈]"
  },
  {
    "message_type": "text",
    "content": "嘿嘿[愉快]"
  },
  {
    "message_type": "text",
    "content": "干活干活，测试一下"
  }
]
```

### session preview sample
```json
{
  "title": "BOT-测试",
  "has_new": true,
  "unread_hint": "[2条]",
  "preview": "水牛: 干活干活，测试一下",
  "timestamp": "20:56"
}
```

### watcher event sample
```json
{
  "ts": "2026-03-30T21:44:09.271985",
  "event": "session_preview_changed",
  "chat": "BOT-测试",
  "preview": "水牛: 还行吧，测试一下",
  "unread_hint": "[1条]",
  "timestamp": "21:44"
}
```

### watcher smoke test summary sample
```json
{
  "watcher_reads_wechat": true,
  "watcher_captured_changes": true,
  "session_preview_items": 11,
  "new_events_captured": 19
}
```

---

## 17. 对 Codex 的明确实现指令建议

如果直接给 Codex 下指令，建议这样描述：

> 基于现有 `F:\wechat-ui-agent` PoC，重构并产品化一个 `Session List Watcher`。该 watcher 必须在不执行鼠标点击、不切换微信会话、不抢占焦点的前提下，持续读取 `mmui::MainWindow` 下的 `session_list`，比较 `title / preview / unread_hint / timestamp` 的变化，并将结果写入 `session_previews.json`、`watcher_state.json`、`session_events.jsonl`。请将其设计为正式模块，具备重试、去重、编码稳定性和长期运行能力。

如果继续推进正文 watcher，可补充说明：

> 当前聊天正文 watcher 可以作为第二阶段任务，但不要宣称“无点击监听所有会话全文”。当前已验证的是会话级 watcher，不是全量正文监听。
