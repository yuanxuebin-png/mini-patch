from checkout import pay_amount


def check():
    assert pay_amount(1200) == 1020.0, f"1200 应付 1020, 得到 {pay_amount(1200)}"
    assert pay_amount(600) == 540.0, f"600 应付 540, 得到 {pay_amount(600)}"
    assert pay_amount(80) == 80.0, "无折扣金额应原价"
    print("all tests passed")


if __name__ == "__main__":
    check()
