"""Create a small, fully synthetic database for the public demonstration.

No row in this file is copied from the private production database.  Names,
phones, addresses, identifiers, projects and waybills are deliberately fake.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEMO_DB = ROOT / "demo.db"


def configure_demo_environment() -> None:
    os.environ["DATABASE_URL"] = "sqlite:///./demo.db"
    os.environ["DINGTALK_LOGIN_ENABLED"] = "false"
    os.environ["LOCAL_USER_ID"] = "1"
    os.environ["PREVIEW_SAFE_MODE"] = "true"
    os.environ["SESSION_COOKIE_SECURE"] = "false"
    os.environ["SF_MOCK_MODE"] = "true"
    os.environ["SF_LIVE_ORDER_ENABLED"] = "false"
    os.environ["DB_MOCK_MODE"] = "true"
    os.environ["DB_LIVE_ORDER_ENABLED"] = "false"
    os.environ["JDL_LIVE_ORDER_ENABLED"] = "false"
    os.environ["SF_CITY_LIVE_ORDER_ENABLED"] = "false"


def create_demo_database(reset: bool = False) -> None:
    if reset:
        for path in (DEMO_DB, DEMO_DB.with_name("demo.db-shm"), DEMO_DB.with_name("demo.db-wal")):
            path.unlink(missing_ok=True)
    if DEMO_DB.exists():
        print("demo.db already exists; use --reset to rebuild it.")
        return

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    configure_demo_environment()

    from database import init_db, models
    from database.connection import SessionLocal

    init_db()
    now = datetime.now().replace(second=0, microsecond=0)

    with SessionLocal() as db:
        users = [
            models.User(
                dingtalk_userid="demo-admin", name="演示管理员", phone="13800000001",
                department="示例科技有限公司-行政管理部", role="admin",
                work_location="厦门", region_code="xiamen_other",
                allow_custom_sender_address=True,
            ),
            models.User(
                dingtalk_userid="demo-xiamen", name="厦门演示员工", phone="13800000002",
                department="示例科技有限公司-运营中心-内容组", role="user",
                work_location="厦门", region_code="xiamen_other",
            ),
            models.User(
                dingtalk_userid="demo-beijing", name="北京演示员工", phone="13800000003",
                department="示例科技有限公司-北京事业部", role="user",
                work_location="北京", region_code="beijing",
            ),
            models.User(
                dingtalk_userid="demo-chengdu", name="成都演示员工", phone="13800000004",
                department="示例科技有限公司-成都事业部", role="user",
                work_location="成都", region_code="chengdu",
            ),
        ]
        db.add_all(users)
        db.flush()
        for user in users[1:]:
            user.supervisor_user_id = users[0].id

        departments = [
            ("D000", "示例科技有限公司"),
            ("D100", "示例科技有限公司-行政管理部"),
            ("D210", "示例科技有限公司-运营中心-内容组"),
            ("D310", "示例科技有限公司-北京事业部"),
            ("D410", "示例科技有限公司-成都事业部"),
        ]
        db.add_all([
            models.Department(dept_code=code, dept_name=name, enabled=True)
            for code, name in departments
        ])

        projects = [
            ("DEMO-2026-001", "演示项目-品牌内容制作", "D210", departments[2][1]),
            ("DEMO-2026-002", "演示项目-北京客户活动", "D310", departments[3][1]),
            ("DEMO-2026-003", "演示项目-成都渠道推广", "D410", departments[4][1]),
        ]
        db.add_all([
            models.Project(
                project_code=code, project_name=name, full_path=name,
                dept_code=dept_code, dept_name=dept_name, enabled=True,
            )
            for code, name, dept_code, dept_name in projects
        ])

        region_carriers = {
            "xiamen_other": ["SF", "DB"],
            "beijing": ["SF", "DB"],
            "chengdu": ["JDL"],
        }
        region_defaults = {
            "xiamen_other": "db_small_standard",
            "beijing": "db_small_standard",
            "chengdu": "jdl_biaokuai",
        }
        def set_config(key: str, value: str, description: str) -> None:
            row = db.query(models.SystemConfig).filter_by(config_key=key).first()
            if row:
                row.config_value = value
                row.description = description
            else:
                db.add(models.SystemConfig(
                    config_key=key, config_value=value, description=description,
                ))

        for region, carriers in region_carriers.items():
            set_config(
                f"region.{region}.enabled_couriers",
                json.dumps(carriers), "公开演示区域快递",
            )
            set_config(
                f"region.{region}.default_service_id",
                region_defaults[region], "公开演示默认推荐",
            )

        samples = [
            (2, "xiamen_other", "SF", "顺丰标快", "pending", None, "", 1, "dept", "D100", departments[1][1], None, None, 18.0),
            (2, "xiamen_other", "DB", "德邦小件标快", "ordered", "DBDEMO000002", "等待揽收", 3, "project", "D210", departments[2][1], projects[0][0], projects[0][1], 16.0),
            (2, "xiamen_other", "SF", "顺丰特快", "success", "SFDEMO000003", "快件已揽收", 6, "dept", "D210", departments[2][1], None, None, 24.0),
            (3, "beijing", "SF", "顺丰标快", "success", "SFDEMO000004", "运输中", 8, "project", "D310", departments[3][1], projects[1][0], projects[1][1], 22.0),
            (3, "beijing", "DB", "德邦小件标快", "success", "DBDEMO000005", "派送中", 10, "dept", "D310", departments[3][1], None, None, 20.0),
            (4, "chengdu", "JDL", "京东标快", "delivered", "JDDEMO000006", "已签收", 13, "project", "D410", departments[4][1], projects[2][0], projects[2][1], 19.0),
            (2, "xiamen_other", "SF", "顺丰标快", "failed", "SFDEMO000007", "客户拒收，原路退回", 17, "dept", "D100", departments[1][1], None, None, 18.0),
            (4, "chengdu", "JDL", "京东标快", "cancelled", "JDDEMO000008", "订单已取消", 21, "dept", "D410", departments[4][1], None, None, 17.0),
            (2, "xiamen_other", "DB", "德邦大件", "success", "DBDEMO000009", "到达分拨中心", 26, "project", "D210", departments[2][1], projects[0][0], projects[0][1], 86.0),
            (2, "xiamen_other", "SF_CITY", "顺丰同城急送", "delivered", "SFCITYDEMO000010", "已送达", 2, "dept", "D210", departments[2][1], None, None, 32.5),
        ]

        for index, sample in enumerate(samples, start=1):
            (
                creator_id, region, courier, service_name, status, waybill, track,
                age_days, cost_type, dept_code, dept_name, project_code,
                project_name, freight,
            ) = sample
            created_at = now - timedelta(days=age_days, hours=index)
            receiver_city = {"xiamen_other": "厦门市", "beijing": "北京市", "chengdu": "成都市"}[region]
            receiver_province = {"xiamen_other": "福建省", "beijing": "北京市", "chengdu": "四川省"}[region]
            order = models.Order(
                order_no=f"DEMO-{now:%Y%m}-{index:04d}", courier_type=courier,
                region_code=region, express_type_name=service_name,
                courier_order_no=waybill,
                sender_name="示例寄件人", sender_phone="13800000001",
                sender_company="示例科技有限公司", sender_province="福建省",
                sender_city="厦门市", sender_district="思明区",
                sender_address="示例大道100号A座601室",
                receiver_name=f"演示收件人{index}", receiver_phone=f"139000000{index:02d}",
                receiver_company="演示客户公司", receiver_province=receiver_province,
                receiver_city=receiver_city, receiver_district="示例区",
                receiver_address=f"示例路{100 + index}号",
                item_name="演示文件" if index % 2 else "办公用品",
                item_weight=1.0 if index != 9 else 22.0, parcel_count=1,
                item_count=1, shipping_reason="公开演示订单",
                freight=freight, actual_freight=freight if waybill else None,
                insurance=0, cost_type=cost_type, dept_code=dept_code,
                dept_name=dept_name, project_code=project_code,
                project_name=project_name, status=status, source="demo",
                track_status=track, track_update_time=created_at + timedelta(hours=4) if track else None,
                created_by=creator_id, created_at=created_at, updated_at=created_at,
                order_time=created_at + timedelta(minutes=15) if waybill else None,
            )
            db.add(order)
            db.flush()
            if waybill and track:
                db.add(models.CourierTrack(
                    order_id=order.id, courier_order_no=waybill,
                    track_time=created_at + timedelta(hours=4),
                    track_status=track, track_detail=f"{track}（公开演示数据）",
                ))

        db.commit()

    print(f"Synthetic demo database created: {DEMO_DB}")
    print("Demo administrator: 演示管理员 (LOCAL_USER_ID=1)")


def main() -> None:
    parser = argparse.ArgumentParser(description="创建完全脱敏的公开演示数据库")
    parser.add_argument("--reset", action="store_true", help="重建 demo.db")
    args = parser.parse_args()
    create_demo_database(reset=args.reset)


if __name__ == "__main__":
    main()
