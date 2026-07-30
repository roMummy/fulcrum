from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from app.core.db import engine
from app.core.settings import settings


@dataclass
class FixResult:
    table: str
    column: str
    affected_rows: int


TARGET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("messages", "created_at"),
    ("messages", "updated_at"),
    ("raw_webhook_events", "created_at"),
    ("raw_webhook_events", "updated_at"),
    ("plugin_execution_logs", "created_at"),
    ("plugin_execution_logs", "updated_at"),
    ("message_activity_hourly", "bucket_start"),
    ("message_activity_hourly", "updated_at"),
    ("plugin_activity_hourly", "bucket_start"),
    ("plugin_activity_hourly", "updated_at"),
    ("conversations", "last_message_at"),
    ("channels", "last_login_at"),
    ("channels", "last_heartbeat_at"),
    ("channels", "last_message_sync_at"),
    ("bot_plugins", "last_reload_at"),
    ("bot_plugins", "last_execution_at"),
    ("chatrooms", "last_synced_at"),
    ("chatroom_members", "last_synced_at"),
    ("chatroom_members", "last_active_at"),
    ("group_plugin_scopes", "last_operated_at"),
    ("task_jobs", "available_at"),
    ("task_jobs", "started_at"),
    ("task_jobs", "finished_at"),
    ("task_jobs", "locked_at"),
    ("alert_records", "resolved_at"),
    ("audit_logs", "created_at"),
    ("audit_logs", "updated_at"),
    ("health_check_records", "created_at"),
    ("health_check_records", "updated_at"),
    ("error_logs", "created_at"),
)


def is_mysql_family() -> bool:
    database_url = settings.database_url.strip().lower()
    return database_url.startswith("mysql") or database_url.startswith("mariadb")


def run_fix() -> list[FixResult]:
    results: list[FixResult] = []
    with engine.begin() as connection:
        connection.execute(text("SET time_zone = '+08:00'"))
        for table_name, column_name in TARGET_COLUMNS:
            statement = text(
                f"""
                UPDATE {table_name}
                SET {column_name} = CONVERT_TZ({column_name}, '+00:00', '+08:00')
                WHERE {column_name} IS NOT NULL
                  AND {column_name} >= '2025-01-01 00:00:00'
                """
            )
            result = connection.execute(statement)
            results.append(FixResult(table=table_name, column=column_name, affected_rows=int(result.rowcount or 0)))
        conversation_fix = connection.execute(
            text(
                """
                UPDATE conversations AS c
                LEFT JOIN (
                    SELECT conversation_id, MAX(created_at) AS latest_message_at
                    FROM messages
                    GROUP BY conversation_id
                ) AS m ON m.conversation_id = c.id
                SET c.last_message_at = m.latest_message_at
                """
            )
        )
        results.append(
            FixResult(
                table="conversations",
                column="last_message_at_rebuild",
                affected_rows=int(conversation_fix.rowcount or 0),
            )
        )
    return results


def main() -> None:
    if not is_mysql_family():
        print("当前 DATABASE_URL 不是 MySQL/MariaDB，跳过修复。")
        return
    results = run_fix()
    print("已完成上海时间修复：")
    for item in results:
        if item.affected_rows:
            print(f"- {item.table}.{item.column}: {item.affected_rows}")


if __name__ == "__main__":
    main()
