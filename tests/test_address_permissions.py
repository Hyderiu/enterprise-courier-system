import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.courier import AddressCreate, create_address, delete_address, list_addresses
from api.routes.orders import OrderCreate, create_order
from database import models
from database.connection import Base
from utils.sender_addresses import (
    assert_sender_address_allowed,
    ensure_company_sender_addresses,
)


class AddressPermissionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user1 = models.User(name="员工甲", dingtalk_userid="u1", role="user")
        self.user2 = models.User(name="员工乙", dingtalk_userid="u2", role="user")
        self.db.add_all([self.user1, self.user2])
        self.db.commit()
        self.db.refresh(self.user1)
        self.db.refresh(self.user2)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def request_for(user):
        return SimpleNamespace(state=SimpleNamespace(current_user={
            "id": user.id, "name": user.name, "role": "employee",
        }))

    def test_personal_addresses_are_isolated_and_cross_delete_is_blocked(self):
        request1 = self.request_for(self.user1)
        request2 = self.request_for(self.user2)
        first = create_address(AddressCreate(
            role="receiver", label="甲的地址", name="甲", phone="13800000001",
            province="福建省", city="厦门市", district="思明区", detail="甲地址",
        ), request1, self.db)
        create_address(AddressCreate(
            role="receiver", label="乙的地址", name="乙", phone="13800000002",
            province="福建省", city="厦门市", district="湖里区", detail="乙地址",
        ), request2, self.db)

        result1 = list_addresses(request1, "personal", "receiver", self.db)
        result2 = list_addresses(request2, "personal", "receiver", self.db)
        self.assertEqual([item["label"] for item in result1["items"]], ["甲的地址"])
        self.assertEqual([item["label"] for item in result2["items"]], ["乙的地址"])
        with self.assertRaises(HTTPException) as raised:
            delete_address(first["id"], request2, self.db)
        self.assertEqual(raised.exception.status_code, 404)

    def test_personal_address_book_rejects_sender_entries(self):
        request = self.request_for(self.user1)
        with self.assertRaises(HTTPException):
            create_address(AddressCreate(
                role="sender", name="甲", phone="13800000001", detail="寄件地址",
            ), request, self.db)
        with self.assertRaises(HTTPException):
            list_addresses(request, "personal", "sender", self.db)

    def test_order_and_requested_receiver_address_are_saved_together(self):
        self.user1.role = "admin"
        self.db.commit()
        request = self.request_for(self.user1)
        request.state.current_user["role"] = "admin"
        # 本用例只验证本地事务，不应依赖当前 .env 中的真实快递账号和外网报价。
        with patch("api.routes.courier.require_selected_service_quote", return_value={
            "id": "db_small_fast", "estimated_fee": 10,
            "estimated_delivery": "明日达", "quote_available": True,
        }):
            result = create_order(OrderCreate(
                courier_type="DB", express_type="XJTK",
                sender_name="寄件人", sender_phone="13800000000",
                sender_province="福建省", sender_city="厦门市", sender_district="思明区",
                sender_address="示例园区",
                receiver_name="收件人", receiver_phone="13900000000",
                receiver_province="福建省", receiver_city="厦门市", receiver_district="湖里区",
                receiver_address="测试路2号", item_name="文件", item_weight=1,
                save_receiver_address=True,
            ), request, BackgroundTasks(), self.db)
        self.assertTrue(result["receiver_address_saved"])
        saved = self.db.query(models.Address).filter_by(
            user_id=self.user1.id, scope="personal", role="receiver"
        ).one()
        self.assertEqual(saved.detail, "测试路2号")

    def test_unapproved_custom_sender_address_is_rejected(self):
        ensure_company_sender_addresses(self.db)
        current_user = {"id": self.user1.id, "role": "employee"}
        allowed = self.db.query(models.Address).filter(
            models.Address.scope == "company", models.Address.role == "sender"
        ).first()
        assert_sender_address_allowed(self.db, current_user, {
            "sender_company": allowed.company_name,
            "sender_province": allowed.province,
            "sender_city": allowed.city,
            "sender_district": allowed.district,
            "sender_address": allowed.detail,
        })
        with self.assertRaises(HTTPException) as raised:
            assert_sender_address_allowed(self.db, current_user, {
                "sender_company": "个人公司", "sender_province": "福建省",
                "sender_city": "厦门市", "sender_district": "思明区",
                "sender_address": "自定义地址",
            })
        self.assertEqual(raised.exception.status_code, 403)

    def test_member_permission_allows_custom_sender_without_saving_company_address(self):
        ensure_company_sender_addresses(self.db)
        original_count = self.db.query(models.Address).filter(
            models.Address.scope == "company", models.Address.role == "sender"
        ).count()
        self.user1.allow_custom_sender_address = True
        self.db.commit()
        assert_sender_address_allowed(self.db, {"id": self.user1.id, "role": "employee"}, {
            "sender_company": "个人公司", "sender_province": "福建省",
            "sender_city": "厦门市", "sender_district": "思明区",
            "sender_address": "只用于本次订单的地址",
        })
        self.assertEqual(self.db.query(models.Address).filter(
            models.Address.scope == "company", models.Address.role == "sender"
        ).count(), original_count)


if __name__ == "__main__":
    unittest.main()
