from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import MetaData, Table, create_engine, func, select
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, Message, Plugin, TaskJob, WorkWechatContact
from app.models.saas import LegacyImportBatch, Tenant
from app.services.database_lifecycle import assert_database_at_head


SessionFactory = Callable[[], Session]

# 首期只迁移已经完成租户化且属于企业微信主流程的核心数据。插件定义是平台级共享
# 资源，不复制来源主键；bot_plugins 通过插件 code 绑定目标库中已经同步的插件。
IMPORT_TABLES = (
    "bots",
    "channels",
    "workwechat_contacts",
    "chatrooms",
    "conversations",
    "messages",
    "bot_plugins",
    "task_jobs",
)
SOURCE_TABLES = (*IMPORT_TABLES, "plugins")
TARGET_MODELS = {
    "bots": Bot,
    "channels": Channel,
    "workwechat_contacts": WorkWechatContact,
    "chatrooms": Chatroom,
    "conversations": Conversation,
    "messages": Message,
    "bot_plugins": BotPlugin,
    "task_jobs": TaskJob,
}
FOREIGN_KEY_MAPS = {
    "channels": {"bot_id": "bots"},
    "workwechat_contacts": {"channel_id": "channels"},
    "chatrooms": {"bot_id": "bots", "channel_id": "channels"},
    "conversations": {"bot_id": "bots", "channel_id": "channels"},
    "messages": {"conversation_id": "conversations"},
    "bot_plugins": {"bot_id": "bots", "plugin_id": "plugins"},
}
TASK_REFERENCE_TABLES = {
    "bot": "bots",
    "bots": "bots",
    "channel": "channels",
    "channels": "channels",
    "chatroom": "chatrooms",
    "chatrooms": "chatrooms",
    "conversation": "conversations",
    "conversations": "conversations",
    "message": "messages",
    "messages": "messages",
    "bot_plugin": "bot_plugins",
    "bot_plugins": "bot_plugins",
}
TASK_JSON_REFERENCE_KEYS = {
    "bot_id": "bots",
    "channel_id": "channels",
    "chatroom_id": "chatrooms",
    "conversation_id": "conversations",
    "message_id": "messages",
    "bot_plugin_id": "bot_plugins",
}
EXCLUDED_TARGET_COLUMNS = {
    "id",
    "tenant_id",
    # 旧单租户渠道和任务没有可信 Provider 节点身份，导入后必须重新绑定和领取。
    "provider_node_id",
    "provider_account_id",
    "lease_token_hash",
    "lease_expires_at",
    "claimed_at",
}


class LegacyImportError(RuntimeError):
    """表示可安全返回给命令行的导入错误代码，不携带连接串或业务原文。"""


def _empty_table_stats() -> dict[str, int]:
    return {"read": 0, "written": 0, "skipped": 0, "errors": 0}


def _new_report(*, tenant_code: str, dry_run: bool) -> dict[str, Any]:
    """创建稳定的机器可读报告结构，报告中不包含来源 URL 和敏感字段。"""

    return {
        "success": False,
        "mode": "dry-run" if dry_run else "import",
        "tenant_code": tenant_code,
        "source_fingerprint": "",
        "tables": {table_name: _empty_table_stats() for table_name in IMPORT_TABLES},
        "warnings": [],
        "errors": [],
    }


def _append_unique(items: list[str], value: str) -> None:
    """保持报告代码顺序稳定，同时避免同类警告按行重复膨胀。"""

    if value not in items:
        items.append(value)


def _build_read_only_source_engine(source_url: str) -> Engine:
    """构造来源只读引擎；SQLite 使用 URI mode=ro，服务端数据库设置只读会话。"""

    normalized_url = str(source_url or "").strip()
    if not normalized_url:
        raise LegacyImportError("source_url_required")
    parsed_url = make_url(normalized_url)
    backend = parsed_url.get_backend_name()
    if backend == "sqlite":
        database_name = parsed_url.database
        if not database_name or database_name == ":memory:":
            raise LegacyImportError("sqlite_source_file_required")
        database_path = Path(database_name).expanduser().resolve()
        if not database_path.is_file():
            raise LegacyImportError("source_database_not_found")
        # SQLAlchemy 的 SQLite URI 只有同时带 mode=ro 和 uri=true 才会由 sqlite3
        # 以只读方式打开文件；路径经过 URL 编码但保留目录分隔符。
        read_only_url = f"sqlite:///file:{quote(str(database_path), safe='/')}?mode=ro&uri=true"
        return create_engine(read_only_url, connect_args={"uri": True})
    if backend not in {"mysql", "mariadb", "postgresql"}:
        raise LegacyImportError(f"unsupported_source_backend:{backend}")

    engine = create_engine(normalized_url, pool_pre_ping=True)
    # 数据库只读账号仍是首选；下面的连接级设置提供第二层保护。若目标数据库不允许
    # 切换只读事务，连接会直接失败，而不会降级成可能写入来源库的普通会话。
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_source_read_only(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            if backend in {"mysql", "mariadb"}:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            else:
                cursor.execute("SET default_transaction_read_only = on")
        finally:
            cursor.close()

    return engine


def _reflect_source_tables(connection: Connection) -> dict[str, Table]:
    """只反射白名单表，来源库缺少任一核心表时拒绝猜测或部分静默导入。"""

    available_tables = set(connection.dialect.get_table_names(connection))
    missing_tables = [table_name for table_name in SOURCE_TABLES if table_name not in available_tables]
    if missing_tables:
        raise LegacyImportError(f"source_tables_missing:{','.join(missing_tables)}")
    metadata = MetaData()
    return {
        table_name: Table(table_name, metadata, autoload_with=connection)
        for table_name in SOURCE_TABLES
    }


def _iter_rows(connection: Connection, table: Table) -> Iterator[Mapping[str, Any]]:
    """按旧主键稳定遍历，避免不同数据库默认行顺序造成报告和映射不确定。"""

    result = connection.execute(select(table).order_by(table.c.id)).mappings()
    yield from result


def _source_snapshot(connection: Connection, tables: Mapping[str, Table], safe_source_label: str) -> tuple[str, dict[str, int]]:
    """根据脱敏来源标识和表计数生成稳定指纹，不读取或输出密钥字段。"""

    counts: dict[str, int] = {}
    fingerprint_tables: dict[str, dict[str, int | None]] = {}
    for table_name in SOURCE_TABLES:
        table = tables[table_name]
        row = connection.execute(select(func.count(table.c.id), func.max(table.c.id))).one()
        counts[table_name] = int(row[0] or 0)
        fingerprint_tables[table_name] = {"count": int(row[0] or 0), "max_id": int(row[1]) if row[1] is not None else None}
    material = json.dumps(
        {"source": safe_source_label, "tables": fingerprint_tables},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), counts


def _source_identity_sets(connection: Connection, tables: Mapping[str, Table]) -> dict[str, set[int]]:
    """只加载主键集合用于预检外键，避免预检阶段把整张消息表驻留内存。"""

    return {
        table_name: {int(value) for value in connection.scalars(select(tables[table_name].c.id))}
        for table_name in SOURCE_TABLES
    }


def _validate_foreign_keys(
    connection: Connection,
    tables: Mapping[str, Table],
    source_ids: Mapping[str, set[int]],
    report: dict[str, Any],
) -> None:
    """在任何目标写入前验证核心父子关系，错误按表累计并返回非零结果。"""

    for table_name, rules in FOREIGN_KEY_MAPS.items():
        for row in _iter_rows(connection, tables[table_name]):
            for column_name, parent_table in rules.items():
                old_parent_id = row.get(column_name)
                if old_parent_id is None:
                    if tables[table_name].c[column_name].nullable:
                        continue
                    report["tables"][table_name]["errors"] += 1
                    _append_unique(report["errors"], f"missing_required_reference:{table_name}.{column_name}")
                    continue
                if int(old_parent_id) not in source_ids[parent_table]:
                    report["tables"][table_name]["errors"] += 1
                    _append_unique(report["errors"], f"missing_source_reference:{table_name}.{column_name}")


def _load_target_plugin_map(
    db: Session,
    connection: Connection,
    tables: Mapping[str, Table],
    report: dict[str, Any],
) -> dict[int, int]:
    """按稳定插件 code 映射目标全局插件，禁止把来源插件主键直接写入 SaaS。"""

    source_plugins = [(int(row["id"]), str(row["code"])) for row in _iter_rows(connection, tables["plugins"])]
    target_plugins = db.scalars(select(Plugin).where(Plugin.code.in_([code for _old_id, code in source_plugins]))).all()
    target_by_code = {plugin.code: int(plugin.id) for plugin in target_plugins}
    plugin_map: dict[int, int] = {}
    for old_id, code in source_plugins:
        target_id = target_by_code.get(code)
        if target_id is None:
            _append_unique(report["errors"], f"missing_target_plugin:{code}")
            continue
        plugin_map[old_id] = target_id
    return plugin_map


def _validate_product_scope(connection: Connection, tables: Mapping[str, Table], report: dict[str, Any]) -> None:
    """旧库包含非企微渠道时整体拒绝，避免平台旁路绕过 SaaS 首期产品边界。"""

    channel_types = sorted({str(value) for value in connection.scalars(select(tables["channels"].c.channel_type).distinct())})
    for channel_type in channel_types:
        if channel_type != "workwechat":
            _append_unique(report["errors"], f"unsupported_channel_type:{channel_type}")


def _validate_task_references(
    connection: Connection,
    tables: Mapping[str, Table],
    source_ids: Mapping[str, set[int]],
    report: dict[str, Any],
) -> None:
    """在写入前解析任务 JSON，并用旧主键集合验证其中所有已知资源引用。"""

    identity_maps = {
        table_name: {old_id: old_id for old_id in ids}
        for table_name, ids in source_ids.items()
    }
    for row in _iter_rows(connection, tables["task_jobs"]):
        try:
            related_id = row.get("related_id")
            related_type = str(row.get("related_type") or "").strip().lower()
            if related_id is not None and related_type in TASK_REFERENCE_TABLES:
                _mapped_id(identity_maps, TASK_REFERENCE_TABLES[related_type], related_id)
            payload = json.loads(str(row.get("payload_json") or "{}"))
            _remap_task_json(payload, identity_maps)
        except (json.JSONDecodeError, LegacyImportError):
            report["tables"]["task_jobs"]["errors"] += 1
            _append_unique(report["errors"], "invalid_task_reference")


def _validate_target_bot_names(
    db: Session,
    tenant_id: int,
    connection: Connection,
    tables: Mapping[str, Table],
    report: dict[str, Any],
) -> None:
    """提前识别租户内机器人名称唯一键冲突，避免写到中途才由数据库报错。"""

    source_names = {str(value) for value in connection.scalars(select(tables["bots"].c.name))}
    if not source_names:
        return
    existing_names = set(
        db.scalars(select(Bot.name).where(Bot.tenant_id == tenant_id, Bot.name.in_(source_names))).all()
    )
    for name in sorted(existing_names):
        _append_unique(report["errors"], f"target_bot_name_exists:{name}")


def _copy_common_values(model: type[Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """只复制新旧表共同列，删除的旧字段和新增的 Provider 身份字段都不被猜测填充。"""

    target_columns = {column.name for column in model.__table__.columns}
    return {
        key: value
        for key, value in row.items()
        if key in target_columns and key not in EXCLUDED_TARGET_COLUMNS
    }


def _mapped_id(id_maps: Mapping[str, Mapping[int, int]], table_name: str, value: Any, *, nullable: bool = False) -> int | None:
    """把旧主键转换为新主键，缺少映射时拒绝保留可能串租户的裸数字。"""

    if value is None and nullable:
        return None
    try:
        return int(id_maps[table_name][int(value)])
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyImportError(f"reference_mapping_missing:{table_name}") from exc


def _remap_task_json(value: Any, id_maps: Mapping[str, Mapping[int, int]]) -> Any:
    """递归解析任务 JSON，只重映射明确命名的资源 ID 字段，不做字符串替换。"""

    if isinstance(value, list):
        return [_remap_task_json(item, id_maps) for item in value]
    if not isinstance(value, dict):
        return value
    remapped: dict[str, Any] = {}
    for key, item in value.items():
        table_name = TASK_JSON_REFERENCE_KEYS.get(str(key))
        if table_name is not None and item is not None:
            remapped[str(key)] = _mapped_id(id_maps, table_name, item)
        else:
            remapped[str(key)] = _remap_task_json(item, id_maps)
    return remapped


def _prepare_task_values(
    row: Mapping[str, Any],
    id_maps: Mapping[str, Mapping[int, int]],
    report: dict[str, Any],
) -> dict[str, Any]:
    """重映射任务显式关联和 JSON 负载，旧 Provider 租约信息始终清空。"""

    values = _copy_common_values(TaskJob, row)
    related_type = str(row.get("related_type") or "").strip().lower()
    related_id = row.get("related_id")
    if related_id is not None:
        related_table = TASK_REFERENCE_TABLES.get(related_type)
        if related_table is None:
            values["related_id"] = None
            _append_unique(report["warnings"], "unknown_task_related_reference_cleared")
        else:
            values["related_id"] = _mapped_id(id_maps, related_table, related_id)
    try:
        payload = json.loads(str(row.get("payload_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise LegacyImportError("invalid_task_payload_json") from exc
    values["payload_json"] = json.dumps(_remap_task_json(payload, id_maps), ensure_ascii=False, separators=(",", ":"))
    if str(values.get("status") or "").strip().lower() in {"running", "processing"}:
        # 旧进程的锁和租约不能在新 Provider 节点体系中继续可信。运行中任务回到
        # pending 后由新节点重新领取，已完成和已失败历史仍保持原状态。
        values["status"] = "pending"
        values["started_at"] = None
        values["locked_at"] = None
        _append_unique(report["warnings"], "task_runtime_state_reset")
    return values


def _import_rows(
    db: Session,
    connection: Connection,
    tables: Mapping[str, Table],
    tenant_id: int,
    plugin_map: Mapping[int, int],
    report: dict[str, Any],
) -> dict[str, dict[int, int]]:
    """按父资源到子资源逐表写入并建立旧到新主键映射，整个过程不提交事务。"""

    id_maps: dict[str, dict[int, int]] = {table_name: {} for table_name in IMPORT_TABLES}
    id_maps["plugins"] = dict(plugin_map)
    for table_name in IMPORT_TABLES:
        model = TARGET_MODELS[table_name]
        for row in _iter_rows(connection, tables[table_name]):
            old_id = int(row["id"])
            if table_name == "task_jobs":
                values = _prepare_task_values(row, id_maps, report)
            else:
                values = _copy_common_values(model, row)
                for column_name, parent_table in FOREIGN_KEY_MAPS.get(table_name, {}).items():
                    values[column_name] = _mapped_id(
                        id_maps,
                        parent_table,
                        row.get(column_name),
                        nullable=tables[table_name].c[column_name].nullable,
                    )
            values["tenant_id"] = tenant_id
            item = model(**values)
            db.add(item)
            db.flush()
            id_maps[table_name][old_id] = int(item.id)
            report["tables"][table_name]["written"] += 1
            if table_name == "channels" and row.get("bot_token_encrypted"):
                _append_unique(report["warnings"], "channel_credentials_require_rotation")
            if table_name == "bot_plugins" and row.get("config_json"):
                _append_unique(report["warnings"], "plugin_config_review_required")
    return id_maps


def _validate_written_rows(db: Session, tenant_id: int, id_maps: Mapping[str, Mapping[int, int]], report: dict[str, Any]) -> None:
    """提交前核对逐表数量和租户归属，任一不一致都让整个事务回滚。"""

    for table_name, model in TARGET_MODELS.items():
        new_ids = tuple(id_maps[table_name].values())
        if not new_ids:
            continue
        actual_count = int(
            db.scalar(select(func.count(model.id)).where(model.tenant_id == tenant_id, model.id.in_(new_ids))) or 0
        )
        expected_count = len(new_ids)
        if actual_count != expected_count or report["tables"][table_name]["written"] != expected_count:
            raise LegacyImportError(f"target_count_mismatch:{table_name}")


def _safe_source_label(source_url: str) -> str:
    """生成不含密码的来源标识，仅参与哈希，不写入最终报告。"""

    return make_url(source_url).render_as_string(hide_password=True)


def run_legacy_import(
    *,
    source_url: str,
    tenant_code: str,
    target_session_factory: SessionFactory,
    dry_run: bool = False,
) -> dict[str, Any]:
    """校验并导入旧系统核心数据；正式模式成功前不会提交任何目标写入。"""

    normalized_tenant_code = str(tenant_code or "").strip()
    report = _new_report(tenant_code=normalized_tenant_code, dry_run=dry_run)
    if not normalized_tenant_code:
        report["errors"].append("tenant_code_required")
        return report

    source_engine: Engine | None = None
    db = target_session_factory()
    try:
        try:
            assert_database_at_head(db.connection())
        except Exception as exc:
            raise LegacyImportError("target_database_not_at_head") from exc
        tenant = db.scalar(select(Tenant).where(Tenant.code == normalized_tenant_code))
        if tenant is None:
            report["errors"].append("target_tenant_not_found")
            return report
        tenant_id = int(tenant.id)

        source_engine = _build_read_only_source_engine(source_url)
        with source_engine.connect() as source_connection:
            tables = _reflect_source_tables(source_connection)
            fingerprint, counts = _source_snapshot(source_connection, tables, _safe_source_label(source_url))
            report["source_fingerprint"] = fingerprint
            for table_name in IMPORT_TABLES:
                report["tables"][table_name]["read"] = counts[table_name]

            if not dry_run and db.scalar(
                select(LegacyImportBatch.id).where(
                    LegacyImportBatch.tenant_id == tenant_id,
                    LegacyImportBatch.source_fingerprint == fingerprint,
                )
            ) is not None:
                report["errors"].append("duplicate_import")
                return report

            source_ids = _source_identity_sets(source_connection, tables)
            _validate_product_scope(source_connection, tables, report)
            _validate_foreign_keys(source_connection, tables, source_ids, report)
            _validate_task_references(source_connection, tables, source_ids, report)
            plugin_map = _load_target_plugin_map(db, source_connection, tables, report)
            _validate_target_bot_names(db, tenant_id, source_connection, tables, report)
            if report["errors"]:
                return report
            if dry_run:
                report["success"] = True
                return report

            id_maps = _import_rows(db, source_connection, tables, tenant_id, plugin_map, report)
            _validate_written_rows(db, tenant_id, id_maps, report)
            report["success"] = True
            # 批次报告只包含计数、错误代码和固定警告代码。即使数据库管理员读取
            # 审计表，也不会看到来源连接串、渠道密文或任务业务负载。
            db.add(
                LegacyImportBatch(
                    tenant_id=tenant_id,
                    source_fingerprint=fingerprint,
                    status="completed",
                    report_json=json.dumps(report, ensure_ascii=False, sort_keys=True),
                )
            )
            db.commit()
            return report
    except IntegrityError:
        db.rollback()
        report["success"] = False
        report["errors"] = ["duplicate_import"]
        for stats in report["tables"].values():
            stats["written"] = 0
        return report
    except LegacyImportError as exc:
        db.rollback()
        report["success"] = False
        _append_unique(report["errors"], str(exc))
        for stats in report["tables"].values():
            stats["written"] = 0
        return report
    except Exception as exc:
        db.rollback()
        report["success"] = False
        # 未知异常只暴露类型，避免 DBAPI 错误把连接参数或具体业务行带入报告。
        report["errors"] = [f"unexpected_import_failure:{type(exc).__name__}"]
        for stats in report["tables"].values():
            stats["written"] = 0
        return report
    finally:
        db.close()
        if source_engine is not None:
            source_engine.dispose()
