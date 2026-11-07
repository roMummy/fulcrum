# 成人插件 SaaS 原生迁移验证记录

日期：2026-11-07

本次交付按已确认的 SaaS 原生重构和四批顺序完成六个非 AI 成人业务插件迁移：`beautyleg`、`video`、`video_man`、`fanhao_search`、`xiuren_image`、`sehuatang_push`。插件均使用当前 `config.toml + plugin.py` 目录协议，默认关闭，并通过租户机器人绑定配置启用。

## 迁移结果

- 目录插件总数：44。
- A-BOT 目标插件数：42。
- SaaS 自有插件数：2。
- 迁移目录校验错误：0。
- 成人插件调度入口：`xiuren_image.refresh_resources`、`sehuatang_push.daily_pdf_push`。
- 成人插件不进入营销页、公共演示数据、默认套餐或平台公共推荐。

## 已执行的轻量验证

以下检查在本地虚拟环境执行，未调用真实渠道发送：

```text
tests/test_plugin_media_boundaries.py
tests/test_plugin_beautyleg.py
tests/test_plugin_video.py
tests/test_plugin_video_man.py
tests/test_plugin_fanhao_search.py
tests/test_plugin_xiuren_image.py
tests/test_plugin_sehuatang_push.py
tests/test_plugin_migration_catalog.py
tests/test_plugin_native_boundaries.py
tests/test_plugin_scheduler.py
```

并执行：

```text
python scripts/verify_plugin_migration.py
python -m compileall -q plugins app/services/plugin_runtime
git diff --check
```

定向测试覆盖目录发现、TOML/AST 契约、租户资源路径、媒体大小和类型边界、标准回复、积分与限流边界、图片缓存调度、PDF HTML 转义、浏览器缺失稳定失败、每日 PDF 目标会话筛选、任务幂等和平台原生能力排除。

## 未在本地模拟测试中证明的内容

- JavBus 或其他真实第三方站点的可达性、反爬策略和页面结构长期稳定性。
- 目标运行镜像是否已安装 Playwright Chromium、中文字体和 PDF 所需系统库。
- 真实代理池、代理认证和租户自定义代理的可用性。
- 目标渠道对统一文件出站队列生成的 PDF 的实际发送结果。
- 全量测试、完整端到端测试、压力测试和生产环境验收尚未执行。

这些内容属于部署环境和外部服务验收，不通过旧 A-BOT 运行时兼容分支解决；缺少依赖时插件返回稳定失败状态并保留当前租户边界。
