"""Small idempotent schema upgrades for the local SQLite installation."""
from sqlalchemy import inspect, text


LOCAL_TIME_MIGRATION_KEY = "migration.local_timestamps_utc8.v1"


def _migrate_server_timestamps_to_china_time(connection, tables, columns_by_table):
    """Convert legacy SQLite CURRENT_TIMESTAMP values from UTC to UTC+8 once."""
    if "system_configs" not in tables:
        return
    applied = connection.execute(text(
        "SELECT 1 FROM system_configs WHERE config_key=:key LIMIT 1"
    ), {"key": LOCAL_TIME_MIGRATION_KEY}).first()
    if applied:
        return

    timestamp_columns = {
        "users": ("created_at",),
        "addresses": ("created_at",),
        "orders": ("created_at", "updated_at"),
        "batches": ("created_at", "updated_at"),
        "courier_tracks": ("created_at",),
        "departments": ("created_at", "updated_at"),
        "projects": ("created_at", "updated_at"),
        "manual_waybill_records": ("created_at", "updated_at"),
        "billing_statements": ("uploaded_at", "updated_at"),
        "billing_statement_lines": ("updated_at",),
        "feedback": ("created_at",),
        "system_configs": ("updated_at",),
    }
    for table, column_names in timestamp_columns.items():
        existing = columns_by_table.get(table, set())
        for column in column_names:
            if column in existing:
                connection.execute(text(
                    f'UPDATE "{table}" SET "{column}"=datetime("{column}", \'+8 hours\') '
                    f'WHERE "{column}" IS NOT NULL'
                ))
    connection.execute(text(
        "INSERT INTO system_configs(config_key,config_value,description) "
        "VALUES (:key,'applied','历史数据库时间统一为北京时间')"
    ), {"key": LOCAL_TIME_MIGRATION_KEY})


def upgrade_schema(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    original_columns = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in tables
    }
    additions_by_table = {
        "users": {
            "supervisor_user_id": "INTEGER",
            "hrm_job_title": "VARCHAR(50)",
            "allow_custom_sender_address": "BOOLEAN DEFAULT 0",
            "approval_level1_required_override": "BOOLEAN",
            "work_location": "VARCHAR(100)",
            "region_code": "VARCHAR(30) DEFAULT 'xiamen_other' NOT NULL",
        },
        "addresses": {
            "region_code": "VARCHAR(30) DEFAULT 'xiamen_other' NOT NULL",
        },
        "orders": {
            "pickup_time_mode": "VARCHAR(20) DEFAULT 'immediate'",
            "pickup_start_time": "DATETIME",
            "pickup_end_time": "DATETIME",
            "item_volume": "FLOAT",
            "parcel_count": "INTEGER DEFAULT 1",
            "courier_order_nos": "TEXT",
            "actual_fee_detail": "TEXT",
            "approval_total_levels": "INTEGER DEFAULT 1",
            "approval_current_level": "INTEGER DEFAULT 1",
            "approval_level1_required": "BOOLEAN DEFAULT 1",
            "approval_level1_user_id": "INTEGER",
            "approval_level1_time": "DATETIME",
            "approval_level1_remark": "TEXT",
            "approval_level2_user_id": "INTEGER",
            "approval_level2_time": "DATETIME",
            "approval_level2_remark": "TEXT",
            "approval_stage_started_at": "DATETIME",
            "approval_last_reminded_at": "DATETIME",
            "region_code": "VARCHAR(30) DEFAULT 'xiamen_other' NOT NULL",
            "billing_account_code": "VARCHAR(100)",
            "receiver_lng": "VARCHAR(32)",
            "receiver_lat": "VARCHAR(32)",
            "item_value": "FLOAT DEFAULT 1",
            "sf_city_notification_events": "TEXT",
        },
        "batches": {
            "item_volume": "FLOAT",
            "insurance": "FLOAT DEFAULT 0",
            "parcel_count": "INTEGER DEFAULT 1",
            "sender_district": "VARCHAR(50)",
            "pickup_time_mode": "VARCHAR(20) DEFAULT 'immediate'",
            "pickup_start_time": "DATETIME",
            "pickup_end_time": "DATETIME",
            "estimated_freight": "FLOAT",
            "approval_total_levels": "INTEGER DEFAULT 1",
            "approval_current_level": "INTEGER DEFAULT 1",
            "approval_level1_required": "BOOLEAN DEFAULT 1",
            "approval_level1_user_id": "INTEGER",
            "approval_level1_time": "DATETIME",
            "approval_level1_remark": "TEXT",
            "approval_level2_user_id": "INTEGER",
            "approval_level2_time": "DATETIME",
            "approval_level2_remark": "TEXT",
            "approval_stage_started_at": "DATETIME",
            "approval_last_reminded_at": "DATETIME",
            "region_code": "VARCHAR(30) DEFAULT 'xiamen_other' NOT NULL",
            "billing_account_code": "VARCHAR(100)",
        },
        "projects": {
            "full_path": "VARCHAR(1000)",
            "parent_id": "INTEGER",
            "level": "INTEGER DEFAULT 1",
            "updated_at": "DATETIME",
        },
        "manual_waybill_records": {
            "approval_level2_user_id": "INTEGER",
            "approval_level2_time": "DATETIME",
            "approval_level2_remark": "TEXT",
            "approval_stage_started_at": "DATETIME",
            "approval_last_reminded_at": "DATETIME",
        },
        "billing_statement_lines": {
            "submitter_user_id": "INTEGER",
            "submitter_name": "VARCHAR(200)",
        },
    }
    with engine.begin() as connection:
        for table, additions in additions_by_table.items():
            if table not in tables:
                continue
            existing = original_columns.get(table, set())
            for name, definition in additions.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        if "users" in tables:
            # 审批人由每笔订单的审批流决定，成员只保留员工/管理员两种角色。
            connection.execute(text("UPDATE users SET role='admin' WHERE role='admin_approver'"))
            connection.execute(text("UPDATE users SET role='user' WHERE role IS NULL OR role IN ('employee','approver')"))
            # 早期本地调试账号，从真实成员列表中清理。
            connection.execute(text("DELETE FROM users WHERE dingtalk_userid='mock_001'"))
            connection.execute(text("UPDATE users SET region_code='xiamen_other' WHERE region_code IS NULL OR region_code=''"))
        if "addresses" in tables:
            connection.execute(text("UPDATE addresses SET region_code='xiamen_other' WHERE region_code IS NULL OR region_code=''"))
        if "orders" in tables:
            if "region_code" not in original_columns.get("orders", set()):
                connection.execute(text("UPDATE orders SET region_code=CASE WHEN courier_type IN ('JDL','JD') THEN 'chengdu' ELSE 'xiamen_other' END"))
            else:
                connection.execute(text("UPDATE orders SET region_code='xiamen_other' WHERE region_code IS NULL OR region_code=''"))
        if "batches" in tables:
            if "region_code" not in original_columns.get("batches", set()):
                connection.execute(text("UPDATE batches SET region_code=CASE WHEN courier_type IN ('JDL','JD') THEN 'chengdu' ELSE 'xiamen_other' END"))
            else:
                connection.execute(text("UPDATE batches SET region_code='xiamen_other' WHERE region_code IS NULL OR region_code=''"))
        if "manual_waybill_records" in tables:
            # 审核流上线前的有效补充记录已经参与历史分摊，继续视为审核通过。
            connection.execute(text(
                "UPDATE manual_waybill_records SET status='approved' WHERE status='active'"
            ))
        if "billing_statement_lines" in tables and "users" in tables:
            # Backfill the new real-name submitter columns for statements that
            # were imported before this schema version. Sender names are
            # intentionally never used as an identity source.
            if "orders" in tables:
                connection.execute(text(
                    "UPDATE billing_statement_lines SET submitter_user_id=("
                    "SELECT created_by FROM orders WHERE orders.id=billing_statement_lines.match_reference_id"
                    ") WHERE submitter_user_id IS NULL AND match_source='system_order'"
                ))
            if "manual_waybill_records" in tables:
                connection.execute(text(
                    "UPDATE billing_statement_lines SET submitter_user_id=("
                    "SELECT created_by FROM manual_waybill_records "
                    "WHERE manual_waybill_records.id=billing_statement_lines.match_reference_id"
                    ") WHERE submitter_user_id IS NULL AND match_source='manual_waybill'"
                ))
            connection.execute(text(
                "UPDATE billing_statement_lines SET submitter_name=("
                "SELECT name FROM users WHERE users.id=billing_statement_lines.submitter_user_id"
                ") WHERE submitter_user_id IS NOT NULL AND "
                "(submitter_name IS NULL OR submitter_name='')"
            ))
        if "system_configs" in tables:
            xiamen_keys = ("sf_customer_code", "sf_check_word", "sf_monthly_card", "db_company_code", "db_appkey", "db_sign", "db_customer_code")
            chengdu_keys = ("jdl_app_key", "jdl_app_secret", "jdl_access_token", "jdl_customer_code", "jdl_business_unit_code")
            for region, keys in (("xiamen_other", xiamen_keys), ("chengdu", chengdu_keys)):
                for key in keys:
                    connection.execute(text(
                        "INSERT OR IGNORE INTO system_configs(config_key,config_value,description) "
                        "SELECT :target,config_value,:description FROM system_configs WHERE config_key=:source"
                    ), {"target": f"region.{region}.{key}", "source": key, "description": f"{region}区域配置迁移"})
            defaults = {
                "region.xiamen_other.enabled_couriers": '["SF","DB"]',
                "region.chengdu.enabled_couriers": '["JDL"]',
                "region.beijing.enabled_couriers": '[]',
            }
            for key, value in defaults.items():
                connection.execute(text(
                    "INSERT OR IGNORE INTO system_configs(config_key,config_value,description) VALUES (:key,:value,'区域启用快递')"
                ), {"key": key, "value": value})
        if engine.dialect.name == "sqlite":
            _migrate_server_timestamps_to_china_time(
                connection, tables, original_columns,
            )
