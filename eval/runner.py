"""
评测执行器（Runner）
===========================================================================
把「题目集 × adapter」跑一遍，输出可复现的分数。

用法：
    # 1) 自检：Oracle 必须接近满分
    python eval/runner.py --adapter oracle

    # 2) 跑某个对照组
    python eval/runner.py --adapter rule-baseline

    # 3) 一次跑多组，直接出对比表（消融实验）
    python eval/runner.py --compare oracle rule-baseline null

    # 4) 只跑某一类题
    python eval/runner.py --adapter oracle --category f_conflict

产物：
    eval/results/<adapter>.json   每题的逐项得分 + 汇总
===========================================================================
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QUESTIONS = os.path.join(HERE, "questions")
RESULTS = os.path.join(HERE, "results")

sys.path.insert(0, HERE)

from adapters import AgentAdapter, RunContext, get_adapter  # noqa: E402
from verifier import Verdict, aggregate, score_question  # noqa: E402


# --------------------------------------------------------------------------
# 题目加载
# --------------------------------------------------------------------------

def load_questions(
    categories: Iterable[str] | None = None,
    qids: Iterable[str] | None = None,
) -> list[dict]:
    """
    加载题目。默认加载全部 8 类。

    categories: 例如 ["f_conflict", "g_attribution"]
    qids:       例如 ["F-001", "G-005"]，优先级高于 categories
    """
    if qids:
        wanted = {q.strip().upper() for q in qids}
        files = sorted(glob.glob(os.path.join(QUESTIONS, "*.jsonl")))
    elif categories:
        wanted = None
        files = [os.path.join(QUESTIONS, f"{c}.jsonl") for c in categories]
    else:
        wanted = None
        files = sorted(glob.glob(os.path.join(QUESTIONS, "*.jsonl")))

    out: list[dict] = []
    for path in files:
        if not os.path.exists(path):
            raise SystemExit(f"找不到题目文件: {path}")
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                q = json.loads(line)
                if wanted is not None and q["qid"].upper() not in wanted:
                    continue
                out.append(q)
    return out


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def run(
    adapter: AgentAdapter,
    questions: list[dict],
    ctx: RunContext | None = None,
    verbose: bool = True,
) -> tuple[list[Verdict], dict]:
    """跑一遍评测，返回 (逐题 verdict, 汇总)。"""
    ctx = ctx or RunContext()
    verdicts: list[Verdict] = []
    t0 = time.time()

    for i, q in enumerate(questions, 1):
        out = adapter.answer(q, ctx)
        v = score_question(q, out)
        verdicts.append(v)
        if verbose:
            flag = "  " if v.final >= 0.999 else ("~ " if v.final >= 0.5 else "!!")
            print(
                f"  {flag}{q['qid']:<7} final={v.final:.2f} "
                f"ans={v.scores.get('answer', 0):.2f} "
                f"hall={'Y' if v.hallucination else 'n'}"
            )

    agg = aggregate(verdicts)
    agg["adapter"] = adapter.name
    agg["elapsed_sec"] = round(time.time() - t0, 2)
    agg["n"] = len(verdicts)
    return verdicts, agg


def save(name: str, verdicts: list[Verdict], agg: dict) -> str:
    """落盘结果。文件名即 adapter 名。"""
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{name}.json")
    payload = {"adapter": name, "aggregate": agg, "verdicts": [v.to_dict() for v in verdicts]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def load_saved(name: str) -> dict:
    path = os.path.join(RESULTS, f"{name}.json")
    if not os.path.exists(path):
        raise SystemExit(f"还没有结果文件: {path}\n先跑: python eval/runner.py --adapter {name}")
    return json.load(open(path, encoding="utf-8"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="评测执行器")
    ap.add_argument("--adapter", help="单个 adapter 名（oracle / rule-baseline / null）")
    ap.add_argument("--compare", nargs="+", help="一次跑多个 adapter 并出对比表")
    ap.add_argument("--category", nargs="+", help="只跑指定类别")
    ap.add_argument("--qid", nargs="+", help="只跑指定题目")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    questions = load_questions(args.category, args.qid)
    print(f"题目数：{len(questions)}")

    names = args.compare or ([args.adapter] if args.adapter else ["oracle"])
    ctx = RunContext()
    summary: list[dict] = []

    try:
        for name in names:
            print(f"\n=== adapter: {name} ===")
            adapter = get_adapter(name)
            verdicts, agg = run(adapter, questions, ctx, verbose=not args.quiet)
            path = save(name, verdicts, agg)
            summary.append(agg)
            print(
                f"  → final={agg['final']:.3f}  answer_acc={agg['answer_acc']:.3f}  "
                f"result_acc={agg['result_acc']:.3f}  halluc={agg['hallucination_rate']:.2f}  "
                f"({agg['elapsed_sec']}s)"
            )
            print(f"  → 已保存 {path}")
    finally:
        ctx.close()

    if len(summary) > 1:
        from report import print_comparison

        print_comparison(summary)


if __name__ == "__main__":
    main()
