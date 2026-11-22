import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import models
from database.connection import Base, get_db
from api.main import app
from config.settings import settings
from utils.billing_allocation import (
    allocation_output_path, import_billing_statement, list_allocation_manifests,
)


class BillingAllocationTests(unittest.TestCase):
    @staticmethod
    def _assert_strict_shared_strings(path, expected_column_count):
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            assert "xl/sharedStrings.xml" in names
            sheet_xml = archive.read("xl/worksheets/sheet1.xml")
        assert b"inlineStr" not in sheet_xml
        root = ElementTree.fromstring(sheet_xml)
        row = next(
            item for item in root.iter(namespace + "row")
            if item.get("r") == "2"
        )
        cells = list(row)
        assert len(cells) == expected_column_count
        assert all(cell.get("t") == "s" for cell in cells)
        assert all(cell.get("s") is not None for cell in cells)

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        root = models.Department(
            dept_code="0000", dept_name="公司", level=1, enabled=True,
        )
        self.db.add(root)
        self.db.flush()
        project_department = models.Department(
            dept_code="P-DEPT", dept_name="项目当前部门", parent_id=root.id,
            level=2, enabled=True,
        )
        order_department = models.Department(
            dept_code="D-DEPT", dept_name="部门当前名称", parent_id=root.id,
            dingtalk_dept_id="RAW-DINGTALK-DEPT", level=2, enabled=True,
        )
        self.db.add_all([project_department, order_department])
        self.db.add(models.Project(
            project_code="P-001", project_name="CRM当前项目名称",
            full_path="P-001", dept_code="P-DEPT",
            dept_name="公司-项目当前部门", enabled=True,
        ))
        self.user = models.User(
            dingtalk_userid="billing-user", name="下单员工甲",
            role="user", is_active=True,
        )
        self.db.add(self.user)
        self.db.flush()
        self.db.add_all([
            self._order("SF-P", "SF", "project", project_code="P-001",
                        project_name="下单时项目名称",
                        dept_code="OLD", dept_name="下单时旧项目部门",
                        created_by=self.user.id),
            self._order("SF-D", "SF", "department", dept_code="RAW-DINGTALK-DEPT",
                        dept_name="下单时选择的部门", created_by=self.user.id),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _order(waybill, courier, cost_type, project_code=None, project_name=None,
               dept_code=None, dept_name=None, created_by=None):
        return models.Order(
            order_no=f"ORDER-{waybill}", courier_type=courier,
            courier_order_no=waybill, region_code="xiamen_other",
            sender_name="寄件人", sender_phone="13800000000", sender_address="地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建", receiver_city="厦门", receiver_address="地址",
            item_name="文件", cost_type=cost_type, project_code=project_code,
            project_name=project_name,
            dept_code=dept_code, dept_name=dept_name, status="ordered",
            created_by=created_by,
            created_at=datetime(2026, 8, 15, 10, 0),
        )

    @staticmethod
    def _statement(sheet_name, headers, rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = sheet_name
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_sf_import_matches_each_fee_line_and_uses_current_crm_project_department(self):
        data = self._statement(
            "账单明细", ["序号", "运单号码", "服务", "应付金额"], [
                [1, "SF-P", "运费", 10],
                [2, "SF-P", "包装服务", 2],
                [3, "SF-D", "运费", 7],
                [4, "SF-X", "运费", 4],
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = import_billing_statement(
                self.db, data, "顺丰账单.xlsx", "2026-08", "xiamen_other", "SF", directory,
            )
            self.assertEqual(manifest["row_count"], 4)
            self.assertEqual(manifest["unique_waybill_count"], 3)
            self.assertEqual(manifest["matched_rows"], 3)
            self.assertEqual(manifest["unmatched_rows"], 1)
            self.assertEqual(manifest["total_amount"], 23.0)
            self.assertEqual(manifest["matched_amount"], 19.0)
            self.assertEqual(manifest["outputs"]["project"]["amount"], 12.0)
            self.assertEqual(manifest["outputs"]["department"]["amount"], 7.0)

            project_path, project_name = allocation_output_path(
                "2026-08", "xiamen_other", "SF", "project", directory
            )
            self.assertEqual(
                project_name, "202608厦门及其他区域顺丰快递费用项目分摊.xlsx"
            )
            project_book = load_workbook(project_path, data_only=True)
            self.assertEqual(project_book.sheetnames, ["分摊信息导入模板"])
            self.assertEqual(
                [project_book.active.cell(1, col).value for col in range(1, 6)],
                ["部门代码", "项目编号", "项目名称", "承担金额", "备注"],
            )
            self.assertEqual(
                [project_book.active.cell(2, col).value for col in range(1, 6)],
                ["P-DEPT", "P-001", "下单时项目名称", "12.00", ""],
            )
            for column in range(1, 5):
                self.assertEqual(project_book.active.cell(2, column).data_type, "s")
                self.assertEqual(project_book.active.cell(2, column).number_format, "@")
            self._assert_strict_shared_strings(project_path, 5)

            department_path, department_name = allocation_output_path(
                "2026-08", "xiamen_other", "SF", "department", directory
            )
            self.assertEqual(
                department_name, "202608厦门及其他区域顺丰快递费用部门分摊.xlsx"
            )
            department_book = load_workbook(department_path, data_only=True)
            self.assertEqual(department_book.sheetnames, ["分摊信息导入模板"])
            self.assertEqual(
                [department_book.active.cell(1, col).value for col in range(1, 4)],
                ["部门代码", "承担金额", "备注"],
            )
            self.assertEqual(
                [department_book.active.cell(2, col).value for col in range(1, 4)],
                ["D-DEPT", "7.00", ""],
            )
            for column in range(1, 3):
                self.assertEqual(department_book.active.cell(2, column).data_type, "s")
                self.assertEqual(department_book.active.cell(2, column).number_format, "@")
            self._assert_strict_shared_strings(department_path, 3)

            detail_path, detail_name = allocation_output_path(
                "2026-08", "xiamen_other", "SF", "detail", directory
            )
            self.assertEqual(
                detail_name, "202608厦门及其他区域顺丰快递费用分摊明细.xlsx"
            )
            detail_book = load_workbook(detail_path, data_only=True)
            self.assertEqual(detail_book.sheetnames, ["账单明细"])
            detail = detail_book["账单明细"]
            self.assertEqual(
                [detail.cell(1, col).value for col in range(2, 7)],
                ["运单号码", "下单员工", "分摊方式", "归属部门", "归属项目"],
            )
            self.assertEqual(
                [detail.cell(2, col).value for col in range(3, 7)],
                ["下单员工甲", "项目分摊", "公司-项目当前部门", "下单时项目名称"],
            )
            self.assertEqual(
                [detail.cell(4, col).value for col in range(3, 7)],
                ["下单员工甲", "部门分摊", "下单时选择的部门", None],
            )
            self.assertEqual(detail.cell(5, 4).value, "未匹配运单")
            self.assertIn("SF", list_allocation_manifests(
                "2026-08", "xiamen_other", directory
            ))

    def test_deppon_and_jdl_use_their_payable_amount_columns(self):
        cases = (
            ("DB", "本期快递明细", ["运单号", "本期应收金额", "剩余应还金额"],
             ["DB-1", 20, 18], 18.0),
            ("JDL", "明细_1", ["运单号", "原始金额", "结算金额"],
             ["JD-1", 30, 24], 24.0),
        )
        with tempfile.TemporaryDirectory() as directory:
            for courier, sheet_name, headers, row, expected in cases:
                with self.subTest(courier=courier):
                    self.db.add(self._order(
                        row[0], courier, "department", dept_code="D-DEPT",
                        dept_name="下单时选择的部门", created_by=self.user.id,
                    ))
                    self.db.commit()
                    manifest = import_billing_statement(
                        self.db, self._statement(sheet_name, headers, [row]),
                        f"{courier}.xlsx", "2026-08", "xiamen_other", courier, directory,
                    )
                    self.assertEqual(manifest["matched_amount"], expected)
                    self.assertEqual(manifest["outputs"]["department"]["amount"], expected)

    def test_rejects_wrong_carrier_template(self):
        data = self._statement("明细_1", ["运单号", "结算金额"], [["JD-1", 10]])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "顺丰费用明细表"):
                import_billing_statement(
                    self.db, data, "错误.xlsx", "2026-08", "xiamen_other", "SF", directory,
                )

    def test_admin_page_import_status_and_download_flow(self):
        data = self._statement(
            "账单明细", ["运单号码", "应付金额"], [["SF-D", 7]],
        )
        original_login = settings.dingtalk_login_enabled
        original_output = settings.billing_allocation_dir

        def override_db():
            yield self.db

        with tempfile.TemporaryDirectory() as directory:
            settings.dingtalk_login_enabled = False
            settings.billing_allocation_dir = directory
            app.dependency_overrides[get_db] = override_db
            try:
                client = TestClient(app)
                page = client.get(
                    "/admin/monthly-exports?month=2026-08&region=xiamen_other"
                )
                self.assertEqual(page.status_code, 200)
                self.assertIn("快递费用分摊", page.text)
                self.assertIn("上传账单", page.text)

                imported = client.post(
                    "/api/admin/billing-allocations/import",
                    data={
                        "month": "2026-08", "region": "xiamen_other",
                        "courier_type": "SF",
                    },
                    files={
                        "file": (
                            "顺丰账单.xlsx", data,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
                self.assertEqual(imported.status_code, 200, imported.text)
                self.assertEqual(imported.json()["manifest"]["matched_rows"], 1)

                status = client.get(
                    "/api/admin/billing-allocations/status",
                    params={"month": "2026-08", "region": "xiamen_other"},
                )
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["items"]["SF"]["matched_amount"], 7.0)

                workbench = client.get(
                    "/api/admin/billing-allocations/workbench",
                    params={
                        "month": "2026-08", "region": "all",
                        "courier_type": "all",
                    },
                )
                self.assertEqual(workbench.status_code, 200, workbench.text)
                self.assertEqual(workbench.json()["total"], 1)
                self.assertEqual(workbench.json()["items"][0]["allocation_type"], "department")

                previous_data = self._statement(
                    "账单明细", ["运单号码", "应付金额"], [["SF-D", 5]],
                )
                previous_import = client.post(
                    "/api/admin/billing-allocations/import",
                    data={
                        "month": "2026-07", "region": "xiamen_other",
                        "courier_type": "SF",
                    },
                    files={
                        "file": (
                            "顺丰上月账单.xlsx", previous_data,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )
                self.assertEqual(previous_import.status_code, 200, previous_import.text)

                visualization = client.get(
                    "/api/admin/billing-allocations/visualization",
                    params={
                        "start_month": "2026-08", "end_month": "2026-09",
                        "region": "all", "courier_type": "all",
                    },
                )
                self.assertEqual(visualization.status_code, 200, visualization.text)
                series = visualization.json()["trend_series"]
                self.assertEqual([item["code"] for item in series], ["SF", "DB", "JDL"])
                self.assertEqual(series[0]["points"][0]["amount"], 7.0)
                self.assertEqual(series[0]["points"][0]["waybill_count"], 1)
                self.assertEqual(series[0]["points"][0]["mom_change_pct"], 40.0)
                self.assertIsNone(series[0]["points"][1]["mom_change_pct"])
                chart_page = client.get(
                    "/admin/billing-visualization?start_month=2026-08&end_month=2026-09"
                )
                self.assertIn("bv-line-svg", chart_page.text)
                self.assertIn("快递单数", chart_page.text)

                self.assertIn("运单补充记录", client.get("/waybill-records").text)
                self.assertIn("运单补充记录管理", client.get("/admin/waybill-records").text)

                downloaded = client.get(
                    "/api/admin/billing-allocations/2026-08/xiamen_other/SF/department"
                )
                self.assertEqual(downloaded.status_code, 200)
                self.assertTrue(downloaded.content.startswith(b"PK"))

                # 工作台是导出与可视化的单一数据源：改、增、删均立即生效。
                current = client.get(
                    "/api/admin/billing-allocations/workbench",
                    params={
                        "month": "2026-08", "region": "xiamen_other",
                        "courier_type": "SF",
                    },
                ).json()
                item = current["items"][0]
                statement_id = current["statements"][0]["statement_id"]
                edited_raw = dict(item["raw_data"])
                edited_raw["应付金额"] = 11
                edited = client.patch(
                    f"/api/admin/billing-allocations/lines/{item['id']}/details",
                    json={
                        "raw_data": edited_raw, "allocation_type": "department",
                        "submitter_user_id": self.user.id,
                        "dept_code": "D-DEPT", "project_code": None,
                    },
                )
                self.assertEqual(edited.status_code, 200, edited.text)

                added_raw = dict(edited_raw)
                added_raw["运单号码"] = "SF-ADDED"
                added_raw["应付金额"] = 4
                added = client.post(
                    f"/api/admin/billing-allocations/statements/{statement_id}/lines",
                    json={
                        "raw_data": added_raw, "allocation_type": "department",
                        "submitter_user_id": self.user.id,
                        "dept_code": "D-DEPT", "project_code": None,
                    },
                )
                self.assertEqual(added.status_code, 200, added.text)
                added_id = added.json()["item"]["id"]

                changed_chart = client.get(
                    "/api/admin/billing-allocations/visualization",
                    params={
                        "start_month": "2026-08", "end_month": "2026-08",
                        "region": "xiamen_other", "courier_type": "SF",
                    },
                ).json()
                self.assertEqual(changed_chart["summary"]["total_amount"], 15.0)
                self.assertEqual(changed_chart["summary"]["row_count"], 2)

                deleted = client.delete(
                    f"/api/admin/billing-allocations/lines/{item['id']}"
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                after_delete = client.get(
                    "/api/admin/billing-allocations/visualization",
                    params={
                        "start_month": "2026-08", "end_month": "2026-08",
                        "region": "xiamen_other", "courier_type": "SF",
                    },
                ).json()
                self.assertEqual(after_delete["summary"]["total_amount"], 4.0)
                self.assertEqual(after_delete["summary"]["row_count"], 1)
                self.assertEqual(
                    self.db.query(models.BillingStatementLine).filter(
                        models.BillingStatementLine.id == added_id,
                    ).count(), 1,
                )
                self.assertEqual(
                    client.delete(
                        f"/api/admin/billing-allocations/lines/{added_id}"
                    ).status_code, 200,
                )
                empty_workbench = client.get(
                    "/api/admin/billing-allocations/workbench",
                    params={
                        "month": "2026-08", "region": "xiamen_other",
                        "courier_type": "SF",
                    },
                ).json()
                self.assertEqual(empty_workbench["total"], 0)
                self.assertIn("运单号码", empty_workbench["raw_columns"])
            finally:
                app.dependency_overrides.pop(get_db, None)
                settings.dingtalk_login_enabled = original_login
                settings.billing_allocation_dir = original_output


if __name__ == "__main__":
    unittest.main()
