import unittest
from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from utils.order_export import build_order_export


class OrderExportFormattingTests(unittest.TestCase):
    def test_export_uses_plain_excel_defaults_and_fits_columns(self):
        order = SimpleNamespace(
            cost_type="department", courier_type="SF", express_type_name="顺丰标快",
            order_time=None, created_at=None, created_by=1, courier_order_no="SF1234567890",
            dept_name="行政管理部", project_name="", track_status="在途",
            sender_name="寄件人", sender_phone="13800000000", sender_company="示例公司",
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例园区", receiver_name="收件人", receiver_phone="13900000000",
            receiver_company="客户公司", receiver_province="福建省", receiver_city="厦门市",
            receiver_district="湖里区", receiver_address="测试路一号",
            item_name="文件", item_weight=1.2, parcel_count=1, actual_freight=12.5,
            insurance=0, shipping_reason="资料寄送",
        )
        stream = build_order_export(
            [order], {1: {"name": "测试员工"}}, lambda _: "transit", {"transit": "在途"},
            region_name="厦门及其他", carrier_name="顺丰",
        )
        workbook = load_workbook(BytesIO(stream.read()), data_only=True)
        self.assertEqual(workbook.sheetnames, ["厦门及其他顺丰-部门", "厦门及其他顺丰-项目"])
        sheet = workbook["厦门及其他顺丰-部门"]

        self.assertIsNot(sheet.sheet_view.showGridLines, False)
        self.assertIsNone(sheet.freeze_panes)
        self.assertIsNone(sheet["A1"].fill.fill_type)
        self.assertFalse(sheet["A1"].font.bold)
        self.assertIsNone(sheet["A1"].border.bottom.style)
        self.assertEqual(sheet.auto_filter.ref, sheet.dimensions)
        self.assertGreaterEqual(sheet.column_dimensions["A"].width, 14)
        self.assertNotEqual(sheet.column_dimensions["A"].width,
                            sheet.column_dimensions["B"].width)
        headers = [cell.value for cell in sheet[1]]
        self.assertNotIn("预估重量(kg)", headers)
        self.assertNotIn("包裹数量", headers)
        self.assertNotIn("保价金额(元)", headers)
        self.assertIn("实际费用(元)", headers)

    def test_each_carrier_keeps_its_own_name_and_rows(self):
        base = dict(
            cost_type="department", order_time=None, created_at=None, created_by=1,
            dept_name="行政部", project_name="", track_status="已下单",
            sender_name="寄件人", sender_phone="13800000000", sender_company="示例公司",
            sender_province="北京市", sender_city="北京市", sender_district="朝阳区",
            sender_address="测试地址", receiver_name="收件人", receiver_phone="13900000000",
            receiver_company="客户", receiver_province="北京市", receiver_city="北京市",
            receiver_district="海淀区", receiver_address="测试地址",
            item_name="文件", item_weight=1, parcel_count=1, actual_freight=10,
            insurance=100, shipping_reason="资料寄送",
        )
        cases = (
            ("SF", "顺丰标快", "顺丰", "顺丰快递·顺丰标快"),
            ("DB", "小件标快", "德邦", "德邦快递·小件标快"),
            ("JDL", "京东标快", "京东", "京东快递·京东标快"),
        )
        for courier_type, product, carrier_name, expected in cases:
            with self.subTest(courier_type=courier_type):
                order = SimpleNamespace(
                    **base, courier_type=courier_type, express_type_name=product,
                    courier_order_no=f"{courier_type}-001",
                )
                stream = build_order_export(
                    [order], {1: {"name": "测试员工"}}, lambda _: "ordered",
                    {"ordered": "待揽收"}, region_name="北京", carrier_name=carrier_name,
                )
                workbook = load_workbook(BytesIO(stream.read()), data_only=True)
                department = workbook[f"北京{carrier_name}-部门"]
                headers = [cell.value for cell in department[1]]
                product_column = headers.index("快递产品") + 1
                self.assertEqual(department.cell(2, product_column).value, expected)
                self.assertNotIn("预估重量(kg)", headers)
                self.assertNotIn("包裹数量", headers)
                self.assertNotIn("保价金额(元)", headers)


if __name__ == "__main__":
    unittest.main()
