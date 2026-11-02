"""折扣规则: 按订单金额给折扣率。"""


def discount_rate(amount: float) -> float:
    """满额折扣: >=1000 打 85 折, >=500 打 9 折, >=100 打 95 折。"""
    if amount >= 1000:
        return 0.15
    if amount >= 500:
        return 0.10
    if amount >= 100:
        return 0.05
    return 0.0
