# 201 Hindsight 启动故障处理

日期：2026-11-14。用户在邀请关系图谱研发期间授权检查并处理 201 的 Hindsight 无法启动问题。

## 证据与根因

- PostgreSQL 健康且内存充足，Hindsight 在加载本地重排模型时失败，修复前观察到累计重启 107 次。
- 日志在 `AutoConfig.from_pretrained` 报告远端连接及缓存读取失败，API 就绪探测被重置。
- 核对实际运行镜像后，确认 `hindsight-ai/hs-rerank-1` 的配置、分词器及约 228 MB 权重已完整预置；模型文件并非需要重新下载。
- 在同一镜像、无网络、隔离临时文件系统的容器里设置 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`，实际初始化生产使用的 `LocalSTCrossEncoder` 成功。
- 因此修复启动时对远端模型元数据的依赖，沿用已有模型，不更换重排器或修改已存记忆。

## 变更与保护

- `deploy/hindsight/docker-compose.yml` 移除启动时依赖镜像站的配置，启用 Hugging Face / Transformers 离线加载，并增加 API readiness 健康检查。
- 201 对应独立项目位于 `/home/liuwei/hindsight`。修改前已备份 Compose、环境配置及完整 PostgreSQL 到 `/home/liuwei/hindsight/backups/offline-startup-20261114-140420/`。
- 数据库备份 `database.sql.gz` 为 646,044 字节，gzip 完整性检查通过；配置及环境备份权限 600，未将其内容提交或输出。
- 使用 `docker compose config --quiet` 预检，再仅重建 `hindsight` 服务；既有 PostgreSQL 容器启动时间 `2026-11-14T05:42:50.994973434Z` 保持不变。
- API 继续只绑定本机 8888；管理页继续使用 9999 和既有登录密钥，网络与鉴权边界保持原配置。

## 实际验证

- 隔离容器中的完整重排器初始化成功，不连接业务数据库或外网。
- 修复后 `hindsight-hindsight-1`、`hindsight-hindsight-db-1` 均 healthy；Hindsight 重启计数为 0。
- 201 本机 API `/health/ready`、管理页 `/` 及应用 `/readyz` 均返回 HTTP 200。
- 从运行中的 Multi-Bot SaaS 容器访问 `http://hindsight:8888/health/ready` 返回 200。
- 本记录不将就绪检查等同于真实微信聊天、记忆写入或完整召回回归；后续部署验收继续核对服务稳定性。
