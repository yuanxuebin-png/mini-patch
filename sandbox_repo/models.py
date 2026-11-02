"""订单模型(演示用, 保持简单)。"""

from dataclasses import dataclass, field


@dataclass
class Order:
    order_id: str
    items: dict[str, float] = field(default_factory=dict)   # 商品 -> 单价

    @property
    def total(self) -> float:
        return round(sum(self.items.values()), 2)
