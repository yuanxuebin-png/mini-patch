"""token 账本: 把「多 Agent 到底省不省 token」从感觉变成数字。

设计动机
--------
docs/isolation.md 里说: 拆不拆子 Agent, 必须能说出一个数字。
本模块就是这个数字的来源 —— 对同一任务统计「谁花了多少 token」。

实现取巧的地方
--------------
LangChain 1.x 的每条 AIMessage 都自带 ``usage_metadata``
(input_tokens / output_tokens / total_tokens, 由厂商 API 返回, 精确值)。
所以**不需要**注册任何 callback 钩子 —— 只要把一次 agent.invoke()
返回的 messages 全部扫一遍求和, 就是这次调用的完整开销。
好处: 零侵入、不会漏(工具循环里每轮的 AIMessage 都在列表里)、可复算。

口径约定(面试讲点)
------------------
- 「这次任务花了多少」 = 会话里所有 AIMessage 的 usage 之和
- 「父上下文被塞了多少」 = 单独统计 input_tokens, 它才是隔离要优化的目标:
  input token 与「每轮请求带了多少历史」成正比, 上下文越大,
  后面每一轮都在为它付费
- output_tokens 反映模型生成量, 和隔离关系不大, 拆开记以免混口径
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage


def sum_usage(messages: list[BaseMessage]) -> dict[str, int]:
    """对一次会话的全部 AIMessage 求和 usage_metadata。

    非 AIMessage(HumanMessage/ToolMessage)没有 usage, 跳过;
    个别消息缺 usage_metadata(如本地 mock)按 0 计, 不报错 ——
    账本是统计工具, 宁可少记不能崩。
    """
    totals = {"calls": 0, "input": 0, "output": 0, "total": 0}
    for m in messages:
        usage = getattr(m, "usage_metadata", None)
        if not usage:
            continue
        totals["calls"] += 1
        totals["input"] += usage.get("input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
        totals["total"] += usage.get("total_tokens", 0)
    return totals


class TokenLedger:
    """按标签(阶段/Agent)记账, 最后打一张对比表。

    用法:
        ledger = TokenLedger()
        ledger.add("scout", result["messages"])   # 侦察阶段
        ledger.add("main",  main_result["messages"])  # 修复阶段
        ledger.report()
    """

    def __init__(self) -> None:
        self._books: dict[str, dict[str, int]] = {}

    def add(self, label: str, messages: list[BaseMessage]) -> dict[str, int]:
        """记一笔。同一 label 多次 add 会累加(适合分轮调用)。"""
        row = sum_usage(messages)
        if label in self._books:
            for k, v in row.items():
                self._books[label][k] += v
        else:
            self._books[label] = row
        return row

    @property
    def books(self) -> dict[str, dict[str, int]]:
        """只读暴露, 供评测脚本二次汇总。"""
        return dict(self._books)

    def report(self) -> str:
        """终端友好的对齐表格(中文对齐按 2 宽算)。"""

        def vis(s: str) -> int:  # 显示宽度: CJK 算 2
            return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

        def pad(s: str, width: int) -> str:
            return s + " " * max(0, width - vis(s))

        rows = [(r["label"], r["calls"], r["input"], r["output"], r["total"])
                for r in self._rows()]
        header = ("阶段", "调用", "输入tok", "输出tok", "合计")
        widths = [
            max(vis(header[i]), *(vis(str(r[i])) for r in rows)) if rows else vis(header[i])
            for i in range(5)
        ]
        lines = ["  ".join(pad(h, w) for h, w in zip(header, widths))]
        lines.append("  ".join("-" * w for w in widths))
        for r in rows:
            cells = tuple(str(c) for c in r)
            lines.append("  ".join(pad(c, w) for c, w in zip(cells, widths)))
        return "\n".join(lines)

    def _rows(self) -> list[dict]:
        return [
            {"label": k, **v}
            for k, v in self._books.items()
        ]

    def __str__(self) -> str:  # print(ledger) 直接出表
        return self.report()


# ----------------------------------------------------------------------
# 自测(零成本, 不调模型): 伪造带 usage_metadata 的消息验证求和与累加
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = [
        HumanMessage("hi"),  # 无 usage, 应被跳过
        AIMessage(
            "thinking...",
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        ),
        AIMessage(
            "",
            tool_calls=[{"name": "grep", "args": {}, "id": "1"}],
            usage_metadata={"input_tokens": 150, "output_tokens": 30, "total_tokens": 180},
        ),
        AIMessage(
            "done",
            usage_metadata={"input_tokens": 200, "output_tokens": 10, "total_tokens": 210},
        ),
    ]

    assert sum_usage(msgs) == {"calls": 3, "input": 450, "output": 60, "total": 510}
    print("[1] sum_usage 求和        OK")

    ledger = TokenLedger()
    ledger.add("侦察", msgs)
    ledger.add("侦察", msgs[:1])  # 全无 usage 的消息, 累加后不变
    ledger.add("修复", msgs)
    rows = ledger.books
    assert rows["侦察"]["input"] == 450, rows
    assert rows["修复"]["total"] == 510
    print("[2] 按标签累加            OK")

    print(ledger.report())
    print("\n全部通过")
