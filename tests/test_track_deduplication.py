import datetime as dt
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.orders import _save_new_tracks, _serialize_unique_tracks
from database import models
from database.connection import Base


class TrackDeduplicationTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        self.engine = engine
        self.db = sessionmaker(bind=engine)()
        self.order = models.Order(
            order_no="ORDER-1", courier_type="SF", courier_order_no="SF-1",
            sender_name="寄件人", sender_phone="13800000000", sender_address="寄件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="收件地址", item_name="文件",
        )
        self.db.add(self.order)
        self.db.flush()
        self.db.add(models.CourierTrack(
            order_id=self.order.id,
            courier_order_no="SF-1",
            track_time=dt.datetime(2026, 9, 24, 15, 0),
            track_status="已收取快件",
            track_detail="顺丰速运 已收取快件",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_refresh_only_inserts_new_state(self):
        incoming = [
            {
                "time": "2026-09-24T15:30:00",
                "status": "已收取快件",
                "detail": "顺丰速运  已收取快件",
            },
            {
                "time": "2026-09-24T16:00:00",
                "status": "运输中",
                "detail": "快件已发往下一站",
            },
        ]
        inserted = _save_new_tracks(self.db, self.order, incoming)
        self.db.commit()
        self.assertEqual(len(inserted), 1)
        self.assertEqual(
            self.db.query(models.CourierTrack).filter_by(order_id=self.order.id).count(), 2
        )

    def test_existing_duplicate_rows_are_collapsed_for_display(self):
        duplicate = models.CourierTrack(
            order_id=self.order.id,
            courier_order_no="SF-1",
            track_time=dt.datetime(2026, 9, 24, 15, 30),
            track_status="已收取快件",
            track_detail="顺丰速运 已收取快件",
        )
        self.db.add(duplicate)
        self.db.commit()
        rows = self.db.query(models.CourierTrack).filter_by(
            order_id=self.order.id
        ).order_by(models.CourierTrack.track_time.desc()).all()
        self.assertEqual(len(_serialize_unique_tracks(rows)), 1)


if __name__ == "__main__":
    unittest.main()
