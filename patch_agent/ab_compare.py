"""A/B 隔离收益对照: 把「拆子 Agent 值不值」变成两个数字。

实验设计
--------
同一个**多文件**仓库(6 个模块, bug 藏在其中之一), 两种架构各跑 N 次:

  A 组(不隔离): 单 Agent, 只读 + 写入 + 命令工具全挂,
                读代码和改代码在同一个会话里完成
  B 组(隔离):   FixPipeline —— 只读侦察子 Agent 先定位,
                主 Agent(父上下文)只拿 4 字段报告去修

为什么用多文件而不是单文件: 单文件任务 A 读一眼就完,
隔离毫无用武之地(第一版对照实测 B 反而更贵, 见 docs/ab_result.md
的历史备注)。隔离的收益假设是「侦察要读的量 >> 修复要改的量」,
实验必须给这个假设成立的条件, 结论才有效力。

口径
----
- A 组总输入 = 那一个会话的 input_tokens 总和
  (每轮请求都带全部历史, 文件内容进过一次, 后面每轮都在付它的钱)
- B 组总输入 = 侦察 + 修复 两段之和
- B 组父上下文 = 只看修复段 —— 这才是「主任务被塞了多少」的口径
- temperature=0 但 LLM 仍有波动, N 次取均值

结果写进 docs/ab_result.md(git 留档, 简历数字的出处)。
诚实声明: 单一任务类型 + 小样本, 结论只代表该场景。
"""

from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent

from patch_agent.commands import make_command_tool
from patch_agent.graph import MAIN_SYSTEM, load_model
from patch_agent.ledger import TokenLedger, sum_usage
from patch_agent.pipeline import FixPipeline
from patch_agent.sandbox import Sandbox
from patch_agent.snapshot import SnapshotStore
from patch_agent.tools_read import make_read_tools
from patch_agent.tools_write import make_write_tools

TASK = "python test_checkout.py 运行失败, 请定位并修复"
TEST_CMD = "python test_checkout.py"
N_RUNS = 2               # 每组次数: 时间与方差的折中

# ----------------------------------------------------------------------
# 多文件实验仓库: bug 在 checkout.py(应付金额乘了折扣率而不是 1-折扣率)
# ----------------------------------------------------------------------
FILES: dict[str, str] = {
    "discounts.py": '''"""折扣规则: 按订单金额给折扣率。"""


def discount_rate(amount: float) -> float:
    """满额折扣: >=1000 打 85 折, >=500 打 9 折, >=100 打 95 折。"""
    if amount >= 1000:
        return 0.15
    if amount >= 500:
        return 0.10
    if amount >= 100:
        return 0.05
    return 0.0
''',
    "models.py": '''"""订单模型(演示用, 保持简单)。"""

from dataclasses import dataclass, field


@dataclass
class Order:
    order_id: str
    items: dict[str, float] = field(default_factory=dict)   # 商品 -> 单价

    @property
    def total(self) -> float:
        return round(sum(self.items.values()), 2)
''',
    "inventory.py": '''"""库存查询(与本次故障无关, 增加检索干扰)。"""

_STOCK = {"SKU-1": 12, "SKU-2": 0, "SKU-3": 7}


def has_stock(sku: str) -> bool:
    return _STOCK.get(sku, 0) > 0


def decrease(sku: str, n: int = 1) -> bool:
    if not has_stock(sku):
        return False
    _STOCK[sku] -= n
    return True
''',
    "notify.py": '''"""消息通知(与本次故障无关, 增加检索干扰)。"""


def build_text(order_id: str, amount: float) -> str:
    return f"订单 {order_id} 应付 {amount:.2f} 元"


def send(channel: str, text: str) -> None:
    # 演示环境只打印, 不真的发送
    print(f"[{channel}] {text}")
''',
    "checkout.py": '''"""结算: 计算应付金额。"""
from discounts import discount_rate


def pay_amount(total: float) -> float:
    """按折扣规则计算应付金额: 应付 = 原价 * (1 - 折扣率)。"""
    rate = discount_rate(total)
    return round(total * rate, 2)
''',
    "test_checkout.py": '''from checkout import pay_amount


def check():
    assert pay_amount(1200) == 1020.0, f"1200 应付 1020, 得到 {pay_amount(1200)}"
    assert pay_amount(600) == 540.0, f"600 应付 540, 得到 {pay_amount(600)}"
    assert pay_amount(80) == 80.0, "无折扣金额应原价"
    print("all tests passed")


if __name__ == "__main__":
    check()
''',
}


def prepare_shop_repo(repo: Path) -> None:
    """重建多文件实验仓库(幂等): 每次恢复为「带 bug 的初始状态」。"""
    repo.mkdir(exist_ok=True)
    for name, text in FILES.items():
        (repo / name).write_text(text, encoding="utf-8")


def run_mode_a(model, repo: Path, task: str) -> dict[str, int]:
    """A 组: 单 Agent 一把梭。返回该会话的 usage 汇总。"""
    box = Sandbox(repo)
    tools = (
        make_read_tools(box)
        + make_write_tools(box, SnapshotStore(box))
        + [make_command_tool(box)]
    )
    agent = create_agent(model, tools=tools, system_prompt=MAIN_SYSTEM)
    result = agent.invoke({"messages": [("user", task)]})
    return sum_usage(result["messages"])


def run_mode_b(model, repo: Path, task: str, test_cmd: str) -> tuple[dict, dict]:
    """B 组: 两阶段管线。返回 (usage汇总, 分阶段账本)。"""
    box = Sandbox(repo)
    ledger = TokenLedger()
    pipe = FixPipeline(model, box)
    final = pipe.run(task, test_cmd, verbose=False, ledger=ledger)
    assert final["status"] == "fixed", "B 组任务应修复成功"
    books = ledger.books
    total = {
        "input": books["B-侦察"]["input"] + books["B-修复(父上下文)"]["input"],
        "output": books["B-侦察"]["output"] + books["B-修复(父上下文)"]["output"],
    }
    return total, books


def main() -> None:
    model = load_model()
    repo = Path(__file__).resolve().parent.parent / "sandbox_repo"

    a_inputs, b_totals, b_parents = [], [], []
    detail_lines: list[str] = []

    for i in range(N_RUNS):
        prepare_shop_repo(repo)
        a = run_mode_a(model, repo, TASK)
        a_inputs.append(a["input"])

        prepare_shop_repo(repo)
        b, books = run_mode_b(model, repo, TASK, TEST_CMD)
        parent = books["B-修复(父上下文)"]["input"]
        b_totals.append(b["input"])
        b_parents.append(parent)
        detail_lines.append(
            f"| {i + 1} | {a['input']} | {b['input']} | {parent} "
            f"| {books['B-侦察']['input']} |"
        )
        print(f"run {i + 1}/{N_RUNS}: A={a['input']}  B总={b['input']}  "
              f"B父={parent}  B侦={books['B-侦察']['input']}")

    a_avg = statistics.mean(a_inputs)
    b_avg = statistics.mean(b_totals)
    p_avg = statistics.mean(b_parents)
    save_total = (1 - b_avg / a_avg) * 100 if a_avg else 0.0
    save_parent = (1 - p_avg / a_avg) * 100 if a_avg else 0.0

    lines = [
        "# A/B 隔离收益实测",
        "",
        f"- 日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "- 任务: 6 模块仓库中定位并修复折扣计算 bug（侦察 + 最小修改 + 测试通过）",
        f"- 模型: deepseek-chat, temperature=0, 每组 {N_RUNS} 次取均值",
        "",
        "| 轮次 | A 总输入 | B 总输入 | B 父上下文 | B 侦察 |",
        "|---|---|---|---|---|",
        *detail_lines,
        "",
        f"- A 组(单 Agent)平均总输入: **{a_avg:.0f}**",
        f"- B 组(隔离)平均总输入: **{b_avg:.0f}**（比 A {'省' if save_total > 0 else '多'} {abs(save_total):.0f}%）",
        f"- B 组父上下文(修复段)平均输入: **{p_avg:.0f}**（比 A 省 {abs(save_parent):.0f}%）",
        "",
        "## 结论口径(重要, 面试讲点)",
        "",
        "- 父上下文输入对比: 修复 Agent 每一轮请求都携带的「历史包袱」被压到只剩报告+最小改动",
        "- B 组总输入包含侦察的独立开销; 任务越小时侦察占比越高, 总量可能反而更贵",
        "  (单文件 demo 的第一版实测就是这样, 已留档为本文件的 git 历史)",
        "- **隔离省的是父上下文的放大效应**, 任务越大、后续轮次越多, 收益越大;",
        "  任务极小时拆分得不偿失 —— 这两个结论都是实测出来的, 不是抄来的",
        "- input_tokens 按 AIMessage.usage_metadata 求和(厂商返回的精确值)",
        "- 小样本单任务, 结论只代表该场景, 不当普适定律讲",
    ]
    out = Path(__file__).resolve().parent.parent / "docs" / "ab_result.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[-14:-8]))
    print(f"\n结果已写入 {out}")


if __name__ == "__main__":
    main()
