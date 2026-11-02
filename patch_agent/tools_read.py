"""只读工具层: list_dir / grep / read_snippet。

设计要点
--------
1. 闭包工厂 make_read_tools(sandbox): 工具需要运行时依赖(沙箱实例),
   @tool 装饰器不支持给工具传额外参数, 标准做法是用工厂闭包把依赖
   「焊死」进工具函数 —— 模型只看到 docstring 里声明的参数。
2. 工具内部捕获 SandboxViolation 转成字符串返回: 让模型看到错误
   并自行纠正, 而不是让整次调用崩掉。
3. 所有输出都有长度上限: 工具输出会进入模型上下文, 不设上限的
   read_file 是上下文爆炸的头号来源(尤其整文件读取)。
4. 这三个工具就是侦察子 Agent(scout.py)的全部视力 —— 只有眼睛,
   没有手, 这是权限隔离的物理实现。
"""

from __future__ import annotations

from functools import wraps
from pathlib import Path

from langchain_core.tools import BaseTool, ToolException

from patch_agent.sandbox import Sandbox, SandboxViolation

# 遍历时跳过的目录与扩展名(二进制/依赖目录对代码任务无意义, 纯属浪费 token)
SKIP_DIRS = {".git", "__pycache__", ".snapshots", ".idea", ".vscode", "node_modules"}
TEXT_EXTS = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"}

MAX_GREP_HITS = 20       # grep 最多返回条数
MAX_SNIPPET_LINES = 80   # 单次读取最多行数
MAX_OUTPUT_CHARS = 6000  # 单个工具输出的字符上限


def _iter_text_files(root: Path):
    """yield 沙箱内所有文本文件(跳过依赖/隐藏目录)。"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        yield p


def _to_model_error(fn):
    """把工具内抛出的 ToolException 转成字符串返回给模型。

    为什么不用 langchain 内置的 handle_tool_error 机制:
    BaseTool.run() 只有在 handle_tool_error=True 时才把 ToolException
    转成字符串, 否则直接 raise; 而 1.6.x 的 @tool 装饰器不暴露这个参数,
    要用就得手工构造 StructuredTool。六行装饰器更直白, 行为也一样。
    """

    @wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolException as e:
            return f"错误: {e}"

    return inner


def make_read_tools(sandbox: Sandbox) -> list[BaseTool]:
    """按沙箱实例生产只读工具三件套。"""

    def guard(raw: str) -> Path:
        """统一的路径入口。

        抛 ToolException(不是 ValueError): langchain 只会把 ToolException
        转成字符串返回给模型(工具失败但对话继续); 其他异常会直接炸掉
        整次 invoke。这是「工具内出错给谁看」的关键区别。
        """
        try:
            return sandbox.resolve(raw)
        except SandboxViolation as e:
            raise ToolException(str(e)) from None

    # ------------------------------------------------------------------
    @tool
    @_to_model_error
    def list_dir(path: str = ".") -> str:
        """列出沙箱内某个目录的第一层内容。每行格式: [dir] 名称 或 [file] 名称。

        用于了解项目结构。看具体代码请用 read_snippet, 全局搜索请用 grep。
        """
        target = guard(path)
        if not target.exists():
            return f"错误: {path} 不存在"
        if not target.is_dir():
            return f"错误: {path} 不是目录, 它是文件; 读内容请用 read_snippet"

        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for e in entries[:60]:  # 上限 60 条, 大目录靠 grep/子目录定位
            tag = "dir" if e.is_dir() else "file"
            lines.append(f"[{tag}] {e.name}")
        if not lines:
            return "(空目录)"
        return "\n".join(lines)

    # ------------------------------------------------------------------
    @tool
    @_to_model_error
    def grep(pattern: str, path: str = ".", ignore_case: bool = False) -> str:
        """在沙箱内的文本文件中搜索子串, 返回 文件:行号: 内容 列表(最多 20 条)。

        pattern 是普通子串不是正则。找函数定义可搜 "def 函数名"。
        path 可缩小范围到某个子目录。命中太多时请换更长的关键词。
        """
        root = guard(path)
        hits: list[str] = []
        needle = pattern if not ignore_case else pattern.lower()

        for f in _iter_text_files(root):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                hay = line if not ignore_case else line.lower()
                if needle in hay:
                    rel = sandbox.rel(f)
                    shown = line.strip()[:160]  # 单行截断, 上下文友好
                    hits.append(f"{rel}:{lineno}: {shown}")
                    if len(hits) >= MAX_GREP_HITS:
                        hits.append(f"(达到 {MAX_GREP_HITS} 条上限, 请换更精确关键词)")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else f"未找到: {pattern!r}"

    # ------------------------------------------------------------------
    @tool
    @_to_model_error
    def read_snippet(path: str, start: int = 1, end: int = 60) -> str:
        """读取文件的第 start 到 end 行(行号从 1 开始, 含两端), 带行号前缀。

        单次最多 80 行。先 grep 定位再精读小段, 不要整文件读取。
        """
        f = guard(path)
        if not f.is_file():
            return f"错误: {path} 不是文件"
        start = max(1, int(start))
        end = int(end)
        if end - start + 1 > MAX_SNIPPET_LINES:
            end = start + MAX_SNIPPET_LINES - 1  # 静默收口, 注明即可

        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError as e:
            return f"读取失败: {e}"

        if start > len(lines):
            return f"错误: 文件共 {len(lines)} 行, start={start} 超出"

        seg = lines[start - 1 : end]
        out = [f"{start + i:4d} | {ln}" for i, ln in enumerate(seg)]
        if len(out) > MAX_SNIPPET_LINES:
            out = out[:MAX_SNIPPET_LINES]
        body = "\n".join(out)
        if len(body) > MAX_OUTPUT_CHARS:
            body = body[:MAX_OUTPUT_CHARS] + "\n...(输出截断, 请缩小行范围)"
        tail = f"\n(文件共 {len(lines)} 行)" if end < len(lines) else ""
        return body + tail

    # ------------------------------------------------------------------
    # name/description 完全来自函数名与 docstring, 模型看到的就是它们
    return [list_dir, grep, read_snippet]


# ----------------------------------------------------------------------
# 自测: python -m patch_agent.tools_read
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    from langchain_core.tools import tool  # noqa: F401  (确认装饰器可用)

    with tempfile.TemporaryDirectory() as tmp:
        box = Sandbox(tmp)
        (box.root / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef bad_div(a, b):\n    return a / b\n",
            encoding="utf-8",
        )
        (box.root / "sub").mkdir()
        (box.root / "sub" / "util.py").write_text("MAX = 10\n", encoding="utf-8")

        tools = {t.name: t for t in make_read_tools(box)}

        # [1] list_dir
        out = tools["list_dir"].invoke({"path": "."})
        assert "[file] calc.py" in out and "[dir] sub" in out, out
        print("[1] list_dir        OK")

        # [2] grep 命中带 文件:行号 (内容里 def bad_div 在第 5 行)
        out = tools["grep"].invoke({"pattern": "def bad_div"})
        assert "calc.py:5:" in out, out
        print("[2] grep            OK ->", out.splitlines()[0])

        # [3] grep 无命中
        out = tools["grep"].invoke({"pattern": "zzz_not_exist"})
        assert out.startswith("未找到"), out
        print("[3] grep 未命中提示 OK")

        # [4] read_snippet 行号与范围
        out = tools["read_snippet"].invoke({"path": "calc.py", "start": 5, "end": 6})
        assert " 5 | def bad_div" in out and " 6 |     return a / b" in out, out
        print("[4] read_snippet    OK")

        # [5] 越界被转成可读错误而不是崩溃
        out = tools["read_snippet"].invoke({"path": "../secret.txt", "start": 1, "end": 2})
        assert "路径越界" in out, out
        print("[5] 越界 -> 模型可读错误 OK")

    print("tools_read 全部自测通过")
