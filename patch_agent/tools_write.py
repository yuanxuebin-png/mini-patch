"""写入工具层: apply_edit / write_file —— 带快照与 diff 的受控写入。

设计要点
--------
1. 优先 apply_edit(找旧文本替换), 而不是 write_file(整文件重写):
   - 省 token: 模型只需输出改动片段, 不用重新输出整个文件
   - 更安全: old_text 必须唯一命中, 改错位置的概率大幅下降
2. 写入前自动拍快照 -> 任何写入都可撤销(见 snapshot.py)
3. 返回值是 unified diff: 模型看到自己改了什么, 人(和审计日志)也看得到
4. old_text 命中多次时拒绝执行: 让模型补上下文, 而不是替它猜
   —— 「工具不确定时停下来问」比「猜一个」安全得多
"""

from __future__ import annotations

import difflib
from pathlib import Path

from langchain_core.tools import BaseTool, ToolException, tool

from patch_agent.sandbox import Sandbox, SandboxViolation
from patch_agent.snapshot import SnapshotStore
from patch_agent.tools_read import _to_model_error

MAX_DIFF_CHARS = 2000  # diff 输出上限, 防大改动刷爆上下文


def _unified_diff(rel: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    text = "".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n...(diff 过长已截断)"
    return text or "(无变化)"


def make_write_tools(sandbox: Sandbox, snapshots: SnapshotStore) -> list[BaseTool]:
    """生产写入工具。快照存储由外部注入, 保证与回滚入口共享同一份状态。"""

    def guarded(raw: str) -> tuple[Path, str]:
        try:
            p = sandbox.resolve(raw)
        except SandboxViolation as e:
            raise ToolException(str(e)) from None
        return p, sandbox.rel(p)

    # ------------------------------------------------------------------
    @tool
    @_to_model_error
    def apply_edit(path: str, old_text: str, new_text: str) -> str:
        """把文件中【唯一出现】的一段旧文本替换为新文本, 返回 unified diff。

        这是修改代码的首选方式。old_text 必须在文件中恰好出现一次;
        出现多次会被拒绝, 请在 old_text 中多带上几行上下文使其唯一。
        新建文件请用 write_file。
        """
        f, rel = guarded(path)
        if not f.is_file():
            raise ToolException(f"文件不存在: {path} (新建请用 write_file)")
        before = f.read_text(encoding="utf-8")

        count = before.count(old_text)
        if count == 0:
            raise ToolException("old_text 未命中: 先用 read_snippet 确认当前内容")
        if count > 1:
            raise ToolException(
                f"old_text 命中 {count} 次, 拒绝执行以免改错位置; "
                "请扩展 old_text 的上下文使其唯一"
            )

        snapshots.take([rel])                      # 写前快照, 可回滚
        after = before.replace(old_text, new_text, 1)
        f.write_text(after, encoding="utf-8")
        return f"已修改 {rel}\n" + _unified_diff(rel, before, after)

    # ------------------------------------------------------------------
    @tool
    @_to_model_error
    def write_file(path: str, content: str) -> str:
        """新建文件, 或整体覆盖一个已有文件, 返回 unified diff。

        修改已有文件请优先用 apply_edit(只传改动片段, 更省更安全)。
        """
        f, rel = guarded(path)
        before = f.read_text(encoding="utf-8") if f.exists() else ""
        snapshots.take([rel])
        f.write_text(content, encoding="utf-8")
        verb = "已覆盖" if before else "已新建"
        return f"{verb} {rel}\n" + _unified_diff(rel, before, content)

    return [apply_edit, write_file]


# ----------------------------------------------------------------------
# 自测: python -m patch_agent.tools_write
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(tmp)
        store = SnapshotStore(box)
        (box.root / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        tools = {t.name: t for t in make_write_tools(box, store)}

        # [1] apply_edit 正常路径: 返回 diff 且内容已变
        out = tools["apply_edit"].invoke(
            {"path": "calc.py", "old_text": "return a + b", "new_text": "return a + b + 0"}
        )
        assert "-    return a + b" in out and "+    return a + b + 0" in out, out
        assert "return a + b + 0" in (box.root / "calc.py").read_text(encoding="utf-8")
        print("[1] apply_edit      OK ->", out.splitlines()[0])

        # [2] 多次命中拒绝(构造两处相同片段)
        (box.root / "calc.py").write_text(
            "def f(x):\n    return x\n\ndef g(x):\n    return x\n", encoding="utf-8"
        )
        out = tools["apply_edit"].invoke(
            {"path": "calc.py", "old_text": "return x", "new_text": "X"}
        )
        assert "命中 2 次" in out, out
        print("[2] 多命中拒绝      OK")

        # [3] 未命中提示
        out = tools["apply_edit"].invoke(
            {"path": "calc.py", "old_text": "不存在的文本xyz", "new_text": "X"}
        )
        assert "未命中" in out, out
        print("[3] 未命中提示      OK")

        # [4] write_file 新建 + 覆盖均有 diff
        out = tools["write_file"].invoke({"path": "new.py", "content": "A = 1\n"})
        assert "已新建 new.py" in out, out
        out = tools["write_file"].invoke({"path": "new.py", "content": "A = 2\n"})
        assert "已覆盖 new.py" in out and "-A = 1" in out and "+A = 2" in out, out
        print("[4] write_file      OK")

        # [5] 写入后可整体回滚(快照层联动)
        store.undo_last()  # 回滚最近一次(new.py 覆盖)
        assert (box.root / "new.py").read_text(encoding="utf-8") == "A = 1\n"
        print("[5] 快照联动回滚    OK")

        # [6] 越界写入被拦
        out = tools["write_file"].invoke({"path": "../evil.txt", "content": "x"})
        assert "路径越界" in out, out
        print("[6] 越界写入拦截    OK")

    print("tools_write 全部自测通过")
