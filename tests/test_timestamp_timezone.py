import datetime as dt
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import Order
from database.schema_upgrade import LOCAL_TIME_MIGRATION_KEY, upgrade_schema


class TimestampTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_new_order_created_at_uses_china_standard_time(self):
        db = sessionmaker(bind=self.engine)()
        before = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)
        order = Order(
            order_no="CO-TIMEZONE-NEW", courier_type="SF",
            sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="北京市", receiver_city="北京市",
            receiver_address="测试路2号", item_name="文件",
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        after = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)
        self.assertLessEqual(before, order.created_at)
        self.assertLessEqual(order.created_at, after)
        db.close()

    def test_upgrade_moves_legacy_server_timestamps_once(self):
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO orders (order_no,courier_type,region_code,sender_name,sender_phone,sender_address,"
                "receiver_name,receiver_phone,receiver_province,receiver_city,receiver_address,item_name,"
                "created_at,updated_at) VALUES "
                "('CO-TIMEZONE-OLD','SF','xiamen_other','寄件人','1','地址','收件人','2','北京','北京','地址','文件',"
                "'2026-10-23 11:32:00','2026-10-23 11:33:00')"
            ))

        upgrade_schema(self.engine)
        upgrade_schema(self.engine)

        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT created_at,updated_at FROM orders WHERE order_no='CO-TIMEZONE-OLD'"
            )).one()
            marker_count = connection.execute(text(
                "SELECT COUNT(*) FROM system_configs WHERE config_key=:key"
            ), {"key": LOCAL_TIME_MIGRATION_KEY}).scalar_one()
        self.assertEqual(str(row.created_at), "2026-10-23 19:32:00")
        self.assertEqual(str(row.updated_at), "2026-10-23 19:33:00")
        self.assertEqual(marker_count, 1)


if __name__ == "__main__":
    unittest.main()
