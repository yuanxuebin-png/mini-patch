"""交互式对话窗口 —— patch-agent 的「Claude Code 壳」。

定位
----
pipeline.py 是无人值守的批处理引擎(评测用), 本文件是它的交互形态:
你在终端里打字下指令, Agent 要么动手(改文件/建文件/跑测试), 要么只给建议。

动手 / 建议两种模式, 权限差异体现在「给模型的工具不一样」:
- 动手模式: 只读三件套 + apply_edit/write_file + run_command
- 建议模式: 只有只读三件套 —— 模型物理上写不了文件, 不是靠提示词求它别写

内置命令(以 / 开头, 不进模型):
  /mode      切换 动手/建议 模式(切换后对话历史清空, 防止两种权限串台)
  /undo      回滚最近一次写入(快照层)
  /tokens    查看本会话累计 token
  /exit      退出

知识库联动: 两种模式都挂了 query_knowledge_tool(只读), 通过 HTTP 调
pocket-rag 的查询接口(python -m pocket_rag.api 启动, 默认 127.0.0.1:8765,
可用环境变量 RAG_API_URL 覆盖); 服务没启动时工具会明确降级提示, 不谎报。

已知边界(v1, 诚实标注):
- 对话历史无压缩, 轮次太多会变慢变贵 —— 超过 60 条消息会提示重开
- DeepSeek 上 max_tokens 限长写法沿用 extra_body 结论(见 graph.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from rich.console import Console

from patch_agent.commands import make_command_tool
from patch_agent.graph import load_model
from patch_agent.knowledge import make_knowledge_tool
from patch_agent.sandbox import Sandbox
from patch_agent.snapshot import SnapshotStore
from patch_agent.tools_read import make_read_tools
from patch_agent.tools_write import make_write_tools

console = Console()

HISTORY_SOFT_CAP = 60      # 超过就提示重开会话(历史无压缩的诚实提醒)
TOOL_PRINT_CHARS = 160     # 工具结果在终端里的展示截断

ACT_PROMPT = """你是这个仓库的代码助手, 仓库根目录就是你的沙箱, 你只能操作沙箱内的文件。

习惯用法:
1. 改代码优先用 apply_edit(old_text 唯一命中才执行), 新建文件用 write_file
2. 修改后用 run_command 跑测试验证, 把结果如实汇报给用户
3. 不确定就先 read_snippet 看清楚再动手; 命中多次时停下来问, 不要猜
4. 不做用户没要求的额外重构
5. 任务涉及「项目规范」「约定」「项目里怎么做的」时, 先用 query_knowledge_tool
   查私有知识库; 它拒答就说库里没有, 不要编造规范

重要: 用户提到的路径可能不在沙箱内。如果用户说的目录和当前沙箱不是同一个,
明确告诉他「当前会话的工作区是 <沙箱路径>, 你提到的目录我访问不了,
请退出后用 python -m patch_agent.chat 该目录路径 重新启动」——
绝对不要假装文件建在了他说的目录里。

回答用中文, 简洁, 关键改动说清楚改了哪个文件哪一段。"""

ADVISE_PROMPT = """你是这个仓库的代码分析顾问。你只有只读工具, 物理上无法修改任何文件。

用户会描述问题或想法, 你用 list_dir/grep/read_snippet 查看代码后,
给出: 问题定位(文件:行号) + 原因分析 + 具体修改建议(给出可直接粘贴的代码片段)。
但你自己不动手改 —— 用户确认后再切到动手模式执行。
若建议依赖项目规范/约定, 可先用 query_knowledge_tool 查私有知识库。

回答用中文。"""


def build_agent(mode: str, sandbox: Sandbox):
    """按模式组装 Agent。权限差异 = 工具清单差异。

    query_knowledge_tool 是只读工具, 两种模式都挂 —— 它不动沙箱,
    破坏不了权限模型; 建议模式少了它反而没法给「符合规范」的建议。
    """
    model = load_model()
    snapshots = SnapshotStore(sandbox)
    if mode == "act":
        tools = (
            make_read_tools(sandbox)
            + make_write_tools(sandbox, snapshots)
            + [make_command_tool(sandbox), make_knowledge_tool()]
        )
        prompt = ACT_PROMPT
    else:
        tools = [*make_read_tools(sandbox), make_knowledge_tool()]
        prompt = ADVISE_PROMPT
    agent = create_agent(model, tools=tools, system_prompt=prompt)
    return agent, snapshots


def run_turn(agent, history: list[BaseMessage], user_text: str):
    """执行一轮对话: 追加用户输入 -> 调用 Agent -> 返回新历史。

    历史整段传递: create_agent 内部自带「模型<->工具」循环,
    上一轮的工具调用与结果都留在 history 里, 上下文天然连续。
    """
    result = agent.invoke({"messages": [*history, ("user", user_text)]})
    return list(result["messages"])


def print_turn(new_msgs: list[BaseMessage]) -> None:
    """把一轮产生的消息打印成「过程 + 结论」的彩色终端视图。

    模型输出一律 markup=False + highlight=False: rich 的 markup 会把
    答案里的 [1] 引用编号当成标签解析掉, 内容必须原样渲染。
    """
    for m in new_msgs:
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                args = ", ".join(f"{k}={str(v)[:80]!r}" for k, v in tc["args"].items())
                console.print(f"  [tool.call]→ [cyan]{tc['name']}[/cyan]({args})")
        elif isinstance(m, ToolMessage):
            text = str(m.content).replace("\n", " ⏎ ")
            if len(text) > TOOL_PRINT_CHARS:
                text = text[:TOOL_PRINT_CHARS] + "…"
            console.print(f"  [tool.result] {text}", style="dim", markup=False)
        elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
            console.print()
            console.print(m.content, markup=False, highlight=False)
            console.print()


def _token_of(messages: list[BaseMessage]) -> tuple[int, int]:
    """从一轮消息里汇总 usage_metadata(厂商返回的精确值, 零侵入)。"""
    tin = tout = 0
    for m in messages:
        u = getattr(m, "usage_metadata", None)
        if u:
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
    return tin, tout


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "sandbox_repo"
    )
    sandbox = Sandbox(root)
    mode = "act"
    agent, snapshots = build_agent(mode, sandbox)
    history: list[BaseMessage] = []
    total_in = total_out = 0

    console.print("[bold cyan]MiniPatch[/bold cyan] 对话窗口", markup=True)
    console.print(f"工作区: {sandbox.root}", style="dim")
    console.print(
        "模式: [green]动手(可改文件)[/green] | 命令: /mode 切建议 /undo 回滚"
        " /tokens 用量 /exit 退出"
    )
    console.print(
        "提示: 只能读写工作区内文件, 要操作别的目录用 "
        "python -m patch_agent.chat 那个目录路径 启动; "
        "私有知识库可用: python -m pocket_rag.api",
        style="dim",
    )
    console.print()

    while True:
        try:
            text = console.input("[bold green]你> [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/exit":
            break
        if text == "/mode":
            mode = "advise" if mode == "act" else "act"
            agent, snapshots = build_agent(mode, sandbox)
            history = []                      # 换权限必须清历史, 防串台
            tag = "[green]动手[/green]" if mode == "act" else "[yellow]建议[/yellow]"
            console.print(f"已切换到{tag}模式(对话已清空)\n")
            continue
        if text == "/undo":
            console.print(f"  {snapshots.undo_last() or '(没有可回滚的快照)'}",
                          style="dim", markup=False)
            continue
        if text == "/tokens":
            console.print(
                f"  本会话累计: 输入 {total_in} tok / 输出 {total_out} tok",
                style="dim",
            )
            continue

        before = len(history)
        with console.status("[dim]思考中…[/dim]"):
            history = run_turn(agent, history, text)
        new_msgs = history[before + 1:]       # 跳过刚追加的 user 消息
        print_turn(new_msgs)

        tin, tout = _token_of(new_msgs)
        total_in, total_out = total_in + tin, total_out + tout
        console.print(
            f"  (本轮 {tin}/{tout} tok, 累计 {total_in}/{total_out})",
            style="dim",
        )
        console.print()

        if len(history) > HISTORY_SOFT_CAP:
            console.print(
                f"  [yellow]提示[/yellow] 对话已 {len(history)} 条消息, "
                "建议退出重开, 否则越来越慢。"
            )

    console.print("再见。", style="dim")


if __name__ == "__main__":
    main()
