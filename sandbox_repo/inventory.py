"""库存查询(与本次故障无关, 增加检索干扰)。"""

_STOCK = {"SKU-1": 12, "SKU-2": 0, "SKU-3": 7}


def has_stock(sku: str) -> bool:
    return _STOCK.get(sku, 0) > 0


def decrease(sku: str, n: int = 1) -> bool:
    if not has_stock(sku):
        return False
    _STOCK[sku] -= n
    return True
