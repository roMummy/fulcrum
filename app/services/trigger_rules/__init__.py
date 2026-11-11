"""群级触发回复:用户在群里配置「什么时候 → 回复什么」,不用写代码。

- catalog:触发类型与动作类型的注册表(新增触发/动作只改这里);
- media:租户素材存储;
- engine:规则的读取、校验、保存和执行;
- 群功能入口见 app/services/group_features/trigger_reply.py。
"""
