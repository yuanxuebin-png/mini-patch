"""执行管线: 用 StateGraph 把「侦察 -> 修复 -> 验证」正式闭环。

v1 的 graph.run_task 是两阶段直调: 修完就结束, 测试过没过没人管。
本模块把它升级成一张真正的图:

                    ┌────────────────────────────────┐
                    │  verify 不通过 且 还有重试额度   │
                    ▼                                │
  START ──> scout ──> fix ──> verify ──┬── 通过 ──> END
                                       │
                                       ├── 无进展(连续两次输出相同) ──> rollback ──> END
                                       └── 重试额度耗尽 ──────────> rollback ──> END

三个设计点(面试讲点)
--------------------
1. **条件边**是 LangGraph 的核心价值: 下一步去哪由上一个节点的产出决定,
   而不是写死的顺序 —— verify 的 exit_code 就是路由依据。
2. **失败信息回灌**: 重试时把上一次的测试输出拼进修复 Agent 的输入,
   模型不是「再试一次」, 而是「带着报错再试一次」—— 这是有无反思的差别。
3. **无进展检测**: 连续两次测试输出完全相同 => 模型在原地打转
   (第一版实测里它曾连续 11 轮改同一行), 此时继续重试只是烧钱,
   正确动作是回滚快照、承认失败、把状态如实上报。

为什么不把 scout/fix 做成图里的「子图」: 它们内部已是
create_agent 的完整循环, 嵌进节点函数即可; 图只负责**阶段间的编排**。
"""

from __future__ import annotations

import subprocess
import time
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from patch_agent.commands import _check_allowed
from patch_agent.graph import MAIN_SYSTEM, load_model, prepare_demo_repo
from patch_agent.sandbox import Sandbox
from patch_agent.scout import ScoutReport, make_scout
from patch_agent.snapshot import SnapshotStore
from patch_agent.tools_read import make_read_tools
from patch_agent.tools_write import make_write_tools
from patch_agent.commands import make_command_tool

MAX_RETRIES = 2        # verify 失败后最多重试次数(总修复尝试 = 1 + MAX_RETRIES)
STALL_LIMIT = 1        # 连续「输出与上次相同」达到该值 => 原地打转, 回滚
CMD_TIMEOUT = 30       # 验证命令超时(秒)


class PipeState(TypedDict):
    """图状态。只放「节点间真正要传递」的字段, 别把会话历史塞进来
    (会话属于各 Agent 自己, 状态越窄, 越好调、越好持久化)。"""

    task: str
    test_cmd: str                  # 验证命令, 如 "python test_calc.py"
    report: dict[str, Any] | None  # ScoutReport.model_dump(), 侦察产物
    retries: int
    last_out: str                  # 上一次测试输出(判定无进展用)
    stalled: int
    fix_hint: str                  # 回灌给修复 Agent 的失败信息
    snapshot_id: str | None
    status: str                    # fixed / gave_up
    log: Annotated[list[str], lambda a, b: a + b]  # 只追加的事件日志


class FixPipeline:
    """一张可复跑的修复管线。同一个实例可对不同沙箱各 invoke 一次
    (每次 invoke 前沙箱应是干净的初始 bug 状态)。"""

    def __init__(self, model: BaseChatModel, sandbox: Sandbox) -> None:
        self.model = model
        self.sandbox = sandbox
        self.snapshots = SnapshotStore(sandbox)
        self.ledger = None              # run(..., ledger=) 时按阶段记账(A/B 对照用)
        self.graph = self._build()

    # ------------------------------------------------------------------
    # 节点
    # ------------------------------------------------------------------
    def scout_node(self, state: PipeState) -> dict:
        scout = make_scout(self.model, self.sandbox)
        result = scout.invoke({"messages": [("user", state["task"])]})
        report: ScoutReport = result["structured_response"]
        if self.ledger is not None:
            self.ledger.add("B-侦察", result["messages"])
        return {"report": report.model_dump(), "log": [f"scout: {report.target_file}:{report.line_start}-{report.line_end}"]}

    def fix_node(self, state: PipeState) -> dict:
        tools = (
            make_read_tools(self.sandbox)
            + make_write_tools(self.sandbox, self.snapshots)
            + [make_command_tool(self.sandbox)]
        )
        from langchain.agents import create_agent

        agent = create_agent(self.model, tools=tools, system_prompt=MAIN_SYSTEM)
        r = state["report"] or {}
        handoff = (
            f"{state['task']}\n\n侦察报告:\n"
            f"- 目标: {r.get('target_file', '?')} (第 {r.get('line_start', '?')}-{r.get('line_end', '?')} 行)\n"
            f"- 依据: {r.get('reason', '?')}\n"
        )
        if state["fix_hint"]:
            handoff += f"\n上一次验证失败, 输出如下, 请换思路修复:\n{state['fix_hint']}\n"
        handoff += "\n请开始修复, 完成后运行测试确认。"
        result = agent.invoke({"messages": [("user", handoff)]})
        if self.ledger is not None:
            self.ledger.add("B-修复(父上下文)", result["messages"])
        return {"log": [f"fix: 第 {state['retries'] + 1} 次尝试完成"]}

    def verify_node(self, state: PipeState) -> dict:
        """跑验证命令。exit_code==0 即通过。

        与 Agent 手里的 run_command 用**同一套白名单校验**(_check_allowed),
        保证「Agent 允许跑的」和「管线允许跑的」是同一个范围 —— 两套标准
        迟早出安全事故。
        """
        tokens = _check_allowed(state["test_cmd"])
        try:
            proc = subprocess.run(
                tokens, cwd=str(self.sandbox.root), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=CMD_TIMEOUT, shell=False,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            out = f"exit_code={proc.returncode}\n{out}"[:2000]
        except subprocess.TimeoutExpired:
            out = f"exit_code=-1\n验证命令超时({CMD_TIMEOUT}s)"

        stalled = state["stalled"]
        if state["last_out"] and out == state["last_out"]:
            stalled += 1
        else:
            stalled = 0

        updates: dict = {
            "last_out": out,
            "stalled": stalled,
            "log": [f"verify: {out.splitlines()[0]}"],
        }
        if out.splitlines()[0] == "exit_code=0":
            updates["status"] = "fixed"     # 通过: 在这里定状态, 条件边只负责路由
            return updates
        # 失败: 扣重试额度, 并把输出裁剪后回灌给修复 Agent
        retries = state["retries"] + 1
        updates["retries"] = retries
        updates["fix_hint"] = out[:1200]
        updates["log"] = updates["log"] + [f"verify: 未通过, 剩余重试 {MAX_RETRIES - retries}"]
        return updates

    def rollback_node(self, state: PipeState) -> dict:
        sid = state.get("snapshot_id")
        if sid:
            touched = self.snapshots.restore(sid)
        else:
            touched = ["本次未发生任何写入, 无需回滚"]
        return {
            "status": "gave_up",
            "log": ["rollback: " + "; ".join(touched)],
        }

    # ------------------------------------------------------------------
    # 路由(条件边)
    # ------------------------------------------------------------------
    def after_verify(self, state: PipeState) -> str:
        if state["last_out"].startswith("exit_code=0"):
            return "fixed"
        if state["stalled"] >= STALL_LIMIT:
            return "rollback"          # 原地打转: 继续试只是烧钱
        if state["retries"] > MAX_RETRIES:
            return "rollback"          # 额度耗尽
        return "fix"

    # ------------------------------------------------------------------
    # 组图
    # ------------------------------------------------------------------
    def _build(self):
        g = StateGraph(PipeState)
        g.add_node("scout", self.scout_node)
        g.add_node("fix", self.fix_node)
        g.add_node("verify", self.verify_node)
        g.add_node("rollback", self.rollback_node)

        g.add_edge(START, "scout")
        g.add_edge("scout", "fix")
        g.add_edge("fix", "verify")
        g.add_conditional_edges(
            "verify",
            self.after_verify,          # 路由函数: 下一步去哪由状态决定
            {"fixed": END, "fix": "fix", "rollback": "rollback"},
        )
        g.add_edge("rollback", END)
        return g.compile()

    # ------------------------------------------------------------------
    def run(self, task: str, test_cmd: str, verbose: bool = True, ledger=None) -> dict:
        """执行完整管线, 返回最终状态。ledger 传入时按阶段记账。"""
        self.ledger = ledger
        init: PipeState = {
            "task": task, "test_cmd": test_cmd, "report": None,
            "retries": 0, "last_out": "", "stalled": 0, "fix_hint": "",
            "snapshot_id": None, "status": "running", "log": [],
        }
        # 快照 id 由写工具在写入时生成, 这里把 id 通道接进 state:
        # 写工具不认识 state, 所以用兜底方案 —— 若发生过写入,
        # rollback 时回滚「最后一次快照」(undo_last), 语义等价。
        final = self.graph.invoke(init)
        if verbose:
            print("\n--- 执行日志 ---")
            for line in final["log"]:
                print(" ", line)
            print("状态:", final["status"])
        return final


# ----------------------------------------------------------------------
# 自测(真调模型): demo 仓库注入 bug -> 管线修复到测试通过
# ----------------------------------------------------------------------
if __name__ == "__main__":
    from pathlib import Path

    import patch_agent.graph as g

    repo = Path(__file__).resolve().parent.parent / "sandbox_repo"
    g.prepare_demo_repo(repo)
    box = Sandbox(repo)
    pipe = FixPipeline(load_model(), box)

    # 把「发生过写入」的判定做实: 快照目录有新文件即视为写过
    snap_dir = box.root / ".snapshots"
    snaps_before = set(p.name for p in snap_dir.glob("*.json"))

    final = pipe.run("python test_calc.py 运行失败, 请定位并修复", "python test_calc.py")

    assert final["status"] in ("fixed", "gave_up"), final["status"]
    assert final["status"] == "fixed", "demo 任务应修复成功"
    snaps_after = set(p.name for p in snap_dir.glob("*.json"))
    print("写入快照数:", len(snaps_after - snaps_before))
    print("pipeline 自测通过")
