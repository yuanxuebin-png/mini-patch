"""工作区沙箱: 所有文件操作必须发生在授权根目录内。

设计动机
--------
Agent 拿到的路径来自模型输出, 属于**不可信输入**。没有这层防护时,
模型一次幻觉就可能让 read_file 读到 .env(密钥泄露), 或让 write_file
覆盖沙箱外的文件。因此路径解析集中在本模块, 其他模块一律通过
Sandbox.resolve() 取真实路径, 不允许自行拼接。

三层防护
--------
1. 相对路径按 root 解析; 绝对路径解析后必须仍在 root 内
2. ``..`` 逃逸: resolve() 归一化后检查 is_relative_to
3. 符号链接逃逸: resolve() 会解开真实路径后再比对
   (Linux/Mac 上 ln -s /etc 内建链接即可被此层拦下)

边界说明(面试讲点)
------------------
这是**路径级**防护, 不是操作系统级沙箱。彻底隔离需要容器或独立进程,
个人项目里路径校验 + 命令白名单覆盖了绝大多数风险, 剩余风险靠
人工审批兜底 —— 这个取舍要能讲清楚。
"""

from __future__ import annotations

from pathlib import Path


class SandboxViolation(PermissionError):
    """路径越出沙箱根目录时抛出。工具层捕获后转成给模型的错误提示。"""


class Sandbox:
    """把不可信的路径字符串解析成沙箱内的安全绝对路径。"""

    def __init__(self, root: str | Path) -> None:
        # resolve(): 转绝对路径 + 解开符号链接 + 归一化, root 自身必须真实存在
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"沙箱根目录不存在: {self.root}")

    def resolve(self, raw: str) -> Path:
        """解析任意输入路径; 越界抛 SandboxViolation。

        参数 raw 通常来自模型, 可能是:
          "calc.py"                相对路径 -> root/calc.py
          "src/util/helper.py"     带子目录
          "../outside.txt"         逃逸尝试 -> 拒绝
          "C:/Windows/system32/x"  绝对路径越界 -> 拒绝
        """
        p = Path(raw)
        if p.is_absolute():
            # 绝对路径: 不与 root 拼接, 直接解析后校验
            # (Path 拼接绝对路径会整体替换, 必须单独处理这一分支)
            resolved = p.resolve()
        else:
            resolved = (self.root / p).resolve()

        if not resolved.is_relative_to(self.root):
            raise SandboxViolation(
                f"路径越界: {raw!r} 解析为 {resolved}, 超出沙箱根 {self.root}"
            )
        return resolved

    def rel(self, resolved: Path) -> str:
        """绝对路径转回相对 root 的显示路径, 用于日志与 diff 头。"""
        return str(resolved.relative_to(self.root))


# ----------------------------------------------------------------------
# 自测: python -m patch_agent.sandbox
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(tmp)
        (box.root / "calc.py").write_text("print(1)", encoding="utf-8")
        (box.root / "sub").mkdir()
        (box.root / "sub" / "a.py").write_text("x", encoding="utf-8")

        # [1] 正常相对路径
        assert box.resolve("calc.py") == box.root / "calc.py"
        assert box.resolve("sub/a.py").name == "a.py"
        print("[1] 相对路径解析  OK")

        # [2] .. 逃逸必须被拦
        try:
            box.resolve("../outside.txt")
            raise AssertionError("二级逃逸未被拦截")
        except SandboxViolation as e:
            print("[2] .. 逃逸拦截   OK ->", str(e)[:60], "...")

        # [3] 绝对路径越界必须被拦
        try:
            box.resolve(str(Path(tmp).parent / "elsewhere.txt"))
            raise AssertionError("绝对路径越界未被拦截")
        except SandboxViolation:
            print("[3] 绝对路径拦截  OK")

        # [4] 根内绝对路径允许(校验通过即可)
        inside = str(box.root / "calc.py")
        assert box.resolve(inside) == box.root / "calc.py"
        print("[4] 根内绝对路径  OK")

        # [5] rel() 往返一致
        assert box.rel(box.resolve("sub/a.py")) == str(Path("sub") / "a.py")
        print("[5] 相对路径还原  OK")

    print("sandbox 全部自测通过")
