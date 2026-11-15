# 微桥邀请资料接入图谱 Implementation Plan

> **For Codex:** 依据用户已授权方案在当前会话逐项执行；仅进行与本轮改动相关的轻量验证，不启用子代理或另行等待执行方式确认。

**Goal:** 将微桥逐成员入群资料持久保存到现有邀请图谱，并显示可靠进度、来源和时间。

**Architecture:** 复用微桥命令队列、手机回报和轮询维护。群 extra 保存有界同步进度，成员 invitation 保存唯一关系证据；页面只读，运营 POST 启动，回报和维护负责续排。

**Tech Stack:** Python / FastAPI / SQLAlchemy / Jinja / 原生 JavaScript / MariaDB / Docker。

---

### Task 1: 邀请证据与真实时间

**Files:** `app/services/chatroom_invitations.py`、`tests/test_chatroom_invitations.py`。

1. 为新来源增加覆盖优先级、未知快照、空字段保护、请求时间顺序与真实时间展示断言。
2. 扩展唯一 invitation 对象，保存 joined_at、fetched_at、description；新接口未知类型保留 unknown，不用查询时间填充 joined_at。
3. 在图谱视图传递证据详情，不改变 ID 建边、循环处理、深层遍历和成员排序。
4. 执行 `.venv/bin/python -m pytest tests/test_chatroom_invitations.py -q`，预期通过。

### Task 2: 小批量同步与回报

**Files:** 新建 `app/services/wechat_webridge/invitation_sync.py` 与 `tests/test_webridge_invitation_sync.py`；修改 `app/services/wechat_webridge/service.py`、`contract.py`。

1. 新建能力检查、群行锁、同步状态和启动函数；按固定主键上界、最多 3 个在途任务续排。
2. 在已验证微桥结果路径保存资料：严格核对 room_id/wxid、成员归属、当前代次，接受 partial；非法回报标记失败。
3. 回报完成后结算并续排，轮询维护恢复用户已启动的同步与超时任务；普通页面访问不触发。
4. 测试部分成功与时间、批次完成、重复开始、旧代次、错误对象、失败和恢复，执行 `.venv/bin/python -m pytest tests/test_webridge_invitation_sync.py tests/test_webridge_member_directory.py -q`。

### Task 3: 路由和图谱展示

**Files:** `app/main.py`、`app/templates/_chatroom_invitations.html`、`app/templates/chatroom_detail.html`、`app/static/js/chatroom-invitations.js`、`app/static/css/style.css`、`tests/test_chatroom_profile_pages.py`。

1. 增加邀请同步 POST 与状态 GET，共用渠道/群校验；启动要求运营权限、CSRF，状态只读并禁缓存。
2. 将能力与保存状态传入页面；增加同步按钮、进度与更新图谱入口；保留历史补查。
3. 节点详情区分入群时间、资料获取时间、系统证据记录时间，显示接口说明；未知时间明确显示未知。
4. 原生 JS 提交与轮询仅更新进度，图谱更新由用户点击；所有来自接口的文本使用 textContent。
5. 定向页面测试验证权限、CSRF、跨租户/群、GET 无写入和转义；`node --check app/static/js/chatroom-invitations.js` 通过，浏览器检查桌面/手机交互。

### Task 4: 提交与部署验收

1. 对变更文件执行 `git diff --check`，只暂存本轮明确文件，用中文说明改动和实际轻量测试后提交。
2. 先密码 SSH 核对 201 当前提交、容器和 readiness；创建时间戳全库/私有插件备份和旧镜像回滚标记。
3. 通过一次性 Git 凭据推送内网主分支并在 201 快进；从已提交代码离线叠层构建，仅重建 app。本轮不更改数据库结构。
4. 用正常入口对实测群启动同步，确认真实邀请边、描述、未知入群时间、进度完成与成员顺序；核对 HTTP /readyz、资源哈希、容器与 Hindsight 健康。
5. 在 201 维护档案记录变更、提交、备份、验证结果和实际限制，最终回复部署与 commit 结果。

## 本轮实际验证（提交前）

- `tests/test_chatroom_profile_pages.py`：19 passed，覆盖真实页面、POST/状态 GET、权限、CSRF、群归属、部分成功与转义。
- 邀请证据、邀请同步、成员目录、微桥能力与群同步五个模块的定向测试：110 passed、2 deselected。排除项为设备面板和积压摘要的历史测试，均因 fixture 未建立 `raw_webhook_events` 表失败；已在原始 `9c66a0d` 的临时副本分别复现，不修改相关产品代码和断言。
- 初次纯受限环境运行包含 FastAPI 的旧能力测试挂起，主动中断后在正常权限下重跑。新测试 fixture 已补齐可信租户上下文，旧维护测试只增加新邀请恢复任务的隔离桩，不绕过生产租户校验。
- 浏览器使用临时数据库与真实路由，在 1440、1024、390 视口完成 26 项检查；覆盖原有图谱搜索、分支、缩放、拖动、放大弹窗、空状态及新增同步提交、轮询、完成、更新图谱、未知时间和入群说明。页面无横向溢出，浏览器脚本错误 0；预览证据在忽略目录 `.run/chatroom-invitation-sync-preview/`。
- Python AST、`node --check app/static/js/chatroom-invitations.js`、`git diff --check` 通过。没有执行全量回归，临时浏览器数据不作为真机邀请关系证据。
- 部署前只读 SSH 已确认 201 仍为 `9c66a0d`、主分支已跟踪文件干净、数据库唯一头 0040；应用、MariaDB、Hindsight 与 PostgreSQL 均 healthy，六个私有插件在位。真实群同步及最终发布结果另记 201 维护档案。
