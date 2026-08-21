from sqlalchemy import UniqueConstraint

from app.models.entities import (
    AdminUser,
    AlertRecord,
    AuditLog,
    Bot,
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    CustomerOperationRequest,
    ErrorLog,
    GroupPluginScope,
    HealthCheckRecord,
    Message,
    MessageActivityHourly,
    MessageAttachment,
    OpenApiApp,
    OpenApiAppChannel,
    Plugin,
    PluginActivityHourly,
    PluginExecutionLog,
    PointsAccount,
    PointsPrisonRecord,
    PointsTransaction,
    RawWebhookEvent,
    SignInHistory,
    SignInRecord,
    TaskJob,
    WeatherSubscription,
    WorkWechatContact,
    WorkWechatContactRelation,
)
from app.models.mixins import TenantOwnedMixin
from app.models.saas import BotBinding


# 该清单是共享数据库中的业务隔离边界。新增业务表时如果忘记加入租户混入，
# 测试会要求开发者显式决定它属于租户数据还是平台全局数据，避免静默漏隔离。
TENANT_OWNED_MODELS = {
    Bot,
    Channel,
    BotPlugin,
    Conversation,
    Chatroom,
    ChatroomMember,
    WorkWechatContact,
    WorkWechatContactRelation,
    CustomerOperationRequest,
    OpenApiApp,
    OpenApiAppChannel,
    GroupPluginScope,
    RawWebhookEvent,
    Message,
    MessageAttachment,
    TaskJob,
    WeatherSubscription,
    SignInRecord,
    SignInHistory,
    PointsAccount,
    PointsTransaction,
    PointsPrisonRecord,
    ErrorLog,
    PluginExecutionLog,
    MessageActivityHourly,
    PluginActivityHourly,
    HealthCheckRecord,
    AlertRecord,
    AuditLog,
}


def _unique_columns(model: type) -> set[tuple[str, ...]]:
    """提取模型声明的唯一约束列，忽略数据库方言生成的具体名称。"""

    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_all_business_models_are_tenant_owned() -> None:
    """全部正式业务模型必须进入 SQLAlchemy 租户过滤和写入校验。"""

    assert all(issubclass(model, TenantOwnedMixin) for model in TENANT_OWNED_MODELS)

    # 插件目录是全平台共享定义；旧 AdminUser 只保留迁移兼容，均不能误加租户过滤。
    assert not issubclass(Plugin, TenantOwnedMixin)
    assert not issubclass(AdminUser, TenantOwnedMixin)


def test_business_identity_constraints_are_tenant_local() -> None:
    """业务名称、开放密钥和幂等编号只能在所属租户内保持唯一。"""

    assert ("tenant_id", "name") in _unique_columns(Bot)
    assert ("tenant_id", "app_key") in _unique_columns(OpenApiApp)
    assert ("tenant_id", "app_id", "channel_id") in _unique_columns(OpenApiAppChannel)
    assert (
        "tenant_id",
        "channel_id",
        "request_id",
    ) in _unique_columns(CustomerOperationRequest)


def test_bot_binding_links_to_actual_bot_and_channel() -> None:
    """商业额度必须能够定位到真正运行的机器人和主渠道。"""

    bot_foreign_keys = {str(key.column) for key in BotBinding.__table__.c.bot_id.foreign_keys}
    channel_foreign_keys = {str(key.column) for key in BotBinding.__table__.c.channel_id.foreign_keys}

    assert bot_foreign_keys == {"bots.id"}
    assert channel_foreign_keys == {"channels.id"}
    assert BotBinding.bot.property.mapper.class_ is Bot
    assert BotBinding.channel.property.mapper.class_ is Channel
