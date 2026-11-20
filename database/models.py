from sqlalchemy import Column, Integer, String, DateTime, Text, Float, Boolean, Index, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy import UniqueConstraint
from .connection import Base
from datetime import datetime, timedelta, timezone


def china_now():
    """Return a timezone-naive China Standard Time value for database columns."""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    dingtalk_userid = Column(String(64), unique=True, index=True)
    dingtalk_unionid = Column(String(64), unique=True, nullable=True)
    name = Column(String(50))
    phone = Column(String(20))
    avatar = Column(String(500))
    department = Column(String(100))
    role = Column(String(20), default="user")
    hrm_job_title = Column(String(50), nullable=True)
    work_location = Column(String(100), nullable=True)
    region_code = Column(String(30), default="xiamen_other", nullable=False, index=True)
    allow_custom_sender_address = Column(Boolean, default=False)
    supervisor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approval_level1_required_override = Column(Boolean, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=china_now, server_default=func.now())


class Address(Base):
    __tablename__ = "addresses"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    scope = Column(String(20), default="personal")
    role = Column(String(20), default="receiver")
    region_code = Column(String(30), default="xiamen_other", nullable=False, index=True)
    label = Column(String(100))
    name = Column(String(50), nullable=False)
    phone = Column(String(20), nullable=False)
    company_name = Column(String(100))
    province = Column(String(50))
    city = Column(String(50))
    district = Column(String(50))
    detail = Column(String(300), nullable=False)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=china_now, server_default=func.now())


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    order_no = Column(String(64), unique=True, nullable=False, index=True)
    courier_type = Column(String(10), nullable=False)
    region_code = Column(String(30), default="xiamen_other", nullable=False, index=True)
    billing_account_code = Column(String(100), nullable=True)
    express_type = Column(String(20))
    express_type_name = Column(String(50))
    courier_order_no = Column(String(64))
    sender_name = Column(String(50), nullable=False)
    sender_phone = Column(String(20), nullable=False)
    sender_company = Column(String(100))
    sender_province = Column(String(50))
    sender_city = Column(String(50))
    sender_district = Column(String(50))
    sender_address = Column(String(300), nullable=False)
    receiver_name = Column(String(50), nullable=False)
    receiver_phone = Column(String(20), nullable=False)
    receiver_company = Column(String(100))
    receiver_province = Column(String(50), nullable=False)
    receiver_city = Column(String(50), nullable=False)
    receiver_district = Column(String(50))
    receiver_address = Column(String(300), nullable=False)
    receiver_lng = Column(String(32), nullable=True, comment="顺丰同城收件地址经度")
    receiver_lat = Column(String(32), nullable=True, comment="顺丰同城收件地址纬度")
    item_name = Column(String(100), nullable=False)
    item_weight = Column(Float)
    item_value = Column(Float, default=1, comment="同城配送物品价值，单位元")
    item_volume = Column(Float, comment="预估总体积，单位 m³")
    parcel_count = Column(Integer, default=1, comment="包裹数量")
    item_count = Column(Integer, default=1)
    item_remark = Column(Text)
    pickup_time_mode = Column(String(20), default="immediate")
    pickup_start_time = Column(DateTime)
    pickup_end_time = Column(DateTime)
    shipping_reason = Column(Text, comment="寄件原因")
    freight = Column(Float)
    insurance = Column(Float, default=0)
    cost_type = Column(String(20))
    dept_code = Column(String(50))
    dept_name = Column(String(100))
    project_code = Column(String(50))
    project_name = Column(String(200))
    status = Column(String(20), default="pending", index=True)
    source = Column(String(20), default="system")
    source_id = Column(String(100))
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approve_time = Column(DateTime)
    approve_remark = Column(Text)
    approval_total_levels = Column(Integer, default=1)
    approval_current_level = Column(Integer, default=1)
    approval_level1_required = Column(Boolean, default=True)
    approval_level1_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approval_level1_time = Column(DateTime)
    approval_level1_remark = Column(Text)
    approval_level2_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approval_level2_time = Column(DateTime)
    approval_level2_remark = Column(Text)
    approval_stage_started_at = Column(DateTime)
    approval_last_reminded_at = Column(DateTime)
    order_time = Column(DateTime)
    order_response = Column(Text)
    sf_city_notification_events = Column(
        Text, comment="顺丰同城已发送的生命周期提醒事件 JSON"
    )
    courier_order_nos = Column(Text, comment="母单及子单运单号JSON")
    courier_picked_by = Column(String(50))
    courier_picked_by_phone = Column(String(20))
    estimated_delivery_time = Column(DateTime)
    route_code = Column(String(10))
    actual_freight = Column(Float)
    actual_fee_detail = Column(Text, comment="快递公司返回的运单费用明细 JSON")
    track_status = Column(String(100))
    track_update_time = Column(DateTime)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=china_now, server_default=func.now(), index=True)
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)
    __table_args__ = (Index("idx_status_created", "status", "created_at"),)



class Batch(Base):
    """批量寄件批次 — 共享物品/寄件人, N 个独立收件人"""
    __tablename__ = "batches"
    id = Column(Integer, primary_key=True)
    batch_no = Column(String(64), unique=True, nullable=False, index=True,
                      comment="批次号: BA + 时间戳")
    courier_type = Column(String(10), nullable=False)
    region_code = Column(String(30), default="xiamen_other", nullable=False, index=True)
    billing_account_code = Column(String(100), nullable=True)
    express_type = Column(String(20))
    express_type_name = Column(String(50))
    # 整批共享的寄件人
    sender_name = Column(String(50), nullable=False)
    sender_phone = Column(String(20), nullable=False)
    sender_company = Column(String(100))
    sender_province = Column(String(50))
    sender_city = Column(String(50))
    sender_district = Column(String(50))
    sender_address = Column(String(300))
    # 整批共享的物品
    item_name = Column(String(100), nullable=False)
    item_weight = Column(Float)
    item_volume = Column(Float, comment="每票预估总体积，单位 m³")
    insurance = Column(Float, default=0, comment="每票声明保价金额，单位元")
    parcel_count = Column(Integer, default=1, comment="每个收件订单的包裹数量")
    item_count_per_pkg = Column(Integer, default=1, comment="每件物品数量")
    item_remark = Column(Text)
    shipping_reason = Column(Text, comment="寄件原因")
    pickup_time_mode = Column(String(20), default="immediate")
    pickup_start_time = Column(DateTime)
    pickup_end_time = Column(DateTime)
    estimated_freight = Column(Float, comment="每票预估运费")
    # 费用归属
    cost_type = Column(String(20))
    dept_code = Column(String(50))
    dept_name = Column(String(100))
    project_code = Column(String(50))
    project_name = Column(String(200))
    # 统计
    total_count = Column(Integer, default=0, comment="收件人总数")
    committed_count = Column(Integer, default=0, comment="已落库的 Order 数")
    ordered_count = Column(Integer, default=0, comment="已调快递下单数")
    failed_count = Column(Integer, default=0)
    # 状态机
    status = Column(String(20), default="draft", index=True,
                    comment="draft → pending_approval → approved → ordering → done / partial / failed")
    # 审批
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approve_time = Column(DateTime)
    approve_remark = Column(Text)
    approval_total_levels = Column(Integer, default=1)
    approval_current_level = Column(Integer, default=1)
    approval_level1_required = Column(Boolean, default=True)
    approval_level1_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approval_level1_time = Column(DateTime)
    approval_level1_remark = Column(Text)
    approval_level2_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approval_level2_time = Column(DateTime)
    approval_level2_remark = Column(Text)
    approval_stage_started_at = Column(DateTime)
    approval_last_reminded_at = Column(DateTime)
    # 归属
    created_by = Column(Integer, ForeignKey("users.id"))
    remark = Column(Text)
    details = Column(Text, comment="import后的rows JSON")
    created_at = Column(DateTime, default=china_now, server_default=func.now(), index=True)
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)

class CourierTrack(Base):
    __tablename__ = "courier_tracks"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    courier_order_no = Column(String(64), index=True)
    track_time = Column(DateTime, nullable=False)
    track_status = Column(String(50))
    track_detail = Column(Text)
    created_at = Column(DateTime, default=china_now, server_default=func.now())


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True)
    dept_code = Column(String(50), unique=True, nullable=False, index=True)
    dept_name = Column(String(1000), nullable=False)
    # 钉钉关联字段
    dingtalk_dept_id = Column(String(30), unique=True, nullable=True, index=True,
                               comment="钉钉部门ID (dept_id)")
    dingtalk_parent_id = Column(String(30), nullable=True, comment="钉钉父部门ID")
    dingtalk_sort = Column(Integer, default=0, comment="钉钉排序")
    # 本地部门树
    parent_id = Column(Integer, ForeignKey("departments.id"), nullable=True, index=True)
    level = Column(Integer, default=1, comment="层级 1=根")
    # 费用归属配置
    cost_center = Column(String(50), nullable=True, comment="费用中心/成本中心编码")
    # 启用控制
    enabled = Column(Boolean, default=True)
    is_synced_from_dingtalk = Column(Boolean, default=False, comment="是否从钉钉同步")
    created_at = Column(DateTime, default=china_now, server_default=func.now())
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    project_code = Column(String(50), nullable=False, unique=True, index=True)
    project_name = Column(String(1000), nullable=False)
    full_path = Column(String(1000), nullable=True, unique=True, index=True)
    parent_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    level = Column(Integer, default=1)
    dept_code = Column(String(50), index=True)
    dept_name = Column(String(1000))
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=china_now, server_default=func.now())
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)
    __table_args__ = (Index("idx_project_dept", "dept_code", "enabled"),)


class ManualWaybillRecord(Base):
    """费用归属补录：记录未通过本系统下单的运单。"""
    __tablename__ = "manual_waybill_records"
    id = Column(Integer, primary_key=True)
    region_code = Column(String(30), nullable=False, index=True)
    courier_type = Column(String(10), nullable=False, index=True)
    waybill_no = Column(String(100), nullable=False, index=True)
    sender_name = Column(String(100), nullable=False)
    cost_type = Column(String(20), nullable=False)
    dept_code = Column(String(50), nullable=True, index=True)
    dept_name = Column(String(1000), nullable=True)
    project_code = Column(String(50), nullable=True, index=True)
    project_name = Column(String(1000), nullable=True)
    status = Column(String(20), default="pending_approval", nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    approval_level2_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    approval_level2_time = Column(DateTime, nullable=True)
    approval_level2_remark = Column(Text, nullable=True)
    approval_stage_started_at = Column(DateTime, nullable=True)
    approval_last_reminded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=china_now, server_default=func.now(), index=True)
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)
    __table_args__ = (
        Index("idx_manual_waybill_scope", "region_code", "courier_type", "waybill_no"),
    )


class BillingStatement(Base):
    """按月份、区域和快递保存的一份承运商费用账单。"""
    __tablename__ = "billing_statements"
    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False, index=True)
    region_code = Column(String(30), nullable=False, index=True)
    courier_type = Column(String(10), nullable=False, index=True)
    source_filename = Column(String(255), nullable=False)
    detail_sheet = Column(String(255), nullable=False)
    amount_header = Column(String(255), nullable=False)
    header_row = Column(Integer, nullable=False)
    waybill_column = Column(Integer, nullable=False)
    amount_column = Column(Integer, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = Column(DateTime, default=china_now, server_default=func.now())
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)
    __table_args__ = (
        UniqueConstraint("month", "region_code", "courier_type", name="uq_billing_statement_scope"),
    )


class BillingStatementLine(Base):
    """账单原始行及其当前分摊归属。"""
    __tablename__ = "billing_statement_lines"
    id = Column(Integer, primary_key=True)
    statement_id = Column(
        Integer, ForeignKey("billing_statements.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    row_index = Column(Integer, nullable=False)
    waybill_no = Column(String(100), nullable=False, index=True)
    submitter_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    submitter_name = Column(String(200), nullable=True)
    amount = Column(Float, nullable=False, default=0)
    raw_data = Column(Text, nullable=True)
    allocation_type = Column(String(20), nullable=True, index=True)
    dept_code = Column(String(50), nullable=True, index=True)
    dept_name = Column(String(1000), nullable=True)
    project_code = Column(String(50), nullable=True, index=True)
    project_name = Column(String(1000), nullable=True)
    match_source = Column(String(30), nullable=True, index=True)
    match_reference_id = Column(Integer, nullable=True)
    issue = Column(String(255), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)
    __table_args__ = (
        UniqueConstraint("statement_id", "row_index", name="uq_billing_statement_row"),
        Index("idx_billing_line_statement_waybill", "statement_id", "waybill_no"),
    )


class Feedback(Base):
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    status = Column(String(20), default="pending", nullable=False, index=True)
    processed_at = Column(DateTime)
    created_at = Column(DateTime, default=china_now, server_default=func.now(), index=True)


class SystemConfig(Base):
    __tablename__ = "system_configs"
    id = Column(Integer, primary_key=True)
    config_key = Column(String(50), unique=True, nullable=False)
    config_value = Column(Text)
    description = Column(String(200))
    updated_at = Column(DateTime, default=china_now, server_default=func.now(), onupdate=china_now)
