import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from fastapi import BackgroundTasks

from api.auth_middleware import needs_admin_role
from dingtalk.client import DingTalkClient
from utils.approval_notifications import (
    build_approval_notification, build_approval_progress_notification,
    build_approval_result_notification, build_dispatch_notification,
    build_feedback_notification,
    dingtalk_authenticated_url, dingtalk_workbench_url,
    queue_approval_notification,
    send_approval_notification,
)
from utils.dingtalk_notification_auth import (
    create_notification_login_token, verify_notification_login_token,
)


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.rows.pop(0)


class _DB:
    def __init__(self, rows):
        self.rows = list(rows)

    def query(self, *_args, **_kwargs):
        return _Query(self.rows)


class DingTalkApprovalNotificationTests(unittest.TestCase):
    def test_feedback_page_uses_its_own_level2_permission_check(self):
        self.assertFalse(needs_admin_role("/admin/feedback"))
        self.assertFalse(needs_admin_role("/api/admin/feedback/27/resolve"))
        self.assertTrue(needs_admin_role("/admin/settings"))

    def test_client_sends_single_action_card(self):
        client = DingTalkClient()
        with patch("dingtalk.client.settings.dingtalk_agent_id", "123"), patch.object(
            client, "post", return_value={"errcode": 0, "task_id": 9}
        ) as post:
            response = client.send_work_notification(
                "manager-user", "待审批", "### 内容", "打开审批页面", "https://example.test/admin/approvals"
            )

        self.assertEqual(response["task_id"], 9)
        path, payload = post.call_args.args
        self.assertEqual(path, "/topapi/message/corpconversation/asyncsend_v2")
        self.assertEqual(payload["agent_id"], 123)
        self.assertEqual(payload["userid_list"], "manager-user")
        self.assertEqual(payload["msg"]["msgtype"], "action_card")
        self.assertEqual(payload["msg"]["action_card"]["single_title"], "打开审批页面")

    def test_order_notification_targets_current_approver_and_deep_links(self):
        approver = SimpleNamespace(id=3, dingtalk_userid="manager-user")
        applicant = SimpleNamespace(name="演示员工")
        record = SimpleNamespace(
            id=18, order_no="CO18", receiver_name="演示收件人甲", created_by=7,
            approval_current_level=2, approval_level2_user_id=3,
        )
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            job, reason = build_approval_notification(_DB([approver, applicant]), record, "order")

        self.assertIsNone(reason)
        self.assertEqual(job["userid"], "manager-user")
        self.assertIn("演示员工", job["markdown"])
        self.assertTrue(job["action_url"].startswith("dingtalk://dingtalkclient/page/link?url="))
        decoded = unquote(unquote(job["action_url"]))
        self.assertIn("/dingtalk-notification-entry?token=", decoded)
        self.assertNotIn("/dingtalk-entry?", decoded)
        self.assertNotIn("pc_slide=true", job["action_url"])

    def test_workbench_link_requests_large_dingtalk_container(self):
        result = dingtalk_workbench_url("https://express.example/admin/approvals?order_id=7")
        self.assertIn("order_id%3D7%26ddtab%3Dtrue", result)

    def test_authenticated_link_runs_sso_before_deep_link(self):
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"), patch(
            "utils.approval_notifications.settings.dingtalk_corp_id", "corp-1"
        ):
            result = dingtalk_authenticated_url("/admin/approvals?order_id=7")
        decoded = unquote(unquote(result))
        self.assertIn("https://express.example/dingtalk-entry", decoded)
        self.assertIn("next=/admin/approvals?order_id=7", decoded)
        self.assertIn("corpid=corp-1", decoded)

    def test_result_notification_targets_applicant_and_contains_opinion(self):
        applicant = SimpleNamespace(id=7, dingtalk_userid="employee-user")
        approver = SimpleNamespace(name="审批主管")
        record = SimpleNamespace(
            id=18, order_no="CO18", created_by=7, approver_id=3,
        )
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            job, reason = build_approval_result_notification(
                _DB([applicant, approver]), record, False, "地址不完整", "order"
            )
        self.assertIsNone(reason)
        self.assertEqual(job["userid"], "employee-user")
        self.assertIn("未通过", job["markdown"])
        self.assertIn("地址不完整", job["markdown"])
        self.assertIn("/dingtalk-notification-entry?token=", unquote(unquote(job["action_url"])))

    def test_submission_progress_names_current_stage_and_approver(self):
        applicant = SimpleNamespace(id=7, dingtalk_userid="employee-user")
        approver = SimpleNamespace(id=3, name="二级审批人")
        record = SimpleNamespace(
            id=18, order_no="CO18", created_by=7,
            approval_current_level=2, approval_level2_user_id=3,
        )
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            job, reason = build_approval_progress_notification(
                _DB([applicant, approver]), record, "submitted", "order"
            )
        self.assertIsNone(reason)
        self.assertIn("提交成功", job["title"])
        self.assertIn("第 2 级审批", job["markdown"])
        self.assertIn("二级审批人", job["markdown"])

    def test_dispatch_result_is_sent_to_applicant_and_names_courier(self):
        applicant = SimpleNamespace(id=7, dingtalk_userid="employee-user")
        record = SimpleNamespace(
            id=18, order_no="CO18", courier_order_no="SF123456",
            courier_type="SF", created_by=7,
        )
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            job, reason = build_dispatch_notification(
                _DB([applicant]), record, {"ok": True}, "order"
            )
        self.assertIsNone(reason)
        self.assertEqual(job["userid"], "employee-user")
        self.assertIn("顺丰速运下单成功", job["title"])
        self.assertIn("快递公司：顺丰速运", job["markdown"])
        self.assertIn("SF123456", job["markdown"])
        self.assertEqual(job["action_title"], "查看订单详情")
        self.assertIn("/dingtalk-notification-entry?token=", unquote(unquote(job["action_url"])))

    def test_dispatch_titles_name_each_supported_courier(self):
        labels = {
            "SF": "顺丰速运", "SF_CITY": "顺丰同城急送",
            "DB": "德邦快递", "JDL": "京东物流", "JD": "京东物流",
        }
        for courier_type, label in labels.items():
            with self.subTest(courier_type=courier_type), patch(
                "utils.approval_notifications.settings.public_base_url",
                "https://express.example",
            ):
                applicant = SimpleNamespace(id=7, dingtalk_userid="employee-user")
                record = SimpleNamespace(
                    id=18, order_no="CO18", courier_order_no="WAYBILL-1",
                    courier_type=courier_type, created_by=7,
                )
                job, reason = build_dispatch_notification(
                    _DB([applicant]), record, {"ok": True}, "order"
                )
                self.assertIsNone(reason)
                self.assertIn(f"{label}下单成功", job["title"])
                self.assertIn(f"快递公司：{label}", job["markdown"])

    def test_feedback_notification_targets_level2_reviewer_with_direct_login(self):
        reviewer = SimpleNamespace(id=9, dingtalk_userid="level2-user")
        applicant = SimpleNamespace(name="反馈员工")
        feedback = SimpleNamespace(id=27, user_id=7, content="希望异常订单可以编辑后重新提交")
        with patch(
            "utils.approval_notifications.get_approval_policy",
            return_value={"level2_user_id": 9},
        ), patch(
            "utils.approval_notifications.settings.public_base_url",
            "https://express.example",
        ):
            job, reason = build_feedback_notification(
                _DB([reviewer, applicant]), feedback
            )

        self.assertIsNone(reason)
        self.assertEqual(job["userid"], "level2-user")
        self.assertIn("反馈员工", job["markdown"])
        decoded = unquote(unquote(job["action_url"]))
        self.assertIn("/dingtalk-notification-entry?token=", decoded)
        self.assertNotIn("/dingtalk-entry?", decoded)
        self.assertEqual(job["action_title"], "打开意见处理页面")

    def test_batch_notification_is_queued_without_calling_network_inline(self):
        approver = SimpleNamespace(id=3, dingtalk_userid="manager-user")
        applicant = SimpleNamespace(name="申请人")
        record = SimpleNamespace(
            id=6, batch_no="BA6", total_count=20, created_by=2,
            approval_current_level=1, approval_level1_user_id=3,
        )
        background = BackgroundTasks()
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            result = queue_approval_notification(background, _DB([approver, applicant]), record, "batch")

        self.assertEqual(result, {"queued": True})
        self.assertEqual(len(background.tasks), 1)

    def test_send_failure_is_best_effort(self):
        with patch("utils.approval_notifications.DingTalkClient") as client_cls:
            client_cls.return_value.send_work_notification.side_effect = RuntimeError("permission denied")
            result = send_approval_notification({
                "userid": "u1", "title": "t", "markdown": "m",
                "action_title": "open", "action_url": "https://example.test",
            })
        self.assertFalse(result["sent"])
        self.assertIn("permission denied", result["reason"])

    def test_notification_login_token_is_recipient_bound_and_expires(self):
        with patch("utils.dingtalk_notification_auth.settings.session_secret", "test-secret"):
            token = create_notification_login_token(
                7, "employee-user", "/orders?order_id=18", now=100
            )
            payload = verify_notification_login_token(token, now=101)
            self.assertEqual(payload["uid"], 7)
            self.assertEqual(payload["did"], "employee-user")
            self.assertEqual(payload["target"], "/orders?order_id=18")
            with self.assertRaisesRegex(ValueError, "无效或已过期"):
                verify_notification_login_token(token + "x", now=101)
            with self.assertRaisesRegex(ValueError, "无效或已过期"):
                verify_notification_login_token(token, now=payload["exp"] + 1)


if __name__ == "__main__":
    unittest.main()
