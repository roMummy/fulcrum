# 欢迎卡片头像时序修复实施计划

**目标：** 修复群欢迎卡片早于新人头像同步而生成的问题，保留现有二选一设置和单一发送路径。

**架构：** 在现有插件任务执行前按需短暂延期；业务模块判断是否等待，工作线程只更新同一个任务的领取状态。资料同步完成后仍由原群功能生成并发送通知；不新增任务类型。

**技术：** Python、SQLAlchemy、现有 TaskJob / IdentityDirectory、pytest。

## 任务 1：复现并固定边界

- 新建 `tests/test_welcome_avatar_wait.py`，复用群功能内存数据夹具。
- 构造新人入群、目录 dirty、欢迎卡片选新人头像但资料尚为空的事件，断言原任务让出队列且没有发送通知；先运行该用例确认当前版本失败。
- 增加同步后恢复、固定 URL 不等待、十五秒上限和十分钟过期等直接场景。

## 任务 2：最小业务判断

- 修改 `app/services/group_features/member_change.py`：提取共用头像模板判断和新人资料读取；复用头像合法性处理；新增有界等待时刻判断，检查当前配置、入群事件和目录同步状态。
- 修改 `app/services/async_workers.py`：在领取的插件任务实际执行前调用等待判断；需要延期则同一个任务恢复 pending，设置一秒后的 available_at，清理领取状态并还原这次纯等待的 attempts，提交后立即让出线程。给出中文注释和可诊断原因。
- 不改通用 TaskJob 表、微桥协议或成员目录任务，不增加旧渠道兼容。

## 任务 3：轻量验证与说明

- 顺序执行 `tests/test_welcome_avatar_wait.py`、`tests/test_group_features.py`，然后执行 `tests/test_webridge_member_directory.py`、`tests/test_inbound_message_age.py` 及直接相关的租户后台任务测试。
- 对变更 Python 文件执行 AST、`git diff --check`；只记录实际结果，不宣称全量验证。
- 更新 `docs/wechat-webridge-directory.md` 的等待与缺图边界，追加本计划实施记录；将线上只读排查写入 201 服务器维护档案。
- 明确只提交本轮源码、测试和文档，排除用户原有群功能草稿，使用中文 commit；本轮不自动部署或发送生产消息。
