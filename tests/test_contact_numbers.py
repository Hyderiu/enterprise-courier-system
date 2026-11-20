import unittest


class ContactNumberTests(unittest.TestCase):
    def test_order_accepts_landline_and_extension(self):
        from api.routes.orders import OrderCreate

        order = OrderCreate(
            courier_type="SF",
            sender_name="寄件人", sender_phone="0592-1234567 转 801",
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="测试路1号",
            receiver_name="政府单位", receiver_phone="010-12345678",
            receiver_province="北京市", receiver_city="北京市", receiver_district="朝阳区",
            receiver_address="测试路2号", item_name="文件", item_weight=1,
        )
        self.assertEqual(order.sender_phone, "0592-1234567 转 801")
        self.assertEqual(order.receiver_phone, "010-12345678")

    def test_personal_address_accepts_landline(self):
        from api.routes.courier import AddressCreate

        address = AddressCreate(
            name="政府单位", phone="010-12345678", detail="测试路2号",
        )
        self.assertEqual(address.phone, "010-12345678")

    def test_contact_is_still_required(self):
        from utils.contact import normalize_contact_number

        with self.assertRaisesRegex(ValueError, "联系电话"):
            normalize_contact_number("   ")

    def test_address_parser_extracts_labeled_landline(self):
        from utils.address_parser import parse_recipient

        result = parse_recipient("某政府部门 电话：010-12345678 北京市朝阳区测试路1号")
        self.assertEqual(result["phone"], "010-12345678")


if __name__ == "__main__":
    unittest.main()
