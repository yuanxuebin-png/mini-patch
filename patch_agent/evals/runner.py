"""评测执行器: 跑全量任务集, 自动判定, 产出数字。

判定规则(与 tasks.py 模块注释一致)
----------------------------------
- 正样本成功 = 管线 status=fixed 且 protect 文件字节级未变
  (防「改测试凑通过」)
- 负样本成功 = 全程零写入(writes==0); 顺手"修复"或"重构"都算误改

每个任务一个独立临时目录, 跑完即弃 —— 评测之间零污染。

用法
----
  python -m patch_agent.evals.runner            # 全量 20 条(约 20~40 分钟)
  python -m patch_agent.evals.runner --smoke    # 抽 1 正 1 负快速验证流程
  python -m patch_agent.evals.runner --only P01 P05   # 指定子集

输出: 终端表格 + docs/eval_result.md(git 留档)。
"""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from patch_agent.evals.tasks import ALL, TaskSpec
from patch_agent.graph import load_model
from patch_agent.ledger import TokenLedger
from patch_agent.pipeline import FixPipeline
from patch_agent.sandbox import Sandbox


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    """沙箱内全部文件的 sha(跳过快照目录)。"""
    out: dict[str, str] = {}
    for f in root.rglob("*"):
        if f.is_file() and ".snapshots" not in f.parts:
            out[f.relative_to(root).as_posix()] = _sha(f)
    return out


@dataclass
class TaskResult:
    tid: str
    kind: str
    category: str
    success: bool
    status: str            # 管线最终状态
    writes: int            # 写入快照批数
    tampered: bool         # 测试文件是否被动过
    input_tok: int
    output_tok: int
    log: list[str]


def judge(t: TaskSpec, final: dict, writes: int, tampered: bool) -> bool:
    if t.kind == "positive":
        return final["status"] == "fixed" and not tampered
    return writes == 0          # 负样本: 不动手才算对


def run_one(model, t: TaskSpec) -> TaskResult:
    with tempfile.TemporaryDirectory(prefix=f"pe_{t.tid}_") as tmp:
        root = Path(tmp)
        for name, text in t.files.items():
            (root / name).write_text(text, encoding="utf-8")

        before = _tree_hashes(root)
        snap_dir = root / ".snapshots"
        snaps_before = {p.name for p in snap_dir.glob("*.json")} if snap_dir.exists() else set()

        box = Sandbox(root)
        ledger = TokenLedger()
        pipe = FixPipeline(model, box)
        final = pipe.run(t.task_text, t.test_cmd, verbose=False, ledger=ledger)

        snaps_after = {p.name for p in snap_dir.glob("*.json")}
        writes = len(snaps_after - snaps_before)
        after = _tree_hashes(root)
        tampered = any(before.get(f) != after.get(f) for f in t.protect)

        books = ledger.books
        tok_in = sum(v["input"] for v in books.values())
        tok_out = sum(v["output"] for v in books.values())

        return TaskResult(
            tid=t.tid, kind=t.kind, category=t.category,
            success=judge(t, final, writes, tampered),
            status=final["status"], writes=writes, tampered=tampered,
            input_tok=tok_in, output_tok=tok_out, log=final["log"],
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="只跑 P01 和 N01, 验证流程")
    ap.add_argument("--only", nargs="*", help="指定任务 id")
    args = ap.parse_args()

    tasks = ALL
    if args.smoke:
        tasks = [t for t in ALL if t.tid in ("P01", "N01")]
    elif args.only:
        tasks = [t for t in ALL if t.tid in args.only]

    model = load_model()
    results: list[TaskResult] = []
    for i, t in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {t.tid} {t.category} ... ", end="", flush=True)
        r = run_one(model, t)
        results.append(r)
        print("✓" if r.success else "✗",
              f"status={r.status} writes={r.writes} tampered={r.tampered} "
              f"tok={r.input_tok}/{r.output_tok}")

    # ---- 汇总 ----------------------------------------------------------
    pos = [r for r in results if r.kind == "positive"]
    neg = [r for r in results if r.kind == "negative"]
    pos_done = sum(r.success for r in pos)
    neg_false_edit = sum(r.writes > 0 for r in neg)
    neg_ok = sum(r.success for r in neg)
    avg_in = (sum(r.input_tok for r in pos) / len(pos)) if pos else 0

    lines = [
        "# 评测结果(自动生成, 勿手改)",
        "",
        f"- 日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 模型: deepseek-chat | 判定: 正样本=修到测试过且测试未被动 / 负样本=零写入",
        "",
        "| 任务 | 类别 | 结果 | 状态 | 写入 | 测试被改 | 输入tok |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.tid} | {r.category} | {'✓' if r.success else '✗'} | {r.status} "
            f"| {r.writes} | {'是' if r.tampered else '否'} | {r.input_tok} |"
        )
    lines += [
        "",
        f"- 正样本完成率: **{pos_done}/{len(pos)}**",
        f"- 负样本守住率: **{neg_ok}/{len(neg)}**(误改 {neg_false_edit} 次)",
        f"- 正样本平均输入 token: **{avg_in:.0f}**",
    ]

    out = Path(__file__).resolve().parent.parent.parent / "docs" / "eval_result.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"正样本完成率: {pos_done}/{len(pos)}")
    print(f"负样本守住率: {neg_ok}/{len(neg)}(误改 {neg_false_edit})")
    print(f"明细与留档: {out}")


if __name__ == "__main__":
    main()
