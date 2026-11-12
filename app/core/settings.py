from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # SaaS 项目使用独立品牌名，避免日志、页面和健康检查与原单租户服务混淆。
    app_name: str = "Fulcrum"
    app_env: str = "dev"
    data_dir: str = str(Path(__file__).resolve().parents[2] / "data")
    log_dir: str = str(Path(__file__).resolve().parents[2] / "logs")
    database_url: str = f"sqlite:///{Path(__file__).resolve().parents[2] / 'data' / 'app.db'}"
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_recycle_seconds: int = 1800
    database_pool_timeout_seconds: int = 30
    plugins_dir: str = str(Path(__file__).resolve().parents[2] / "plugins")
    secret_key: str = "change-me"
    # 插件外部服务密钥使用独立的 32 字节 URL-safe Base64 主密钥。
    # 这里不提供默认值，也不复用登录会话 secret_key，避免不同安全边界共用同一把密钥。
    plugin_secret_key: str = Field(default="", validation_alias="ABOT_PLUGIN_SECRET_KEY")
    log_level: str = "INFO"
    log_file_max_bytes: int = 10 * 1024 * 1024
    # RotatingFileHandler 的 backupCount 只统计备份文件。
    # 每类日志(app.log / error.log)共保留 5 个文件:当前文件 + 4 个备份,单个 10M,总占用上限约 50M。
    log_file_backup_count: int = 4
    auth_seed_demo_users: bool | None = None
    auth_bootstrap_admin_username: str = ""
    auth_bootstrap_admin_password: str = ""
    auth_bootstrap_admin_display_name: str = "Bootstrap Admin"
    auth_cookie_max_age_seconds: int = 43200
    auth_cookie_secure: bool | None = None
    auth_cookie_samesite: str = "lax"
    # 验证码挑战只保存在单进程有界内存中，不写数据库；容量和有效期都允许部署环境微调，
    # 服务实现还会再次收敛范围，避免错误配置造成无界内存增长或过长的挑战寿命。
    captcha_cache_max_entries: int = 4096
    captcha_ttl_seconds: int = 300
    tool_api_key: str = ""
    # 对外可访问的 Fulcrum 基址。云端拉取模式下 provider 可能不在 Fulcrum 同一台机器，
    # 如果后台通过 127.0.0.1 登录，必须用这个配置生成文件下载链接，避免 provider 下载时连到自己的本机。
    public_base_url: str = ""
    # 图片库根目录(环境变量 MEDIA_LIBRARY_ROOT):把共享盘或本机目录只读挂进容器后在这里填容器内路径,
    # 例如 /media-library。图片类插件的“图片目录”配置是它下面的子目录;留空则使用各租户自己的插件目录。
    media_library_root: str = ""
    # AI 长期记忆服务(Hindsight)的地址(环境变量 HINDSIGHT_URL),例如 http://hindsight:8888;留空表示不启用长期记忆。
    hindsight_url: str = ""
    # AI 联网搜索使用的 Exa MCP 服务地址(环境变量 EXA_MCP_URL),默认是 Exa 官方免费服务;租户不能改,所以不会被指向内部地址。
    exa_mcp_url: str = "https://mcp.exa.ai/mcp"
    # 本地连接器扫码期间使用的临时 SOCKS 中继监听地址；只服务登录会话，终态立即失效。
    local_proxy_relay_host: str = "127.0.0.1"
    local_proxy_relay_port: int = 9165
    # 首版计费只展示人工付款说明；二维码路径和银行信息均由部署环境配置，不写死到模板。
    billing_payment_title: str = "Fulcrum 人工付款"
    billing_payment_instructions: str = "请完成转账后填写交易流水号并上传付款凭证。"
    billing_payment_qr_image: str = ""
    billing_payment_bank_details: str = ""
    billing_payment_proof_max_bytes: int = 5 * 1024 * 1024
    # 第三方支付回调用独立签名密钥；为空时不开放自动履约入口，继续使用人工审核。
    payment_webhook_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def is_dev_like(self) -> bool:
        return self.app_env.strip().lower() in {"dev", "local", "demo", "test"}

    def validate_runtime_secrets(self) -> None:
        """非开发环境必须配置真实的 SECRET_KEY,否则拒绝启动。

        SECRET_KEY 用来签名登录 Cookie、给密码哈希加盐、派生机器人令牌和 API 密钥的加密密钥,也用于验证码。
        默认值写在公开源码里,任何人都知道;忘了配置的部署等于没有保护,所以必须在启动时拦住。
        开发/测试环境(APP_ENV 为 dev/local/demo/test)不受影响。
        """

        if self.is_dev_like:
            return
        key = str(self.secret_key or "").strip()
        if not key or key.lower().startswith("change-") or len(key) < 32:
            raise RuntimeError(
                "SECRET_KEY 未配置或仍是示例值(至少 32 位随机字符串);非开发环境拒绝启动。"
                "可用 `python -c \"import secrets; print(secrets.token_urlsafe(48))\"` 生成。"
            )

    @property
    def should_seed_demo_users(self) -> bool:
        if self.auth_seed_demo_users is not None:
            return self.auth_seed_demo_users
        return False

    @property
    def should_use_secure_cookie(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return not self.is_dev_like

    @property
    def has_bootstrap_admin_config(self) -> bool:
        return bool(self.auth_bootstrap_admin_username.strip() and self.auth_bootstrap_admin_password)


settings = Settings()
settings.validate_runtime_secrets()
Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
Path(settings.plugins_dir).mkdir(parents=True, exist_ok=True)
