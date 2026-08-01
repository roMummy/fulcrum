# 旧系统数据导入

本工具把旧 `Multi Bot Admin` 的核心业务数据导入一个已经存在的 SaaS 租户。来源数据库不会被修改，目标数据库必须先通过 `alembic upgrade head` 升级到当前版本。

## 导入范围

- 机器人、企业微信渠道
- 企业微信联系人、群聊
- 会话、消息
- 任务
- 机器人插件绑定

插件定义不会重复创建。工具按旧库插件 `code` 绑定目标库已经同步的全局插件。Provider 节点、设备凭证和任务租约不会从旧系统推断，导入后需要重新创建节点并绑定企微账号。

## 安全边界

- `--tenant-code` 必填，工具不会创建或猜测目标租户。
- SQLite 来源使用 `mode=ro` 强制只读；MariaDB/MySQL/PostgreSQL 来源会把连接会话设置为只读，仍建议使用专用只读账号。
- 报告和批次审计不保存来源连接串、数据库密码、渠道密文、插件配置或任务负载。
- 渠道密文只按密文迁移。导入报告出现 `channel_credentials_require_rotation` 时，需要在新系统重新配置或确认两边使用兼容的加密密钥。
- 插件配置按不透明 JSON 迁移。出现 `plugin_config_review_required` 时，需要逐项检查其中的凭证并按需轮换。
- SaaS 首期只允许企业微信。来源存在其他渠道类型时，预检直接失败且目标库零写入。

## 执行前准备

1. 备份目标数据库。
2. 为旧数据库准备只读账号；SQLite 文件至少保留一份不可修改副本。
3. 在目标环境配置正确的 `DATABASE_URL`，不要把连接串写入命令历史或报告。
4. 运行 `.venv/bin/alembic upgrade head`。
5. 确认目标租户已由平台后台创建，且插件目录同步完成。

## 校验模式

先运行 dry-run。该模式会检查表结构、来源外键、企微渠道范围、目标租户、插件 code 和目标机器人名称冲突，但不会写入目标业务表或导入批次表。

```bash
.venv/bin/python scripts/import_legacy_tenant.py \
  --source-url 'sqlite:////absolute/path/legacy.db' \
  --tenant-code self-operated \
  --report-path .run/legacy-import-dry-run.json \
  --dry-run
```

## 正式导入

确认 dry-run 的 `success` 为 `true` 且完成目标库备份后，去掉 `--dry-run`：

```bash
.venv/bin/python scripts/import_legacy_tenant.py \
  --source-url 'sqlite:////absolute/path/legacy.db' \
  --tenant-code self-operated \
  --report-path .run/legacy-import.json
```

正式导入在一个事务中完成。任一核心表写入或数量校验失败时整体回滚。成功后写入 `legacy_import_batches`，相同租户和来源指纹再次正式执行会返回 `duplicate_import`。

## 报告和退出码

报告为 JSON，包含模式、目标租户编码、来源指纹、逐表读取/写入/跳过/错误数量、固定警告代码和错误代码。

- `0`：校验或正式导入成功。
- `2`：校验失败、重复导入或事务回滚。

工具不会把来源 URL 或凭证写入标准输出。排障时应以报告错误代码和数据库日志为准，不能粘贴真实密码、渠道密钥或 Provider 设备令牌。
