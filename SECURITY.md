# 安全策略 / Security Policy

## 报告漏洞 / Reporting a vulnerability

请**不要**在公开 Issue 里披露安全漏洞。请使用 GitHub 的私密漏洞报告：仓库页面 → **Security** → **Report a vulnerability**。

Please do **not** open a public issue for security problems. Use GitHub's private vulnerability reporting:
the repository's **Security** tab → **Report a vulnerability**.

报告里请尽量包含：影响范围、复现步骤、受影响的版本或提交、可能的修复建议。我们会尽快确认并回复。

## 部署前必读 / Before you deploy

- `SECRET_KEY` 必须设置为至少 32 位的随机字符串。非开发环境（`APP_ENV` 不是 `dev/local/demo/test`）使用示例值、空值或过短的值时，应用会拒绝启动。
- 插件敏感配置使用独立的 AES-GCM 主密钥 `ABOT_PLUGIN_SECRET_KEY`，不能复用 `SECRET_KEY`，没有默认值。
- 不要把 `.env`、`.env.docker`、数据库导出（`backups/`）提交到仓库。仓库的 `.gitignore` 已排除它们。
- 生产环境请放在带 TLS 的反向代理之后，并保持 `AUTH_COOKIE_SECURE` 为默认（非开发环境自动启用）。
- 平台管理员初始账号只在首次安装时通过环境变量或安装向导创建，请使用强密码并及时修改。

## 支持的版本 / Supported versions

项目仍在快速演进，安全修复只合入 `main` 分支。 / The project evolves quickly; security fixes land on `main` only.
