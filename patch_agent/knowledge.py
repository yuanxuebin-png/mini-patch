"""知识库联动工具 —— 双项目从「两个 Demo」变「一套系统」的钉子。

定位
----
patch-agent 的 Agent 在写代码/修代码时, 可能需要项目规范、技术约定等
私有知识(通用大模型不懂你的仓库规矩)。本工具通过 HTTP 调用 pocket-rag
的查询接口, 让 Agent 能主动检索私有知识库。

设计要点
--------
1. 只读工具: 两种模式(动手/建议)都可用, 不破坏权限模型。
2. 失败降级, 不许谎报: 服务没启动/超时, 返回明确的「不可用 + 怎么启动」,
   绝不返回空字符串让模型自己脑补。
3. 超时给足: /query 背后是一次真实的模型调用(检索+生成), 90s 上限,
   比普通网络请求长, 但仍防止 Agent 卡死。
4. 拒答透传: 知识库拒答(refused=true)时原样告诉模型「库里没有」,
   让它基于代码本身判断, 而不是编一条规范出来。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8765"
QUERY_TIMEOUT = 90          # 背后是检索+模型生成, 给足但不无限


def _base_url() -> str:
    return os.environ.get("RAG_API_URL", DEFAULT_BASE).rstrip("/")


def query_knowledge(question: str) -> str:
    """问私有知识库一个技术/规范问题, 返回带来源的答案或明确的失败说明。"""
    base = _base_url()
    try:
        req = urllib.request.Request(
            f"{base}/query",
            data=json.dumps({"question": question}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return (
            "知识库服务不可用(未启动或连接失败)。"
            "请提示用户: 先运行 python -m pocket_rag.api 启动知识库, 再重试。"
            "没有知识库也能继续干活, 只是基于你自己的判断。"
        )
    except Exception as e:                              # 未知错误也如实上报
        return f"知识库查询出错: {e}"

    if payload.get("refused"):
        return (
            "知识库明确拒答: 库中没有与该问题相关的内容。"
            "不要编造规范, 按代码库现有风格自行判断即可。"
        )
    lines = [payload["answer"]]
    if payload.get("sources"):
        lines.append("来源: " + "; ".join(payload["sources"]))
    return "\n".join(lines)


def make_knowledge_tool():
    """工厂: 返回挂到 Agent 上的知识库查询工具。"""
    from langchain_core.tools import tool

    @tool
    def query_knowledge_tool(question: str) -> str:
        """查询用户的私有知识库(项目规范、技术笔记、API 约定等)。
        当任务涉及「按项目规范」「按约定」「项目里怎么做的」这类
        通用大模型不知道的私有知识时调用; 普通编程问题不用调。"""
        return query_knowledge(question)

    return query_knowledge_tool
