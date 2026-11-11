# 贡献指南 / Contributing

感谢你愿意参与 Fulcrum！ / Thanks for your interest in Fulcrum!

## 开发环境

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # 开发环境默认 APP_ENV=dev，可直接使用示例配置
```

## 运行测试

部分测试会修改全局配置，放在同一个进程里会互相污染，所以请**逐文件隔离运行**。脚本会为每个测试文件使用独立进程和独立的测试数据库：

```bash
scripts/run_tests_isolated.sh                              # 运行全部 tests/test_*.py
scripts/run_tests_isolated.sh .github/ci-test-files.txt    # 只运行 CI 使用的清单
JOBS=8 TIMEOUT=300 scripts/run_tests_isolated.sh           # 调整并行数和单文件超时
```

只想跑单个文件时，请指定独立的测试数据库（部分测试会拒绝在非测试库上清表）：

```bash
APP_ENV=test DATA_DIR="$PWD/.run/t" DATABASE_URL="sqlite:///$PWD/.run/t/test.db" LOG_DIR="$PWD/.run/t/logs" \
python -m pytest tests/test_xxx.py -q
```

CI 只运行 `.github/ci-test-files.txt` 里已验证稳定通过的文件；少数历史测试文件仍有失败，正在整理，修好后会加入清单。
提交前至少运行与你改动直接相关的测试。 / Run at least the tests directly related to your change.

## 代码约定

- 注释和提交说明可以使用中文或英文；新增代码请写清楚"为什么这样做"的注释。
- 保持单一清晰的业务路径：删除失效的旧入口、旧调用链和旧测试，不为已废弃的页面、接口、字段保留兼容分支。
- 数据库结构变更必须附带 Alembic 迁移，并保证迁移在 SQLite 和 MariaDB 上都能执行。
- 涉及租户数据的查询必须经过租户会话，不要绕过租户隔离。
- 不要提交密钥、真实账号、真实群 ID 或个人信息；测试数据请使用虚构值。

## 提交 Pull Request

1. 从 `main` 创建分支，保持改动聚焦。
2. 说明改了什么、为什么改、怎么验证的（运行了哪些测试）。
3. 如果改动了界面，请附上截图。

## 私有插件

部分只在内网使用的插件不随本仓库发布。需要在本地保留这类插件目录时，把目录名写进环境变量
`FULCRUM_PRIVATE_PLUGINS`（逗号分隔），目录校验脚本和测试会忽略它们。
