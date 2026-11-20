"""管理后台寄件明细 Excel 导出。"""
from io import BytesIO
from unicodedata import east_asian_width

from openpyxl import Workbook
from openpyxl.utils import get_column_letter


ATTRIBUTION_HEADERS = {
    "department": ["运单号", "部门"],
    "project": ["运单号", "项目", "部门"],
}

CARRIER_NAMES = {"SF": "顺丰", "SF_CITY": "顺丰同城", "DB": "德邦", "JDL": "京东"}

DETAIL_HEADERS = [
    "下单时间", "快递产品", "当前状态", "下单员工",
    "寄件人", "寄件人电话", "寄件公司", "寄件地址",
    "收件人", "收件人电话", "收件公司", "收件地址",
    "物品类型", "实际费用(元)", "寄件原因",
]


def _full_address(order, prefix):
    return "".join(str(getattr(order, f"{prefix}_{part}", "") or "") for part in (
        "province", "city", "district", "address"
    ))


def _cost_type(order):
    value = str(order.cost_type or "department").strip().lower()
    return "project" if value in {"project", "项目"} else "department"


def _status_text(order, category, status_labels):
    return status_labels.get(category(order), "待处理")


def _row(order, attribution, creator_details, category, status_labels):
    creator = creator_details.get(order.created_by, {})
    carrier_type = str(order.courier_type or "").upper()
    carrier_name = CARRIER_NAMES.get(carrier_type, carrier_type or "未知快递")
    service = f"{carrier_name}快递"
    if order.express_type_name:
        service += f"·{order.express_type_name}"
    ordered_at = order.order_time or order.created_at
    details = [
        ordered_at, service, _status_text(order, category, status_labels), creator.get("name", ""),
        order.sender_name or "", str(order.sender_phone or ""), order.sender_company or "",
        _full_address(order, "sender"), order.receiver_name or "", str(order.receiver_phone or ""),
        order.receiver_company or "", _full_address(order, "receiver"), order.item_name or "",
        order.actual_freight, order.shipping_reason or "",
    ]
    waybill = str(order.courier_order_no or "")
    if attribution == "project":
        return [waybill, order.project_name or "", order.dept_name or "", *details]
    return [waybill, order.dept_name or "", *details]


def _text_width(value):
    """Return a practical Excel width, counting CJK characters as double width."""
    if value is None:
        return 0
    lines = str(value).splitlines() or [""]
    return max(sum(2 if east_asian_width(char) in {"W", "F", "A"} else 1 for char in line)
               for line in lines)


def _fit_columns(sheet):
    """Fit columns to their contents without letting long addresses dominate the sheet."""
    width_limits = {
        "运单号": (14, 24), "部门": (10, 28), "项目": (12, 38),
        "下单时间": (16, 20), "快递产品": (12, 22), "当前状态": (10, 16),
        "下单员工": (10, 16), "寄件人": (9, 16), "收件人": (9, 16),
        "寄件人电话": (13, 16), "收件人电话": (13, 16),
        "寄件公司": (12, 28), "收件公司": (12, 28),
        "寄件地址": (18, 40), "收件地址": (18, 40), "寄件原因": (12, 32),
    }
    for index, cell in enumerate(sheet[1], 1):
        header = str(cell.value or "")
        minimum, maximum = width_limits.get(header, (10, 18))
        content_width = max(_text_width(row[index - 1].value) for row in sheet.iter_rows())
        sheet.column_dimensions[get_column_letter(index)].width = min(
            maximum, max(minimum, content_width + 2)
        )


def _style_sheet(sheet, attribution):
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet.iter_rows(min_row=2):
        row[0].number_format = "@"
    date_column = len(ATTRIBUTION_HEADERS[attribution]) + 1
    for cells in sheet.iter_cols(min_col=date_column, max_col=date_column, min_row=2):
        for cell in cells:
            cell.number_format = "yyyy-mm-dd hh:mm"
    headers = [cell.value for cell in sheet[1]]
    for label in ("实际费用(元)",):
        index = headers.index(label) + 1
        for cells in sheet.iter_cols(min_col=index, max_col=index, min_row=2):
            for cell in cells:
                cell.number_format = '¥#,##0.00'
    _fit_columns(sheet)


def build_order_export(
    orders, creator_details, category, status_labels,
    region_name="全部区域", carrier_name="快递",
):
    workbook = Workbook()
    workbook.remove(workbook.active)
    prefix = f"{str(region_name or '全部区域').strip()}{str(carrier_name or '快递').strip()}"
    for attribution, suffix in (("department", "部门"), ("project", "项目")):
        title = f"{prefix}-{suffix}"[:31]
        sheet = workbook.create_sheet(title)
        sheet.append([*ATTRIBUTION_HEADERS[attribution], *DETAIL_HEADERS])
        for order in orders:
            if _cost_type(order) == attribution:
                sheet.append(_row(
                    order, attribution, creator_details, category, status_labels
                ))
        _style_sheet(sheet, attribution)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
