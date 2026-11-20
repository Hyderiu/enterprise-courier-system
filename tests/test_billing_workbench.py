import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.manual_waybills import (
    ManualWaybillBody, ManualWaybillDecision, approve_manual_waybill,
    create_manual_waybill, reject_manual_waybill, update_manual_waybill,
    withdraw_manual_waybill,
)
from config.settings import settings
from database import models
from database.connection import Base
from utils.billing_allocation import import_billing_statement
from utils.billing_workbench import (
    _department_allocation, _reference_maps, assign_statement_line,
    save_uploaded_statement,
)


class BillingWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        root = models.Department(dept_code="0000", dept_name="公司", enabled=True)
        self.db.add(root)
        self.db.flush()
        department = models.Department(
            dept_code="D-001", dept_name="行政部", parent_id=root.id,
            dingtalk_dept_id="RAW-D-001", level=2, enabled=True,
        )
        project_department = models.Department(
            dept_code="P-DEP", dept_name="项目部", parent_id=root.id,
            dingtalk_dept_id="RAW-P-DEP", level=2, enabled=True,
        )
        self.db.add_all([department, project_department])
        self.db.add(models.Project(
            project_code="P-001", project_name="测试项目", dept_code="RAW-P-DEP",
            dept_name="公司-项目部", enabled=True,
        ))
        self.db.add(models.Project(
            project_code="P-NO-DEPT", project_name="未配置部门项目",
            dept_code=None, dept_name=None, enabled=True,
        ))
        self.alice = models.User(
            dingtalk_userid="alice", name="员工甲", role="user", is_active=True,
        )
        self.bob = models.User(
            dingtalk_userid="bob", name="员工乙", role="user", is_active=True,
        )
        self.admin = models.User(
            dingtalk_userid="admin", name="管理员", role="admin", is_active=True,
        )
        self.db.add_all([self.alice, self.bob, self.admin])
        self.db.commit()
        self.db.add_all([
            models.SystemConfig(
                config_key="approval_level_count", config_value="2",
                description="测试审批级数",
            ),
            models.SystemConfig(
                config_key="approval_level2_user_id", config_value=str(self.admin.id),
                description="测试二级审核员",
            ),
        ])
        self.db.commit()
        self.temp = tempfile.TemporaryDirectory()
        self.original_output = settings.billing_allocation_dir
        settings.billing_allocation_dir = self.temp.name

    def tearDown(self):
        settings.billing_allocation_dir = self.original_output
        self.temp.cleanup()
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def request(user, role="employee", auth=True):
        return SimpleNamespace(state=SimpleNamespace(
            current_user={"id": user.id, "role": role}, auth_enabled=auth,
        ))

    @staticmethod
    def statement(rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "账单明细"
        sheet.append(["运单号码", "应付金额", "费用类型"])
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def upload(self, content, month="2026-10"):
        import_billing_statement(
            self.db, content, "顺丰账单.xlsx", month, "xiamen_other", "SF",
        )
        return save_uploaded_statement(
            self.db, content, "顺丰账单.xlsx", month, "xiamen_other", "SF",
            uploaded_by=self.admin.id,
        )

    def test_manual_record_only_matches_after_level2_approval(self):
        body = ManualWaybillBody(
            region_code="xiamen_other", courier_type="SF", waybill_no="SF-MANUAL",
            sender_name="员工甲", cost_type="project", project_code="P-001",
            created_by=self.bob.id,
        )
        created = create_manual_waybill(
            body, self.request(self.alice), self.db,
        )["item"]
        self.assertEqual(created["created_by"], self.alice.id)
        self.assertEqual(created["status"], "pending_approval")

        manifest = self.upload(self.statement([["SF-MANUAL", 12, "运费"]]))
        self.assertEqual(manifest["matched_rows"], 0)
        approve_manual_waybill(
            created["id"], ManualWaybillDecision(remark="核对无误"),
            self.request(self.admin, "admin"), self.db,
        )
        line = self.db.query(models.BillingStatementLine).one()
        self.db.refresh(line)
        self.assertEqual(line.match_source, "manual_waybill")
        self.assertEqual(line.submitter_user_id, self.alice.id)
        self.assertEqual(line.submitter_name, "员工甲")
        self.assertEqual(line.project_code, "P-001")
        self.assertEqual(line.dept_code, "P-DEP")

        withdraw_manual_waybill(
            created["id"], self.request(self.alice), self.db,
        )
        self.db.refresh(line)
        self.assertIsNone(line.allocation_type)
        self.assertIn("未找到", line.issue)

    def test_admin_can_create_for_selected_employee_without_retyping_name(self):
        body = ManualWaybillBody(
            region_code="xiamen_other", courier_type="SF",
            waybill_no="SF-ADMIN-CREATE", cost_type="department",
            dept_code="D-001", created_by=self.alice.id,
        )
        item = create_manual_waybill(
            body, self.request(self.admin, "admin"), self.db,
        )["item"]
        self.assertEqual(item["created_by"], self.alice.id)
        self.assertEqual(item["created_by_name"], "员工甲")
        record = self.db.query(models.ManualWaybillRecord).filter(
            models.ManualWaybillRecord.id == item["id"],
        ).one()
        self.assertEqual(record.sender_name, "员工甲")

    def test_edit_and_resubmit_clears_previous_level2_decision(self):
        body = ManualWaybillBody(
            region_code="xiamen_other", courier_type="SF", waybill_no="SF-REVIEW",
            sender_name="员工甲", cost_type="department", dept_code="D-001",
        )
        record_id = create_manual_waybill(
            body, self.request(self.alice), self.db,
        )["item"]["id"]
        reject_manual_waybill(
            record_id, ManualWaybillDecision(remark="请修改"),
            self.request(self.admin, "admin"), self.db,
        )
        changed = body.model_copy(update={"waybill_no": "SF-REVIEW-2"})
        item = update_manual_waybill(
            record_id, changed, self.request(self.alice), self.db,
        )["item"]
        self.assertEqual(item["status"], "pending_approval")
        self.assertIsNone(item["approval_level2_time"])
        self.assertIsNone(item["approval_level2_remark"])

    def test_legacy_dingtalk_department_id_resolves_to_current_crm_business_code(self):
        departments, paths, _ = _reference_maps(self.db)
        result = _department_allocation(
            "RAW-D-001", "旧订单部门", departments, paths,
        )
        self.assertEqual(result["dept_code"], "D-001")

    def test_employee_cannot_modify_another_employee_record_but_admin_can(self):
        body = ManualWaybillBody(
            region_code="xiamen_other", courier_type="DB", waybill_no="DB-1",
            sender_name="员工甲", cost_type="department", dept_code="D-001",
        )
        record_id = create_manual_waybill(
            body, self.request(self.alice), self.db,
        )["item"]["id"]
        changed = body.model_copy(update={
            "waybill_no": "DB-2", "sender_name": "员工乙",
        })
        with self.assertRaises(HTTPException) as denied:
            update_manual_waybill(record_id, changed, self.request(self.bob), self.db)
        self.assertEqual(denied.exception.status_code, 403)

        result = update_manual_waybill(
            record_id, changed, self.request(self.admin, "admin"), self.db,
        )
        self.assertEqual(result["item"]["waybill_no"], "DB-2")
        self.assertEqual(result["item"]["sender_name"], "员工甲")

    def test_admin_assignment_updates_duplicate_fee_rows_and_survives_reupload(self):
        content = self.statement([
            ["SF-EXTERNAL", 10, "运费"], ["SF-EXTERNAL", 2, "包装费"],
        ])
        manifest = self.upload(content)
        self.assertEqual(manifest["unmatched_rows"], 2)
        line = self.db.query(models.BillingStatementLine).order_by(
            models.BillingStatementLine.id,
        ).first()
        siblings, assigned = assign_statement_line(
            self.db, line.id, "department", "D-001",
            submitter_user_id=self.alice.id, updated_by=self.admin.id,
        )
        self.assertEqual(len(siblings), 2)
        self.assertEqual(assigned["outputs"]["department"]["amount"], 12.0)
        self.assertTrue(all(item.match_source == "manual_assignment" for item in siblings))
        self.assertTrue(all(item.submitter_user_id == self.alice.id for item in siblings))

        reuploaded = self.upload(content)
        self.assertEqual(reuploaded["unmatched_rows"], 0)
        persisted = self.db.query(models.BillingStatementLine).all()
        self.assertEqual(len(persisted), 2)
        self.assertTrue(all(item.dept_code == "D-001" for item in persisted))
        self.assertTrue(all(item.match_source == "manual_assignment" for item in persisted))

    def test_project_without_crm_department_can_be_assigned_a_department(self):
        order = models.Order(
            order_no="CO-NO-DEPT", courier_type="SF", region_code="xiamen_other",
            courier_order_no="SF-NO-DEPT", cost_type="project",
            project_code="P-NO-DEPT", project_name="未配置部门项目",
            sender_name="员工甲", sender_phone="13800000000",
            sender_province="福建省", sender_city="厦门市",
            sender_district="思明区", sender_address="测试地址",
            receiver_name="收件人", receiver_phone="13800000001",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_district="思明区", receiver_address="测试地址2",
            item_name="文件", item_weight=1, item_count=1, parcel_count=1,
            status="ordered", created_by=self.alice.id,
            created_at=datetime(2026, 10, 12, 9, 30),
        )
        self.db.add(order)
        self.db.commit()

        content = self.statement([["SF-NO-DEPT", 18, "运费"]])
        manifest = self.upload(content)
        self.assertEqual(manifest["unmatched_rows"], 1)
        line = self.db.query(models.BillingStatementLine).one()
        self.assertIsNone(line.allocation_type)
        self.assertEqual(line.project_code, "P-NO-DEPT")
        self.assertIn("CRM 项目未配置", line.issue)

        siblings, assigned = assign_statement_line(
            self.db, line.id, "project", "P-NO-DEPT",
            dept_code="D-001", updated_by=self.admin.id,
        )
        self.assertEqual(assigned["unmatched_rows"], 0)
        self.assertEqual(assigned["outputs"]["project"]["amount"], 18.0)
        self.assertEqual(siblings[0].dept_code, "D-001")
        self.assertEqual(siblings[0].project_code, "P-NO-DEPT")

        reuploaded = self.upload(content)
        self.assertEqual(reuploaded["unmatched_rows"], 0)
        persisted = self.db.query(models.BillingStatementLine).one()
        self.assertEqual(persisted.dept_code, "D-001")
        self.assertEqual(persisted.project_code, "P-NO-DEPT")


if __name__ == "__main__":
    unittest.main()
