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

## 实施结果与实际验证

- 按真实任务入口先复现失败：新成员没有头像时原任务立刻完成并发出空缩略图卡片；加入延期逻辑后，同一测试在正式成员资料保存后仅发送一次带新人头像的卡片。
- 原插件任务等待时恢复 pending，一秒后再领取，清除领取状态并还原这次纯等待的 attempts；没有新建插件任务或重复发起成员读取，也没有阻塞工作线程。等待原因 `member_avatar_sync` 写入任务和原始事件摘要，截止时间固定为任务创建后十五秒。
- 生成与等待共用实际头像需求、当前新人名单、身份目录和头像地址校验；固定 URL 不查询头像也不等待。已同步头像和好友头像可直接使用，来源选择与既有头像变量走同一路径。
- 同步完成后即便没有头像也立即按既定缺图规则发送；等待超时不补发。等待期间关闭配置或原始消息超过十分钟会跳过对应执行；对于等待期间已离群的新成员，发送前按当前成员状态过滤，避免迟到欢迎。该退群边界也先复现失败，再完成修正。
- 最终轻量验证命令：`.venv/bin/python -m pytest -q tests/test_welcome_avatar_wait.py tests/test_group_features.py tests/test_webridge_member_directory.py tests/test_inbound_message_age.py tests/test_tenant_machine_interfaces.py::test_worker_rejects_cross_tenant_channel_before_provider_call tests/test_tenant_machine_interfaces.py::test_worker_recovers_running_jobs_inside_each_tenant_scope --tb=short`，结果 **119 passed in 16.17s**。
- 三个变更 Python 文件 AST 及 `git diff --check` 通过。未执行全量测试，未新增依赖、数据库迁移或旧渠道兼容分支；未修改用户原有未跟踪草稿。
- 线上仅做 201 只读查询，没有重放事件、补发历史卡片、下发手机命令、保存群配置或部署。没有用真实微信群验证修复后的图片实际展示；后续发布后仍应以新入群事件的实际发送结果验收。
