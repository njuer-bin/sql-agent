"""
结果报告生成器（Report）
===========================================================================
把 runner 落盘的 JSON 渲染成人能读的对比表。

这是**消融实验的主武器**：面试时你要能指着这张表说
"加记忆模块后，口径类问题准确率从 X 提到 Y"。

用法：
    python eval/report.py                          # 用已有结果出对比表
    python eval/report.py oracle rule-baseline     # 指定要对比的组
    python eval/report.py --detail oracle          # 逐题明细
    python eval/report.py --md out.md              # 导出 Markdown
===========================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from runner import RESULTS, load_saved  # noqa: E402

# 展示顺序：对照组在前，越往后能力越强
_ORDER = ["null", "rule-baseline", "oracle"]


# --------------------------------------------------------------------------
# 表格工具
# --------------------------------------------------------------------------

def _row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    out = [_row(headers, widths), _row(["-" * w for w in widths], widths)]
    out += [_row(r, widths) for r in rows]
    return "\n".join(out)


# --------------------------------------------------------------------------
# 对比表
# --------------------------------------------------------------------------

def print_comparison(summary: list[dict]) -> None:
    """打印多组 adapter 的横向对比表。"""
    def key(s: dict) -> int:
        return _ORDER.index(s["adapter"]) if s["adapter"] in _ORDER else 99

    rows = sorted(summary, key=key)
    headers = ["adapter", "n", "final", "execution", "result", "answer",
               "retrieval", "halluc", "sec"]
    body = []
    for s in rows:
        body.append([
            s["adapter"],
            str(s["n"]),
            f"{s['final']:.3f}",
            f"{s['execution_acc']:.3f}",
            f"{s['result_acc']:.3f}",
            f"{s['answer_acc']:.3f}",
            f"{s['retrieval_acc']:.3f}",
            f"{s['hallucination_rate']:.3f}",
            f"{s['elapsed_sec']:.1f}",
        ])
    print("\n" + "=" * 78)
    print("消融对比（Ablation）")
    print("=" * 78)
    print(_table(headers, body))

    # 按类别拆一行 —— 记忆类题目的提升最明显，单独看
    print("\n按类别（final / answer_acc）")
    cats = sorted({c for s in rows for c in (s.get("by_category") or {})})
    headers2 = ["category", "n"] + [s["adapter"] for s in rows]
    body2 = []
    for c in cats:
        line = [c]
        n = 0
        for s in rows:
            cc = (s.get("by_category") or {}).get(c)
            if cc:
                n = cc["n"]
                line.append(f"{cc['final']:.2f} / {cc['answer_acc']:.2f}")
            else:
                line.append("-")
        line.insert(1, str(n))
        body2.append(line)
    print(_table(headers2, body2))


# --------------------------------------------------------------------------
# 单组报告
# --------------------------------------------------------------------------

def markdown_report(name: str, payload: dict, detail: bool = False) -> str:
    """生成一份 Markdown 报告。"""
    agg = payload["aggregate"]
    verdicts = payload["verdicts"]
    L: list[str] = []

    L.append(f"# 评测报告 — adapter: `{name}`\n")
    L.append(f"- 题目数：**{agg['n']}**")
    L.append(f"- 加权总分 final：**{agg['final']:.3f}**")
    L.append(f"- 结论正确率 answer_acc：**{agg['answer_acc']:.3f}**")
    L.append(f"- 结果正确率 result_acc：**{agg['result_acc']:.3f}**")
    L.append(f"- SQL 可执行率 execution_acc：**{agg['execution_acc']:.3f}**")
    L.append(f"- 记忆检索命中率 retrieval_acc：**{agg['retrieval_acc']:.3f}**")
    L.append(f"- 幻觉率 hallucination_rate：**{agg['hallucination_rate']:.3f}**")
    L.append(f"- 耗时：{agg['elapsed_sec']}s\n")

    if "attribution_answer_acc" in agg:
        L.append(f"- 归因题 answer_acc：**{agg['attribution_answer_acc']:.3f}**")
        L.append(f"- 归因题幻觉率：**{agg['attribution_hallucination_rate']:.3f}**\n")
    if "memory_answer_acc" in agg:
        L.append(f"- 记忆题 answer_acc：**{agg['memory_answer_acc']:.3f}**")
        L.append(f"- 记忆题 retrieval_acc：**{agg['memory_retrieval_acc']:.3f}**\n")

    L.append("## 分类别\n")
    L.append("| category | n | final | answer_acc | retrieval_acc | halluc |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for cat, cc in sorted((agg.get("by_category") or {}).items()):
        L.append(
            f"| {cat} | {cc['n']} | {cc['final']:.3f} | {cc['answer_acc']:.3f} "
            f"| {cc['retrieval_acc']:.3f} | {cc['hallucination_rate']:.3f} |"
        )

    if detail:
        L.append("\n## 逐题明细\n")
        L.append("| qid | category | type | difficulty | final | answer | result | halluc |")
        L.append("|---|---|---|---|---:|---:|---:|---|")
        for v in verdicts:
            L.append(
                f"| {v['qid']} | {v['category']} | {v['question_type']} "
                f"| {v['difficulty']} | {v['final']:.2f} "
                f"| {v['scores'].get('answer', 0):.2f} "
                f"| {v['scores'].get('result', 0):.2f} "
                f"| {'Y' if v['hallucination'] else ''} |"
            )
        bad = [v for v in verdicts if v["hallucination"]]
        if bad:
            L.append(f"\n### 幻觉明细（{len(bad)} 题）\n")
            for v in bad:
                L.append(f"- **{v['qid']}** 编造数字：{', '.join(v['hallucinated_numbers'][:8])}")

    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="结果报告")
    ap.add_argument("names", nargs="*", help="要报告的 adapter 名")
    ap.add_argument("--detail", action="store_true", help="输出逐题明细")
    ap.add_argument("--md", help="把 Markdown 报告写到这个文件")
    args = ap.parse_args()

    names = args.names or sorted(
        os.path.splitext(f)[0] for f in os.listdir(RESULTS) if f.endswith(".json")
    )
    payloads = [load_saved(n) for n in names]

    if len(payloads) > 1:
        print_comparison([p["aggregate"] for p in payloads])

    for p in payloads:
        text = markdown_report(p["aggregate"]["adapter"], p, detail=args.detail)
        print(text)
        if args.md:
            path = args.md if len(payloads) == 1 else f"{args.md[:-3]}-{p['aggregate']['adapter']}.md"
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"→ 已写出 {path}")


if __name__ == "__main__":
    main()
