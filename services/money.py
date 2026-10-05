"""Currency conversion and cost fields shared by business services and views."""
from decimal import Decimal, InvalidOperation

COST_FIELDS = ("buy_cents", "buy_shipping_cents", "sell_shipping_cents",
               "platform_fee_cents", "processing_cents", "other_cents", "reserve_cents")

def money(cents):
    return "待核实" if cents is None else f"¥{Decimal(cents) / 100:,.2f}"

def cents(value, required=False):
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError("必填金额不能为空")
        return None
    try:
        n = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("金额格式错误")
    if not n.is_finite() or n < 0 or n.as_tuple().exponent < -2:
        raise ValueError("金额须为非负数，最多两位小数")
    return int(n * 100)
