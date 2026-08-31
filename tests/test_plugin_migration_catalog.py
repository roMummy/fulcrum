from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PROJECT_ROOT / "plugins"

# 已有插件必须始终保留真实目录。这里锁定确认过的业务范围，避免后续批量迁移时
# 误删已经接入的插件，或者用名称不同的兼容目录替代当前唯一业务入口。
EXISTING_PLUGIN_CODES = {
    "ai_auto_response",
    "daily_news",
    "dify",
    "douyin_parser",
    "douyu",
    "duanju_search",
    "game_task",
    "global_news",
    "group_auto_invite",
    "group_member_change",
    "group_virtual",
    "guess_song",
    "inactive_rank",
    "jd_sign_token",
    "lucky_pot",
    "message_push_task",
    "message_sign",
    "message_summary",
    "music",
    "point_trade",
    "weather",
}

# 新插件会按计划逐个加入，因此目录集合只要求它们是允许出现的代码，不在基础提交里
# 强迫 15 个插件一次性全部存在。最终完整性由交付阶段的迁移验证脚本负责。
NEW_PLUGIN_CODES = {
    "ai_gen_image",
    "codex_quota_allocator",
    "daily_ranking",
    "dota2_stats",
    "epic_free",
    "fun_command_play",
    "github_opengraph",
    "gscore_adapter",
    "maibot_adapter",
    "member_context",
    "member_roast",
    "newapi_coupon",
    "robot_menu",
    "value_rank",
    "xiuxian",
}

SAAS_OWN_PLUGIN_CODES = {"customer_operations", "supplier_order_flow"}

# 这些能力已经明确排除或上移为平台原生能力。测试直接禁止同名目录出现，防止
# 研发过程中为了快速复用又把 A-BOT 单机管理链路或不需要的内容带回 SaaS。
FORBIDDEN_PLUGIN_CODES = {
    "beautyleg",
    "fanhao_search",
    "plugin_manager",
    "sehuatang_push",
    "stats_collector",
    "system_updater",
    "trendradar_permission",
    "video",
    "video_man",
    "xiuren_image",
}


def _directory_plugin_codes() -> set[str]:
    """只识别同时包含目录声明和执行入口的真实插件目录。"""

    return {
        path.name
        for path in PLUGINS_ROOT.iterdir()
        if path.is_dir() and (path / "config.toml").is_file() and (path / "plugin.py").is_file()
    }


def test_plugin_directories_stay_inside_confirmed_migration_scope() -> None:
    """目录插件只能来自已确认范围，不允许迁移过程顺手加入其他旧插件。"""

    actual = _directory_plugin_codes()
    allowed = EXISTING_PLUGIN_CODES | NEW_PLUGIN_CODES | SAAS_OWN_PLUGIN_CODES

    assert EXISTING_PLUGIN_CODES <= actual
    assert actual <= allowed
    assert not actual & FORBIDDEN_PLUGIN_CODES


def test_existing_plugin_directories_have_single_saas_entrypoint() -> None:
    """已有插件统一使用 plugin.py，不能重新引入 A-BOT main.py 双入口。"""

    for plugin_code in EXISTING_PLUGIN_CODES:
        plugin_dir = PLUGINS_ROOT / plugin_code
        assert (plugin_dir / "config.toml").is_file()
        assert (plugin_dir / "plugin.py").is_file()
        assert not (plugin_dir / "main.py").exists()
