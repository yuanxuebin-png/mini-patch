"""侦察子 Agent: 只读定位, 结构化交卷 —— 隔离式分工的落地实现。

设计对应 docs/isolation.md:
- 上下文隔离: scout 读多少代码都在它自己的会话里, 父任务只收 4 个字段
- 权限隔离:  工具列表只有 make_read_tools 的三件套, 物理上没有写能力
- 状态隔离:  response_format=ScoutReport, 结论是 Pydantic 对象而非自由文本

response_format 的实测说明: DeepSeek 不支持 json_schema 的
response_format, 但 create_agent(response_format=Schema) 内部走
function_calling 路线, 在 DeepSeek 上可用(实测验证)。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel

from patch_agent.sandbox import Sandbox
from patch_agent.tools_read import make_read_tools

SCOUT_SYSTEM = """你是代码侦察员, 任务是给后续的修复者定位故障代码位置。

规则:
- 你只有只读工具, 不能也不需要修改任何文件
- 先 list_dir 了解结构, 用 grep 定位关键词, 用 read_snippet 精读小段
- 报错的测试文件往往直接指向问题模块, 优先读它
- 最终只通过结构化报告交卷; reason 一句话, 不超过 80 字
"""


class ScoutReport(BaseModel):
    """侦察结论: 父子之间唯一的接口。字段越窄, 隔离越牢。"""

    target_file: str = Field(description="需要修改的文件, 相对沙箱根的路径")
    line_start: int = Field(description="问题代码起始行, 从 1 开始")
    line_end: int = Field(description="问题代码结束行(含)")
    reason: str = Field(
        max_length=80,
        description="为什么是这里, 一句话依据, 不超过 80 字, 不要复述代码",
    )


def make_scout(model: BaseChatModel, sandbox: Sandbox):
    """构建只读侦察 Agent。返回的 compiled graph 可直接 .invoke()。"""
    return create_agent(
        model,
        tools=make_read_tools(sandbox),
        system_prompt=SCOUT_SYSTEM,
        response_format=ScoutReport,
    )


# ----------------------------------------------------------------------
# 自测(真调模型): python -m patch_agent.scout
# 前置: 沙箱根存在 demo 仓库(先跑 python -m patch_agent.demo --prepare)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from pathlib import Path

    from patch_agent.graph import load_model, prepare_demo_repo

    repo = Path(__file__).resolve().parent.parent / "sandbox_repo"
    prepare_demo_repo(repo)
    box = Sandbox(repo)

    scout = make_scout(load_model(), box)
    result = scout.invoke(
        {"messages": [("user", "python test_calc.py 抛 ZeroDivisionError, 定位问题位置")]}
    )
    report: ScoutReport = result["structured_response"]
    print("target_file:", report.target_file)
    print("line_range :", report.line_start, "-", report.line_end)
    print("reason     :", report.reason)
