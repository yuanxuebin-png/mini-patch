"""编排层: 侦察 -> 修复 的两阶段任务执行。

为什么是「两阶段直调」而不是一张大图(v1 取舍):
- create_agent 内部已自带「模型 <-> 工具」循环, 外层再包一层图,
  在 v1 里只会增加调试成本而不增加能力
- 两阶段的边界清晰可打印: 阶段 1 的产物(ScoutReport)就是阶段 2 的输入,
  token 账本(后续 commit)按阶段记账也就顺理成章
- 升级路线(roadmap): 外层换成 StateGraph, 加 verify 节点与条件边,
  测试不过自动回到修复阶段, 并在 max_retries 后回滚快照

.env 查找顺序:
1. patch-agent/.env        本项目自己的
2. D:/Code_Projects/Agent_Create/.env   学习项目已有的(内含 DeepSeek key)
load_dotenv 只在 CWD 找 .env, 所以显式传路径。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from patch_agent.sandbox import Sandbox
from patch_agent.scout import ScoutReport, make_scout
from patch_agent.snapshot import SnapshotStore
from patch_agent.tools_read import make_read_tools
from patch_agent.tools_write import make_write_tools
from patch_agent.commands import make_command_tool

ENV_CANDIDATES = [
    Path(__file__).resolve().parent.parent / ".env",
    Path("D:/Code_Projects/Agent_Create/.env"),
]


def load_model(temperature: float = 0) -> BaseChatModel:
    """读 .env 并构建主模型(DeepSeek)。"""
    for env in ENV_CANDIDATES:
        if env.exists():
            load_dotenv(env)
            break
    return init_chat_model(
        os.getenv("PRIMARY_MODEL", "openai:deepseek-chat"),
        temperature=temperature,
    )


MAIN_SYSTEM = """你是代码修复工程师, 在沙箱化的示例仓库中工作。

给你一段侦察报告(目标文件与行号)和故障现象。流程:
1. 先 read_snippet 精读目标位置及其上下文, 必要时补充侦察
2. 用 apply_edit 做最小修改 —— 不要顺手重构, 不要改无关代码
3. 用 run_command 运行测试验证, 修到测试通过为止
4. 测试通过后立即收尾汇报, 不要继续其他改动

约束: 所有路径必须在沙箱内; 禁止绕过工具直接操作。
"""


def run_task(model: BaseChatModel, sandbox: Sandbox, task: str, verbose: bool = True) -> dict:
    """执行一个修复任务: 侦察 -> 修复。返回两阶段的产物与消息记录。"""

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    # ---- 阶段 1: 只读侦察 ------------------------------------------
    say("=== 阶段 1: 侦察(只读子 Agent) ===")
    scout = make_scout(model, sandbox)
    scout_result = scout.invoke({"messages": [("user", task)]})
    report: ScoutReport = scout_result["structured_response"]
    say(f"定位: {report.target_file} 第 {report.line_start}-{report.line_end} 行")
    say(f"依据: {report.reason}")

    # ---- 阶段 2: 修复(读写 + 命令) ---------------------------------
    say("=== 阶段 2: 修复(主 Agent) ===")
    snapshots = SnapshotStore(sandbox)
    tools = (
        make_read_tools(sandbox)
        + make_write_tools(sandbox, snapshots)
        + [make_command_tool(sandbox)]
    )
    from langchain.agents import create_agent

    main_agent = create_agent(model, tools=tools, system_prompt=MAIN_SYSTEM)
    handoff = (
        f"{task}\n\n侦察报告:\n"
        f"- 目标: {report.target_file} (第 {report.line_start}-{report.line_end} 行)\n"
        f"- 依据: {report.reason}\n"
        f"请开始修复, 修到测试通过。"
    )
    main_result = main_agent.invoke({"messages": [("user", handoff)]})
    final_text = main_result["messages"][-1].content

    say("=== 收尾 ===")
    say(final_text[:500])
    return {
        "scout_report": report,
        "scout_messages": scout_result["messages"],
        "main_messages": main_result["messages"],
        "final_text": final_text,
        "snapshots": snapshots,
    }


# ----------------------------------------------------------------------
# demo 仓库(注入的 bug): 空列表求平均 -> 除零
# ----------------------------------------------------------------------
DEMO_CALC = '''def average(numbers):
    """计算平均值。空列表应返回 0.0。"""
    total = sum(numbers)
    return total / len(numbers)
'''

DEMO_TEST = '''from calc import average


def check():
    assert average([2, 4, 6]) == 4.0, "基本用例失败"
    assert average([]) == 0.0, "空列表应返回 0.0"
    print("all tests passed")


if __name__ == "__main__":
    check()
'''


def prepare_demo_repo(repo_dir: Path) -> None:
    """重建演示仓库(幂等): 每次都恢复为「带 bug 的原始状态」。"""
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "calc.py").write_text(DEMO_CALC, encoding="utf-8")
    (repo_dir / "test_calc.py").write_text(DEMO_TEST, encoding="utf-8")


if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent / "sandbox_repo"
    prepare_demo_repo(repo)
    box = Sandbox(repo)
    result = run_task(
        load_model(),
        box,
        "python test_calc.py 运行失败, 请定位并修复, 修到测试通过",
    )
    print("\n===== 结构化侦察结论 =====")
    print(result["scout_report"].model_dump())
