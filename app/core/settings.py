from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # SaaS 项目使用独立品牌名，避免日志、页面和健康检查与原单租户服务混淆。
    app_name: str = "Multi Bot SaaS"
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
    tool_api_key: str = ""
    # 对外可访问的 MBA 基址。云端拉取模式下 provider 可能不在 MBA 同一台机器，
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
    billing_payment_title: str = "曜枢人工付款"
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
Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
Path(settings.plugins_dir).mkdir(parents=True, exist_ok=True)
