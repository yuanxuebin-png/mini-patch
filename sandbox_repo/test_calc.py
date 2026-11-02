from calc import average


def check():
    assert average([2, 4, 6]) == 4.0, "基本用例失败"
    assert average([]) == 0.0, "空列表应返回 0.0"
    print("all tests passed")


if __name__ == "__main__":
    check()
