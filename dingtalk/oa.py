"""
钉钉 OA 审批能力模块
- 审批实例详情查询
- 审批表单字段 → 快递订单字段映射
- 从 OA 审批自动创建订单 + 调快递公司下单
"""

import logging
import datetime as dt
from typing import Optional

from config.settings import settings
from dingtalk.client import DingTalkClient
from database.connection import SessionLocal
from database import models

log = logging.getLogger("dingtalk.oa")


# ============================================================
# 审批模板字段映射表
# 左边 = OA 审批表单上的控件 label（**与钉钉 OA 管理后台里填的 label 必须完全一致**）
# 右边 = 我们 Order 模型里的字段名
# 如果 OA 表单改了控件名，这里同步改即可
# ============================================================
OA_FIELD_MAP = {
    # 寄件人
    "寄件人姓名": "sender_name",
    "寄件人电话": "sender_phone",
    "寄件人地址": "sender_address",          # 可拆分: 寄件省/寄件市/寄件区
    "寄件省": "sender_province",
    "寄件市": "sender_city",
    "寄件区": "sender_district",
    # 收件人
    "收件人姓名": "receiver_name",
    "收件人电话": "receiver_phone",
    "收件人地址": "receiver_address",
    "收件省": "receiver_province",
    "收件市": "receiver_city",
    "收件区": "receiver_district",
    # 物品
    "物品名称": "item_name",
    "数量": "item_count",
    "单个物品重量": "item_weight",
    "寄件原因": "shipping_reason",
    "备注": "item_remark",
    # 快递服务
    "快递公司": "courier_type",              # SF / DB
    "快递服务产品": "express_type_name",     # 顺丰标快 / 德邦大件3.60
    # 费用归属
    "费用类型": "cost_type",                 # department / project
    "部门编号": "dept_code",
    "部门": "dept_name",
    "项目编号": "project_code",
    "项目名称": "project_name",
}


def get_approval_instance(process_instance_id: str) -> dict:
    """
    调钉钉 API 查审批实例详情
    官方文档: /topapi/processinstance/get
    """
    client = DingTalkClient()
    resp = client.get(
        "/topapi/processinstance/get",
        params={"process_instance_id": process_instance_id},
    )
    if resp.get("errcode") != 0:
        raise RuntimeError(f"钉钉查询审批失败 errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}")
    return resp.get("process_instance", {})


def parse_form_fields(instance: dict) -> dict:
    """
    把钉钉审批返回的 formComponentValues 数组解析成 dict
    钉钉格式: [{"name": "寄件人姓名", "value": "张三"}, {"name": "数量", "value": "3"}, ...]
    """
    form = {}
    for fc in instance.get("form_component_values", []):
        name = fc.get("name", "")
        value = fc.get("value", "")
        # 某些控件 value 是 JSON 字符串（如省市区三级联动）
        try:
            if isinstance(value, str) and (value.startswith("[") or value.startswith("{")):
                import json as _json
                parsed = _json.loads(value)
                # 如果是省市区三级联动的数组 ["福建省","厦门市","思明区"]
                if isinstance(parsed, list) and len(parsed) == 3:
                    if name == "寄件地址" and "寄件省" not in form:
                        form["寄件省"], form["寄件市"], form["寄件区"] = parsed
                    elif name == "收件地址" and "收件省" not in form:
                        form["收件省"], form["收件市"], form["收件区"] = parsed
                    else:
                        form[name] = parsed[-1] if parsed else ""
                else:
                    form[name] = parsed
            else:
                form[name] = value
        except Exception:
            form[name] = value
    return form


def map_form_to_order(form: dict) -> dict:
    """
    把 OA 表单字段（已解析成 dict）映射成我们订单数据结构
    返回的 dict 可以直接传给 _create_order_from_oa()
    """
    order = {}
    for oa_label, order_field in OA_FIELD_MAP.items():
        raw = form.get(oa_label, "")
        if raw == "" or raw is None:
            continue
        # 类型转换
        if order_field in ("item_count",):
            try:
                order[order_field] = int(float(str(raw)))
            except (ValueError, TypeError):
                order[order_field] = 1
        elif order_field in ("item_weight",):
            try:
                order[order_field] = float(str(raw))
            except (ValueError, TypeError):
                order[order_field] = 0.0
        else:
            order[order_field] = str(raw).strip()

    # 快递公司大写
    if order.get("courier_type"):
        order["courier_type"] = str(order["courier_type"]).upper()
        # 容错: OA 里写 "顺丰" / "SF" / "SF Express" 都转成 "SF"
        if order["courier_type"] in ("顺丰", "SF", "SF EXPRESS", "丰桥"):
            order["courier_type"] = "SF"
        elif order["courier_type"] in ("德邦", "DB", "DEPPON", "DPN"):
            order["courier_type"] = "DB"

    # 默认值
    order.setdefault("courier_type", "SF")
    order.setdefault("item_count", 1)
    order.setdefault("item_weight", 0.0)
    order.setdefault("cost_type", "department")

    return order


# ============================================================
# 核心: 从 OA 审批创建订单 + 下单
# ============================================================

def create_order_from_oa(
    process_instance_id: str,
    event_id: str = None,
    dry_run: bool = False,
) -> Optional[models.Order]:
    """
    完整流程: 查审批详情 → 解析表单 → 创建 Order 记录 → 调快递公司下单

    Args:
        process_instance_id: 钉钉审批实例 ID
        event_id: 钉钉事件 ID（用于幂等去重）
        dry_run: True 时只创建 Order 不下单（测试用）

    Returns:
        Order 对象；失败返回 None
    """
    db = SessionLocal()
    try:
        # --- 幂等检查: 同一个 processInstanceId 不重复处理 ---
        existing = db.query(models.Order).filter(
            models.Order.source == "dingtalk_oa",
            models.Order.source_id == process_instance_id,
        ).first()
        if existing:
            log.warning(f"[幂等] OA审批 {process_instance_id} 已处理过 → order_no={existing.order_no}")
            return existing

        # --- 1. 拉审批详情 ---
        instance = get_approval_instance(process_instance_id)
        log.info(f"[OA] 审批详情 processCode={instance.get('process_code')} "
                 f"title={instance.get('title')} result={instance.get('result')}")

        # 二次确认: 审批确实是通过状态
        if instance.get("result") != "agree":
            log.warning(f"[OA] 审批 result={instance.get('result')}, 跳过下单")
            return None

        # --- 2. 解析表单 ---
        form = parse_form_fields(instance)
        # 补充审批发起人信息
        instance_id_info = instance.get("originator_userid") or instance.get("originatorId")
        order_data = map_form_to_order(form)

        # --- 3. 创建 Order 记录 ---
        now = dt.datetime.utcnow()
        order_no = "CO" + now.strftime("%Y%m%d%H%M%S") + str(abs(hash(process_instance_id)))[:6].upper()

        # 收件地址兼容拼接（如果 OA 里有省市区和详细地址分开存）
        receiver_addr = order_data.get("receiver_address", "")
        if not receiver_addr:
            parts = [
                order_data.get("receiver_province", ""),
                order_data.get("receiver_city", ""),
                order_data.get("receiver_district", ""),
                order_data.get("receiver_address", ""),
            ]
            receiver_addr = "".join(str(p) for p in parts if p)

        sender_addr = order_data.get("sender_address", "")
        if not sender_addr:
            parts = [
                order_data.get("sender_province", ""),
                order_data.get("sender_city", ""),
                order_data.get("sender_district", ""),
                order_data.get("sender_address", ""),
            ]
            sender_addr = "".join(str(p) for p in parts if p)

        order = models.Order(
            order_no=order_no,
            courier_type=order_data["courier_type"],
            express_type_name=order_data.get("express_type_name", ""),
            sender_name=order_data.get("sender_name", ""),
            sender_phone=order_data.get("sender_phone", ""),
            sender_province=order_data.get("sender_province", ""),
            sender_city=order_data.get("sender_city", ""),
            sender_district=order_data.get("sender_district", ""),
            sender_address=sender_addr,
            receiver_name=order_data.get("receiver_name", ""),
            receiver_phone=order_data.get("receiver_phone", ""),
            receiver_province=order_data.get("receiver_province", ""),
            receiver_city=order_data.get("receiver_city", ""),
            receiver_district=order_data.get("receiver_district", ""),
            receiver_address=receiver_addr,
            item_name=order_data.get("item_name", ""),
            item_count=order_data.get("item_count", 1),
            item_weight=order_data.get("item_weight", 0.0),
            shipping_reason=order_data.get("shipping_reason"),
            item_remark=order_data.get("item_remark"),
            cost_type=order_data.get("cost_type"),
            dept_code=order_data.get("dept_code"),
            dept_name=order_data.get("dept_name"),
            project_code=order_data.get("project_code"),
            project_name=order_data.get("project_name"),
            status="approved",                  # OA 已审批过，直接 approved
            source="dingtalk_oa",
            source_id=process_instance_id,
            approver_id=None,                   # OA 外部审批，系统内没有对应 approver
            approve_time=now,
            approve_remark=f"OA审批通过 event_id={event_id or '-'}",
            created_at=now,
        )
        db.add(order)
        db.flush()   # 拿 order.id

        # --- 4. 调快递公司下单 ---
        if not dry_run:
            _place_courier_order(order, db)
        else:
            log.info(f"[DRY-RUN] 跳过快递下单 order_no={order_no}")

        db.commit()
        db.refresh(order)
        log.info(f"[OA→快递] ✅ 完成 order_no={order_no} status={order.status} "
                 f"tracking={order.courier_order_no or '-'}")
        return order

    except Exception as e:
        log.error(f"[OA→快递] ❌ 处理失败 processInstanceId={process_instance_id}: {e}", exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


def _place_courier_order(order: models.Order, db) -> None:
    """
    调用快递公司下单（复用 courier 包里已有的逻辑）
    成功会回填 courier_order_no / estimated_delivery_time / freight / status
    失败会回填 status="failed" / approve_remark 里带错误信息
    """
    try:
        from courier import get_courier_for_order
        from api.order_dispatch import build_order_data

        courier = get_courier_for_order(order, db)
        payload = build_order_data(order, db)
        resp = courier.create_order(payload)

        import json as _json
        order.order_time = dt.datetime.utcnow()
        order.order_response = _json.dumps(resp, ensure_ascii=False, default=str)

        if resp.get("success"):
            order.status = "ordered"
            order.courier_order_no = resp.get("tracking_no") or resp.get("mail_no") or resp.get("courier_order_no")
            order.freight = resp.get("freight")
            est = resp.get("estimated_delivery_time")
            if est:
                try:
                    if isinstance(est, str):
                        # 尝试解析常见格式
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                            try:
                                order.estimated_delivery_time = dt.datetime.strptime(est[:19], fmt)
                                break
                            except ValueError:
                                continue
                    elif isinstance(est, (int, float)):
                        order.estimated_delivery_time = dt.datetime.fromtimestamp(int(est) / 1000
                                                                                    if est > 1e12 else est)
                except Exception:
                    pass
        else:
            order.status = "failed"
            order.approve_remark = f"下单失败: {resp.get('msg') or resp.get('message') or resp.get('code')}"

    except Exception as courier_err:
        order.status = "failed"
        order.approve_remark = f"快递公司异常: {courier_err}"
        log.error(f"快递公司下单异常: {courier_err}", exc_info=True)
