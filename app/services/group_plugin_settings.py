"""群详情的结构化插件设置：只保存本群差异，不向页面暴露机器人密钥。"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Chatroom, GroupPluginScope
from app.services.plugin_commands import command_overrides, command_triggers
from app.services.plugin_directory import SKIP_MESSAGE_PLUGIN_TYPES, get_discovered_plugin
from app.services.plugin_scopes import group_plugin_scope_enabled
from app.services.plugin_schemas import (
    _parse_field_value, build_plugin_config_view, deep_merge_config, diff_config_override,
    get_plugin_config_schema, parse_config_json,
)
from app.services.wechat_ipad.chatrooms import update_group_plugin_scope_config_override


TRIGGER_FIELDS = {'commands', 'keywords', 'require_mention', 'command_overrides'}


def _sensitive_fields(code: str) -> set[str]:
    """表单声明和清单声明共同限定敏感字段，未生成表单的密钥也不能回显或下放到群。"""

    discovered = get_discovered_plugin(code)
    schema = get_plugin_config_schema(code)
    names = set(discovered.secret_fields) if discovered else set()
    if schema:
        names.update(field.name for field in schema.fields if field.sensitive)
    return names


def _base_config(binding: BotPlugin) -> dict[str, Any]:
    """字段声明和清单提供默认值；机器人配置覆盖默认，密钥从普通配置剔除。"""

    code = binding.plugin.code if binding.plugin else ''
    discovered = get_discovered_plugin(code)
    schema = get_plugin_config_schema(code)
    defaults = dict(discovered.runtime_config) if discovered else {}
    if discovered:
        # 匹配声明也是继承默认值的一部分；完整表单提交原有 @ 开关或关键词时，
        # 不能因为 runtime 未重复声明它们就误写成本群覆盖。
        defaults.setdefault('commands', list(discovered.matcher.commands))
        defaults.setdefault('keywords', list(discovered.matcher.keywords))
        defaults.setdefault('require_mention', discovered.matcher.require_mention)
    for field in schema.fields if schema else ():
        defaults.setdefault(field.name, field.default)
    config = deep_merge_config(defaults, parse_config_json(binding.config_json))
    for name in _sensitive_fields(code):
        config.pop(name, None)
    return config


def _commands(discovered, base: Mapping[str, Any]) -> tuple[str, ...]:
    raw = base.get('commands')
    return tuple(str(item) for item in raw if isinstance(item, str) and item.strip()) if isinstance(raw, list) and raw else discovered.matcher.commands


def _scope(db: Session, channel: Channel, room: Chatroom, binding_id: int) -> GroupPluginScope | None:
    return db.scalar(select(GroupPluginScope).where(GroupPluginScope.channel_id == channel.id,
        GroupPluginScope.chatroom_id == room.id, GroupPluginScope.bot_plugin_id == binding_id))


def _binding(db: Session, channel: Channel, room: Chatroom, binding_id: int) -> BotPlugin:
    """路由已经限定当前租户，服务层仍核对渠道、群和机器人绑定的完整归属。"""

    binding = db.get(BotPlugin, binding_id)
    if (binding is None or binding.status == 'unbound' or binding.plugin is None
            or room.channel_id != channel.id or room.bot_id != channel.bot_id
            or binding.bot_id != channel.bot_id
            or int(binding.tenant_id) != int(channel.tenant_id) or int(room.tenant_id) != int(channel.tenant_id)):
        raise ValueError('插件绑定不属于当前群的机器人，或已经解绑')
    return binding


def group_plugin_settings_view(db: Session, channel: Channel, room: Chatroom, binding: BotPlugin) -> dict[str, Any]:
    """生成卡片、表单与保存回报共用的非敏感视图，保证摘要对应真正生效值。"""

    plugin = binding.plugin
    discovered = get_discovered_plugin(plugin.code if plugin else '')
    scope = _scope(db, channel, room, binding.id)
    base = _base_config(binding)
    effective = deep_merge_config(base, parse_config_json(scope.config_override_json) if scope else {})
    schema = get_plugin_config_schema(plugin.code if plugin else '')
    sensitive = _sensitive_fields(plugin.code if plugin else '')
    for name in sensitive:
        effective.pop(name, None)
    config_view = build_plugin_config_view(plugin.code if plugin else '', json.dumps(effective, ensure_ascii=False))
    # 本群页面只需要可编辑字段，不发送机器人完整配置 JSON 或隐藏的预览副本。
    config_view = {'mode':'schema',
        'fields':[field for field in config_view['fields'] if not field['sensitive'] and field['name'] not in TRIGGER_FIELDS | sensitive]}
    commands = _commands(discovered, base) if discovered else ()
    mapping = command_overrides(effective)
    inherited_mapping = command_overrides(base)
    message_plugin = bool(discovered and discovered.manifest.plugin_type not in SKIP_MESSAGE_PLUGIN_TYPES)
    # 声明固定命令的插件按严格命令模式执行，不提供实际不会生效的通用关键词框。
    keywords_supported = bool(message_plugin and not commands and (discovered.matcher.keywords or (schema and any(field.name == 'keywords' for field in schema.fields))))
    require_mention = bool(effective.get('require_mention', discovered.matcher.require_mention if discovered else False))
    return {
        'available':bool(discovered and discovered.is_valid),
        'version':plugin.version if plugin else '', 'plugin_id':plugin.id if plugin else 0,
        'description':(plugin.description if plugin else '') or (discovered.manifest.description if discovered else '') or '',
        'binding_enabled':binding.status == 'enabled',
        'custom':bool(scope and (scope.scope_status != 'inherited' or scope.config_override_json)),
        'config_custom':bool(scope and scope.config_override_json),
        'enabled':group_plugin_scope_enabled(binding, scope),
        'message_plugin':message_plugin, 'keywords_supported':keywords_supported,
        'keywords_value':', '.join(effective.get('keywords', discovered.matcher.keywords if discovered else ()) or ()),
        'require_mention':require_mention,
        'commands':[{'index':index, 'command':command,
                     'value':', '.join(mapping.get(command) or inherited_mapping.get(command) or (command,)),
                     'placeholder':', '.join(inherited_mapping.get(command) or (command,))}
                    for index, command in enumerate(commands)],
        'triggers':list(command_triggers(commands, effective)),
        'config_view':config_view,
    }


def attach_group_plugin_settings(db: Session, channel: Channel, room: Chatroom, items: list[dict[str, Any]]) -> None:
    """各渠道继续共享当前群详情；只给实际存在的绑定补齐一个结构化视图。"""

    for item in items:
        binding = db.get(BotPlugin, item['binding_id'])
        if binding is not None:
            item['settings'] = group_plugin_settings_view(db, channel, room, binding)


def _words(raw: Any) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in re.split(r'[,，\r\n]+', str(raw or '')) if part.strip()))


def _boolean(raw: Any) -> bool:
    normalized = str(raw).strip().lower()
    if normalized not in {'true','false','1','0','on','off'}:
        raise ValueError('开关值无效，请刷新后重试')
    return normalized in {'true','1','on'}


def _field_value(field, raw: Any) -> Any:
    """沿用插件字段类型和数值边界，同时保留本群明确保存的空字符串及空列表。"""

    value = str(raw or '').strip()
    if field.required and not value:
        raise ValueError(f'请填写「{field.label}」')
    if field.field_type == 'boolean':
        return _boolean(raw)
    if field.field_type == 'list':
        return _words(value)
    if field.field_type == 'select' and value not in {option.value for option in field.options}:
        raise ValueError(f'「{field.label}」选项无效')
    if field.field_type == 'number' and value:
        try:
            if not math.isfinite(float(value)):
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError(f'「{field.label}」需要填写有效数字') from None
    if field.field_type not in {'number','time','select'}:
        if len(value) > 20000:
            raise ValueError(f'「{field.label}」内容过长')
        return value
    return _parse_field_value(field.field_type, value, field.default, field.minimum, field.maximum)


def _check_commands(db: Session, channel: Channel, room: Chatroom, binding: BotPlugin, commands: tuple[str, ...], target: Mapping[str, Any]) -> None:
    """不同业务的触发词不能歧义；仅对用户设置的词检查同群其他启用插件。"""

    mapping = command_overrides(target)
    seen: dict[str, str] = {}
    for command in commands:
        for word in mapping.get(command) or (command,):
            if len(word) > 64 or any(char.isspace() for char in word) or word.startswith(('/', '#')):
                raise ValueError('触发词最多 64 个字符，不含空格、换行或 /、# 前缀')
            if word.casefold() in seen and seen[word.casefold()] != command:
                raise ValueError(f'触发词「{word}」重复，请为不同指令分别设置')
            seen[word.casefold()] = command
    changed = {word.casefold() for words in mapping.values() for word in words}
    if not changed:
        return
    for other in db.scalars(select(BotPlugin).where(BotPlugin.bot_id == binding.bot_id, BotPlugin.id != binding.id, BotPlugin.status == 'enabled')).all():
        scope = _scope(db, channel, room, other.id)
        if not group_plugin_scope_enabled(other, scope) or not other.plugin:
            continue
        discovered = get_discovered_plugin(other.plugin.code)
        if not discovered or not discovered.is_valid or discovered.manifest.plugin_type in SKIP_MESSAGE_PLUGIN_TYPES:
            continue
        config = deep_merge_config(_base_config(other), parse_config_json(scope.config_override_json) if scope else {})
        conflict = changed.intersection(word.casefold() for word in command_triggers(_commands(discovered, config), config))
        if conflict:
            raise ValueError(f'触发词「{sorted(conflict)[0]}」已由本群「{other.plugin.display_name}」使用')


def save_group_plugin_settings(db: Session, channel: Channel, room: Chatroom, binding_id: int,
                               form: Mapping[str, Any], operator_id: int) -> BotPlugin:
    """只接收页面声明的字段，先验证完整目标再一次性保存本群差异。"""

    binding = _binding(db, channel, room, binding_id)
    view = group_plugin_settings_view(db, channel, room, binding)
    if not view['available']:
        raise ValueError('插件当前不可用，无法保存设置')
    base = _base_config(binding)
    scope = _scope(db, channel, room, binding.id)
    target = deep_merge_config(base, parse_config_json(scope.config_override_json) if scope else {})
    schema = get_plugin_config_schema(binding.plugin.code)
    # 群配置不能持有密钥，保存时直接清理历史覆盖里的敏感同名字段。
    sensitive = _sensitive_fields(binding.plugin.code)
    for name in sensitive:
        target.pop(name, None)
    editable = {field.name:field for field in schema.fields if not field.sensitive and field.name not in TRIGGER_FIELDS | sensitive} if schema else {}
    allowed = {'csrf_token'} | {f'schema__{name}' for name in editable} | {f'command__{item["index"]}' for item in view['commands']}
    if view['message_plugin']:
        allowed.add('require_mention')
    if view['keywords_supported']:
        allowed.add('keywords')
    if set(form) - allowed:
        raise ValueError('提交包含本群不支持的配置字段，请刷新页面后重试')
    mapping = {key:list(value) for key,value in command_overrides(target).items()}
    for item in view['commands']:
        key, command = f'command__{item["index"]}', item['command']
        if key in form:
            values = _words(form[key]) or list(command_overrides(base).get(command) or (command,))
            if len(values) > 12:
                raise ValueError('每个指令最多设置 12 个触发词')
            mapping[command] = values
    if mapping:
        target['command_overrides'] = mapping
    if 'require_mention' in form:
        target['require_mention'] = _boolean(form['require_mention'])
    if 'keywords' in form:
        target['keywords'] = _words(form['keywords'])
    for name, field in editable.items():
        if f'schema__{name}' in form:
            target[name] = _field_value(field, form[f'schema__{name}'])
    commands = tuple(item['command'] for item in view['commands'])
    _check_commands(db, channel, room, binding, commands, target)
    # 默认业务命令不必写入映射；这样机器人默认值变化时未覆盖项目仍可继续继承。
    if 'command_overrides' in target:
        inherited = command_overrides(base)
        target['command_overrides'] = {key:list(value) for key,value in command_overrides(target).items()
                                      if value != (inherited.get(key) or (key,))}
        if not target['command_overrides']:
            target.pop('command_overrides')
    diff = diff_config_override(base, target)
    # 不用普通配置的空值压缩器，false、空字符串和空数组都是有效的群覆盖。
    update_group_plugin_scope_config_override(db, channel=channel, chatroom=room, bot_plugin_id=binding.id,
        config_override_json=json.dumps(diff, ensure_ascii=False, sort_keys=True) if diff else None,
        operator_id=operator_id, audit_note='在群详情保存结构化插件设置', source='chatroom_plugin_settings')
    return binding


def reset_group_plugin_settings(db: Session, channel: Channel, room: Chatroom, binding_id: int, operator_id: int) -> BotPlugin:
    """只清除配置覆盖，现有启停选择由群作用域服务保留。"""

    binding = _binding(db, channel, room, binding_id)
    update_group_plugin_scope_config_override(db, channel=channel, chatroom=room, bot_plugin_id=binding.id,
        config_override_json=None, operator_id=operator_id, audit_note='恢复本群插件配置默认值', source='chatroom_plugin_settings_reset')
    return binding
