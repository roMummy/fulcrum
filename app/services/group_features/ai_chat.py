"""AI 对话:群里 @ 机器人说的不是功能时,按人格直接聊天并记住对方。

和其他群功能不同,它不响应命令也不处理事件,而是由 AI 调度(ai_dispatch)在处理消息时
读取本群的开关与配置:
- 开关:群聊是否启用 AI 对话(群已启用的前提下);
- 独立模型:接口地址、模型名、密钥都可只填需要改的,留空的项沿用租户「AI 助手」里的统一设置;
- 人格:留空沿用租户设置,租户也没设就用平台默认人格。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.entities import Chatroom
from app.services.group_features.registry import ConfigField, GroupFeature, register
from app.services.group_features.store import get_state, has_saved_state
from app.services.security import decrypt_secret

FEATURE = register(
    GroupFeature(
        key="ai_chat",
        name="AI 对话",
        description="群友 @ 机器人说的不是功能时,机器人按人格直接聊天并记住对方。未单独设置时跟随租户「AI 助手」里的聊天开关;需先启用 AI 总开关。",
        fields=(
            ConfigField(
                "api_base", "接口地址", group="独立模型(可选,留空沿用租户统一设置)", hint="例如 https://api.openai.com/v1",
            ),
            ConfigField("model", "模型名称", group="独立模型(可选,留空沿用租户统一设置)", hint="例如 gpt-4o-mini"),
            ConfigField("api_key", "密钥", kind="secret", group="独立模型(可选,留空沿用租户统一设置)", hint="只保存加密后的值,页面不会回显"),
            ConfigField("persona", "人格设定", kind="textarea", group="人格(可选)", hint="留空沿用租户统一人格;最多 2000 字"),
            ConfigField(
                "web_search", "联网搜索", kind="bool", default=True, group="联网搜索",
                hint="对方问到最新消息、天气、价格等时,机器人会先上网查再回答;搜索词会发送到第三方搜索服务(Exa),每天有次数上限",
            ),
            ConfigField(
                "memory_enabled", "记住聊过的内容(长期记忆)", kind="bool", default=True, group="长期记忆",
                hint="按群、按人记住称呼、喜好、计划等,聊天时按话题回想;需要平台已部署记忆服务",
            ),
            ConfigField(
                "memory_all", "记住群里所有聊天(不只 @ 机器人的)", kind="bool", default=False, group="长期记忆",
                depends_on="memory_enabled",
                hint="每攒满 30 条有效消息,或最后一条之后空闲 20 分钟,就把这段群聊整理进记忆;从开启时开始记,不回溯历史。会记录群友没有 @ 机器人的发言,开启前请确认群友知情。",
            ),
        ),
    )
)


@dataclass(frozen=True)
class GroupAiOverride:
    """本群的 AI 对话配置快照(密钥已解密,只在调用模型前短暂持有)。"""

    enabled: bool
    # 这个群是否单独设置过开关;没设置过时聊天开关跟随租户设置。
    explicit: bool = False
    memory_enabled: bool = True
    # 记住群里所有聊天(由后台批量整理),而不只是 @ 机器人的那句。
    memory_all: bool = False
    # 聊天时是否允许联网搜索。
    web_search: bool = True
    api_base: str = ""
    model: str = ""
    api_key: str = ""
    persona: str = ""


def get_override(chatroom: Chatroom) -> GroupAiOverride:
    enabled, config = get_state(chatroom, FEATURE)
    return GroupAiOverride(
        enabled=enabled,
        explicit=has_saved_state(chatroom, FEATURE),
        memory_enabled=bool(config.get("memory_enabled", True)),
        memory_all=bool(config.get("memory_all", False)),
        web_search=bool(config.get("web_search", True)),
        api_base=str(config.get("api_base") or "").strip().rstrip("/"),
        model=str(config.get("model") or "").strip(),
        api_key=decrypt_secret(str(config.get("api_key") or "")) or "",
        persona=str(config.get("persona") or "").strip()[:2000],
    )
