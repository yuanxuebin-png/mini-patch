"""patch-agent 包。

代码修改智能体: 读代码 -> 定位 -> 修改 -> 测试 -> 根据报错继续修。
分层: sandbox(安全) -> tools(能力) -> snapshot(可回滚) -> graph(编排) -> scout(隔离侦察)。
"""

__version__ = "0.1.0"
