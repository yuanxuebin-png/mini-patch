"""消息通知(与本次故障无关, 增加检索干扰)。"""


def build_text(order_id: str, amount: float) -> str:
    return f"订单 {order_id} 应付 {amount:.2f} 元"


def send(channel: str, text: str) -> None:
    # 演示环境只打印, 不真的发送
    print(f"[{channel}] {text}")
