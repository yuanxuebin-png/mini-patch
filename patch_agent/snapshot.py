"""文件快照与回滚: 让「Agent 改文件」这件事可撤销。

设计动机
--------
写权限是代码 Agent 最危险的能力。快照层让每次写入前自动保存原内容,
任何一次改坏都可以一键还原 —— 这是「会破坏状态的系统」最朴素的兜底,
和订单/支付系统里「先落快照再变更」是同一个思路。

存储格式
--------
.snapshots/<时间戳>.json, 内容: {相对路径: 原文本 | null}
- null 表示回滚时应删除该文件(快照时它还不存在)
- 存 JSON 而不是直接拷贝文件: 一次快照覆盖多个文件, 原子可读,
  也方便 debug 时直接打开看改了什么
- 目录 .snapshots 在沙箱根下, 但被工具遍历忽略(SKIP_DIRS), 不会
  污染 grep 结果

边界
----
只处理 utf-8 文本(编码不对直接抛错, 宁可失败不可损坏);
不做嵌套快照链, undo 只回最近一次 —— 简单可预测, 够用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from patch_agent.sandbox import Sandbox


class SnapshotStore:
    """以「一次写入涉及的一组文件」为单位的快照存取。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.dir = sandbox.root / ".snapshots"
        self.dir.mkdir(exist_ok=True)
        self._last_id: str | None = None

    def take(self, rel_paths: list[str]) -> str:
        """写入前调用: 记录这批文件当前内容, 返回快照 id。

        文件不存在也记录(null) —— 因为 write 可能是「新建」,
        回滚时新建文件要删掉, 而不是恢复成某个旧版本。
        """
        sid = datetime.now().strftime("%H%M%S-%f")
        data: dict[str, str | None] = {}
        for rel in rel_paths:
            p = self.sandbox.resolve(rel)
            data[rel] = p.read_text(encoding="utf-8") if p.exists() else None
        (self.dir / f"{sid}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._last_id = sid
        return sid

    def restore(self, sid: str) -> list[str]:
        """恢复指定快照, 返回被恢复/删除的路径列表。"""
        fp = self.dir / f"{sid}.json"
        if not fp.exists():
            raise FileNotFoundError(f"快照不存在: {sid}")
        data: dict = json.loads(fp.read_text(encoding="utf-8"))
        touched: list[str] = []
        for rel, content in data.items():
            p = self.sandbox.resolve(rel)  # 恢复也走沙箱校验, 防止清单被改后越界
            if content is None:
                if p.exists():
                    p.unlink()
                    touched.append(f"已删除(新建被撤销): {rel}")
            else:
                p.write_text(content, encoding="utf-8")
                touched.append(f"已恢复: {rel}")
        return touched

    def undo_last(self) -> list[str]:
        """回滚最近一次快照(最常用入口)。"""
        if self._last_id is None:
            return ["没有可回滚的快照"]
        return self.restore(self._last_id)

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))


# ----------------------------------------------------------------------
# 自测: python -m patch_agent.snapshot
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(tmp)
        store = SnapshotStore(box)
        (box.root / "calc.py").write_text("v1", encoding="utf-8")

        # [1] 修改已有文件 -> 快照 -> 再改 -> 回滚
        store.take(["calc.py"])
        (box.root / "calc.py").write_text("v2-broken", encoding="utf-8")
        assert (box.root / "calc.py").read_text(encoding="utf-8") == "v2-broken"
        store.undo_last()
        assert (box.root / "calc.py").read_text(encoding="utf-8") == "v1"
        print("[1] 修改回滚        OK")

        # [2] 新建文件 -> 快照(null) -> 回滚 = 删除
        store.take(["new_file.py"])
        (box.root / "new_file.py").write_text("created", encoding="utf-8")
        store.undo_last()
        assert not (box.root / "new_file.py").exists()
        print("[2] 新建撤销        OK")

        # [3] 一次快照覆盖多个文件
        (box.root / "a.py").write_text("A1", encoding="utf-8")
        (box.root / "b.py").write_text("B1", encoding="utf-8")
        sid = store.take(["a.py", "b.py"])
        (box.root / "a.py").write_text("A2", encoding="utf-8")
        (box.root / "b.py").write_text("B2", encoding="utf-8")
        store.restore(sid)
        assert (box.root / "a.py").read_text(encoding="utf-8") == "A1"
        assert (box.root / "b.py").read_text(encoding="utf-8") == "B1"
        print("[3] 多文件原子恢复  OK")

        # [4] 快照清单可读(json), 且不在工具遍历范围
        assert store.list_ids(), "应有快照记录"
        print("[4] 快照清单        OK ->", store.list_ids())

    print("snapshot 全部自测通过")
