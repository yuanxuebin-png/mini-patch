"""命令白名单执行器: 让 Agent 能跑测试, 但跑不了别的。

威胁模型
--------
「跑测试」必须给 Agent(否则改完无法验证), 但一个能执行任意命令的
工具等于把整台机器交出去。这里的取舍是**白名单而不是黑名单**:
先声明允许什么, 其余一律拒绝 —— 黑名单永远漏(新花样层出不穷),
白名单漏了也是「功能不够」而不是「安全事故」。

规则
----
1. 首 token 必须在 ALLOWED_FIRST 内: python / py / pytest / git
2. git 只允许只读子命令(status/diff/log/show) —— push/reset 直接拒
3. 任何 token 命中 DANGEROUS_TOKENS 直接拒: 覆盖 python -c、pip、rm 等
   (python -c 可以执行任意代码, 等于白名单形同虚设, 必须显式禁)
4. 执行: 无 shell、固定 cwd=沙箱根、超时强杀、输出截断

边界(面试讲点)
--------------
这是**进程级约定**, 不是 OS 级沙箱: 它防「模型乱来」, 不防「精心构造的
绕过」(比如 python 脚本内容本身做坏事)。彻底隔离要容器或受限子进程。
个人项目里这层 + 快照回滚 + 审计日志, 覆盖了绝大多数真实风险。
"""

from __future__ import annotations

import subprocess

from langchain_core.tools import BaseTool, ToolException, tool

from patch_agent.sandbox import Sandbox
from patch_agent.tools_read import _to_model_error

ALLOWED_FIRST = {"python", "py", "pytest", "git"}
GIT_READONLY = {"status", "diff", "log", "show"}
DANGEROUS_TOKENS = {
    "-c", "--command",           # python -c == 任意代码
    "pip", "install", "uninstall",
    "push", "reset", "rebase", "clean",
    "rm", "del", "rmdir", "rd", "format", "erase",
    "shutdown", "taskkill",
    "curl", "wget", "invoke-webrequest",
    "chmod", "chown", "sudo",
    "eval", "exec",
}
MAX_OUTPUT_CHARS = 4000


def _check_allowed(command: str) -> list[str]:
    """解析并校验命令; 返回 token 列表, 不合法抛 ToolException。"""
    tokens = [t.strip('"') for t in command.split()]  # 不用 shlex: 演示场景空格分割够用
    if not tokens:
        raise ToolException("空命令")
    if tokens[0].lower() not in ALLOWED_FIRST:
        raise ToolException(
            f"命令 {tokens[0]!r} 不在白名单(允许: {sorted(ALLOWED_FIRST)})"
        )
    if tokens[0].lower() == "git" and (len(tokens) < 2 or tokens[1].lower() not in GIT_READONLY):
        raise ToolException(f"git 仅允许只读子命令: {sorted(GIT_READONLY)}")
    lowered = {t.lower() for t in tokens}
    hit = lowered & DANGEROUS_TOKENS
    if hit:
        raise ToolException(f"命令包含被禁止的 token: {sorted(hit)}")
    return tokens


def make_command_tool(sandbox: Sandbox, timeout: int = 60) -> BaseTool:
    """按沙箱实例生产 run_command 工具。"""

    @tool
    @_to_model_error
    def run_command(command: str) -> str:
        """在沙箱根目录执行白名单内的命令并返回输出。

        允许: python <脚本>、pytest、git status/diff/log/show。
        禁止: python -c、pip、任何文件删除与网络下载命令。
        用它运行测试来验证你的修改。
        """
        tokens = _check_allowed(command)
        try:
            proc = subprocess.run(
                tokens,
                cwd=str(sandbox.root),        # 固定工作目录 = 沙箱根
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,              # 超时强杀, 防死循环测试拖死会话
                shell=False,                  # 永远不开 shell: 免受 cmd 内建与注入拼接
            )
        except FileNotFoundError:
            # 白名单允许 != 命令存在(pytest 未装时就会到这里):
            # 要转成模型可读的提示, 而不是抛 FileNotFoundError 崩掉调用
            raise ToolException(
                f"命令不存在(未安装或不在 PATH): {tokens[0]}"
            ) from None
        except subprocess.TimeoutExpired:
            raise ToolException(f"命令超时({timeout}s)被终止: {command}") from None

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        parts = [f"exit_code={proc.returncode}"]
        if out:
            parts.append("--- stdout ---\n" + out)
        if err:
            parts.append("--- stderr ---\n" + err)
        result = "\n".join(parts)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n...(输出过长已截断)"
        return result

    return run_command


# ----------------------------------------------------------------------
# 自测: python -m patch_agent.commands
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(tmp)
        tool = make_command_tool(box, timeout=5)
        (box.root / "hello.py").write_text("print('hello from sandbox')", encoding="utf-8")
        (box.root / "loop.py").write_text(
            "import time\nwhile True:\n    time.sleep(0.1)\n", encoding="utf-8"
        )

        # [1] 白名单命令执行成功
        out = tool.invoke({"command": "python hello.py"})
        assert "exit_code=0" in out and "hello from sandbox" in out, out
        print("[1] python 脚本     OK")

        # [2] pytest 在白名单内(允许执行); 本机未装 pytest,
        #     预期返回"命令不存在"的友好提示而不是崩溃 —— 验证白名单与存在性分层
        out = tool.invoke({"command": "pytest --version"})
        assert ("exit_code" in out) or ("命令不存在" in out), out
        print("[2] pytest 分层     OK ->", out.splitlines()[0])

        # [3] python -c 必须被拒
        out = tool.invoke({"command": "python -c \"print('boom')\""})
        assert "被禁止" in out, out
        print("[3] python -c 拦截  OK")

        # [4] pip 必须被拒
        out = tool.invoke({"command": "python -m pip install requests"})
        assert "被禁止" in out, out
        print("[4] pip 拦截        OK")

        # [5] git push 拒, git status 允许
        out = tool.invoke({"command": "git push origin main"})
        assert "仅允许只读" in out, out
        out = tool.invoke({"command": "git status"})
        assert not out.startswith("错误"), out
        print("[5] git 读写分级    OK")

        # [6] 白名单外命令拒
        out = tool.invoke({"command": "node build.js"})
        assert "不在白名单" in out, out
        print("[6] 白名单外拒绝    OK")

        # [7] 超时强杀
        out = tool.invoke({"command": "python loop.py"})
        assert "超时" in out, out
        print("[7] 超时强杀        OK")

    print("commands 全部自测通过")
