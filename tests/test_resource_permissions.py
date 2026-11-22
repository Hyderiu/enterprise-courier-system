import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from api.routes.batch import _assert_batch_owner, _assert_batch_readable
from api.routes.orders import _assert_order_mutable, _assert_order_readable


def request_for(user_id, role="employee", auth_enabled=True):
    return SimpleNamespace(state=SimpleNamespace(
        auth_enabled=auth_enabled,
        current_user={"id": user_id, "role": role},
    ))


def record(owner=1, level1=3, level2=4):
    return SimpleNamespace(
        created_by=owner,
        approver_id=None,
        approval_level1_user_id=level1,
        approval_level2_user_id=level2,
    )


class ResourcePermissionTests(unittest.TestCase):
    def test_other_employee_cannot_read_or_mutate_order(self):
        order = record(owner=1)
        with self.assertRaises(HTTPException) as read_error:
            _assert_order_readable(order, request_for(2))
        self.assertEqual(read_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as mutation_error:
            _assert_order_mutable(order, request_for(2))
        self.assertEqual(mutation_error.exception.status_code, 403)

    def test_order_approver_can_read_but_not_mutate(self):
        order = record(owner=1, level1=3)
        _assert_order_readable(order, request_for(3))
        with self.assertRaises(HTTPException):
            _assert_order_mutable(order, request_for(3))

    def test_owner_and_admin_can_mutate_order(self):
        order = record(owner=1)
        _assert_order_mutable(order, request_for(1))
        _assert_order_mutable(order, request_for(99, role="admin"))

    def test_other_employee_cannot_read_or_mutate_batch(self):
        batch = record(owner=1)
        with self.assertRaises(HTTPException):
            _assert_batch_readable(batch, request_for(2))
        with self.assertRaises(HTTPException):
            _assert_batch_owner(batch, request_for(2))

    def test_batch_approver_can_read_but_only_owner_can_mutate(self):
        batch = record(owner=1, level2=4)
        _assert_batch_readable(batch, request_for(4))
        with self.assertRaises(HTTPException):
            _assert_batch_owner(batch, request_for(4))
        _assert_batch_owner(batch, request_for(1))


if __name__ == "__main__":
    unittest.main()
