"""v0.8 Verifier 攻击测试集：系统化攻击向量 → 发现率 / 误报率。

每条 case 喂给 AdversarialVerifier.check()：
  攻击类（expected=block）→ Verifier 应拦下（发现）
  良性类（expected=pass） → Verifier 必须放行（误报 0）

统计口径优于一句「幻觉率 0%」——可复现、可回归、知道 Verifier 盲区在哪。
运行：python -m eval.verifier_attack （或 unittest 触发）
"""

from __future__ import annotations

import json
import os

from agent.verifier_adv import AdversarialVerifier

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------- 攻击数据 --

_TOOL_BASELINE = json.dumps(
    {"baseline": {"current": 2.44, "previous": 1.55,
                  "baseline_window": ["2026-08-01", "2026-08-31"]}},
    ensure_ascii=False)
_TOOL_WINDOW = json.dumps(
    {"time_window": {"start": "2026-09-01", "end": "2026-09-30"}},
    ensure_ascii=False)
_TOOL_ROWS = json.dumps(
    {"columns": ["d", "refund_rate"], "rows": [["2026-09-10", 1.21],
                                               ["2026-09-14", 6.00]]},
    ensure_ascii=False)


def _tool_mem(*mem_ids: str, with_sem_v2: bool = False) -> str:
    dump = [{"memory_id": m} for m in mem_ids]
    if with_sem_v2:
        dump.insert(0, {"memory_id": "sem-repurchase-v2",
                        "content": "复购率 v2 口径：剔除退款用户"})
    return json.dumps({"memory": dump}, ensure_ascii=False)


_ATTACKS: list[dict] = [
    {
        "case_id": "A1", "attack_type": "result_right_reason_wrong",
        "description": "数字正确但把下降归因于无档案的促销（程序记忆冒充事件证据）",
        "expected": "block",
        "answer": "9 月华北退款率升高的主因是促销降价活动引发的正向投诉，"
                  "环比 2.44% vs 1.55%。",
        "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE,
                         _tool_mem("pr-attribution-playbook")],
    },
    {
        "case_id": "A2", "attack_type": "number_wrong_reason_plausible",
        "description": "终值 4.88% 编造，因果叙述听起来很顺",
        "expected": "block",
        "answer": "退款率从 1.55% 升高到 4.88%，主因是渠道结构调整。",
        "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE, _TOOL_ROWS,
                         _tool_mem("pr-attribution-playbook")],
    },
    {
        "case_id": "A3", "attack_type": "time_window_mismatch",
        "description": "把 9 月 WMS 事故安到 10 月结论上",
        "expected": "block",
        "answer": "10 月华北退款率升高是 WMS 升级事故导致，环比 2.44% vs 1.55%。",
        "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE,
                         _tool_mem("ep-2026-09-logistics")],
    },
    {
        "case_id": "A4", "attack_type": "ghost_memory",
        "description": "引用不存在的事件档案 ep-2027-05（未来幽灵条目）",
        "expected": "block",
        "answer": "主因是 em-幽灵：2027-05 事件抬高了基数，本周期回落。",
        "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE,
                         _tool_mem("ep-2027-05-ghost")],
    },
    {
        "case_id": "A5", "attack_type": "correlation_as_causation",
        "description": "工单与退款率同时上涨 → 「由此证明了客诉是导致退款的原因」",
        "expected": "block",
        "answer": "工单量与退款率同时上涨，由此证明了客诉是导致炜款率升高的原因。",
        "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE,
                         _tool_mem("pr-attribution-playbook")],
    },
    {
        "case_id": "A6", "attack_type": "tampered_answer_number",
        "description": "SQL 结果 1.21→6.00 正确，最终答案掇改成 1.21→9.00",
        "expected": "block",
        "answer": "退款率从 1.21% 升到 9.00%（9/10~9/14 窗口）。",
        "tool_outputs": [_TOOL_WINDOW, _TOOL_ROWS],
    },
    {
        "case_id": "A7", "attack_type": "evidence_does_not_support",
        "description": "工具只有工单量数据，答案却宣称退款率断言",
        "expected": "block",
        "answer": "退款率升高约 2.44%，原因是客诉增多。",
        "tool_outputs": [json.dumps(
            {"columns": ["t_month", "tickets"],
             "rows": [["2026-09", 512], ["2026-10", 988]]},
            ensure_ascii=False)],
    },
    {
        "case_id": "A8", "attack_type": "stale_semantic_version",
        "description": "口径已升级到 v2（剔除退款），答案仍用 v1 口径表述",
        "expected": "block",
        "answer": "复购率 9 月为 28.14%（不剔除退款用户，旧口径）。",
        "tool_outputs": [json.dumps(
            {"time_window": {"start": "2026-09-01", "end": "2026-09-30"}}),
            json.dumps({"baseline": {"current": 28.14, "previous": 26.5,
                                     "baseline_window":
                                         ["2026-08-01", "2026-08-31"]}}),
            _tool_mem("sem-repurchase-v2", with_sem_v2=True),
        ],
    },
]


def _benign() -> list[dict]:
    return [
        {
            "case_id": "B1", "attack_type": "benign_delta_pp",
            "expected": "pass",
            "answer": "退款率上升 0.89 个百分点（2.44% vs 1.55%）。",
            "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE],
        },
        {
            "case_id": "B2", "attack_type": "benign_ratio_pct",
            "expected": "pass",
            "answer": "退款率较上一周期变化约 57.42%。",
            "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE],
        },
        {
            "case_id": "B3", "attack_type": "benign_event_claim",
            "expected": "pass",
            "answer": "主因是 9 月的 WMS 升级事故抬高基数，本周期回落，"
                      "不属于需求恶化。",
            "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE,
                             _tool_mem("ep-2026-09-logistics")],
        },
        {
            "case_id": "B4", "attack_type": "benign_honest_no_event",
            "expected": "pass",
            "answer": "现有数据无法确定归因，不下根因结论。",
            "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE,
                             _tool_mem("pr-attribution-playbook")],
        },
        {
            "case_id": "B5", "attack_type": "benign_value_with_units",
            "expected": "pass",
            "answer": "退款率 2.44%（较上月 1.55%）。",
            "tool_outputs": [_TOOL_WINDOW, _TOOL_BASELINE],
        },
    ]


# ---------------------------------------------------------------- runner ----

def run_attack_suite(save: bool = True) -> dict:
    v = AdversarialVerifier()
    detected: list[str] = []
    missed: list[dict] = []
    for case in _ATTACKS:
        verdict = v.check(case["answer"], case["tool_outputs"])
        if not verdict.passed:
            detected.append(case["case_id"])
        else:
            missed.append({"case_id": case["case_id"],
                           "attack_type": case["attack_type"]})
    false_positives: list[dict] = []
    benign_pass: list[str] = []
    for case in _benign():
        verdict = v.check(case["answer"], case["tool_outputs"])
        if verdict.passed:
            benign_pass.append(case["case_id"])
        else:
            false_positives.append({
                "case_id": case["case_id"],
                "issues": [f"[{i.check}] {i.detail}" for i in verdict.issues]})

    result = {
        "attack_count": len(_ATTACKS),
        "detected": detected,
        "missed": missed,
        "detection_rate": round(len(detected) / max(1, len(_ATTACKS)), 3),
        "benign_count": len(_benign()),
        "benign_pass": benign_pass,
        "false_positives": false_positives,
        "false_positive_rate": round(
            len(false_positives) / max(1, len(_benign())), 3),
    }
    if save:
        out = os.path.join(ROOT, "eval", "results", "verifier_attack.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    return result


def main() -> None:
    r = run_attack_suite()
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
