"""Contact-number normalization shared by order and address endpoints."""


def normalize_contact_number(value) -> str:
    """Keep the supplied phone/landline text, requiring only a non-empty value."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("请输入联系电话")
    return normalized
