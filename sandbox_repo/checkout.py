"""结算: 计算应付金额。"""
from discounts import discount_rate


def pay_amount(total: float) -> float:
    """按折扣规则计算应付金额: 应付 = 原价 * (1 - 折扣率)。"""
    rate = discount_rate(total)
    return round(total * (1 - rate), 2)
