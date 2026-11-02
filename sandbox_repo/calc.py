def average(numbers):
    """计算平均值。空列表应返回 0.0。"""
    if not numbers:
        return 0.0
    total = sum(numbers)
    return total / len(numbers)
