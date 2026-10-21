"""W4 对抗式 Verifier：独立于 planner 上下文，只看「答案 ↔ 工具输出」挑刺。

对应 PLAN.md W4「对抗式 Verifier（不共享 planner 上下文）」。四类检查：
  1. 数字溯源：答案里的数字必须能在工具输出里找到（编造数字 → 拦）
  2. 方向一致：答案声称的涨跌方向必须与实测数值一致（因果/方向倒置 → 拦）
  3. 有数据支撑：声称了数值结论但工具输出为空（无中生有 → 拦）
  4. 证据链完整：声明的验证步骤必须真的执行过（跳步 → 拦）

Verifiable 拦下时给出 issue 列表；pipeline 必须把 issue 写回答案而不是硬发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_PCT_TAIL = re.compile(r"^\s*(?:%|个百分点|pp\b)")


def _nums(text: str, skip_pct: bool = False) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        if skip_pct and _PCT_TAIL.match(text[m.end():]):
            continue
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


_UP_WORDS = ["上涨", "暴涨", "升高", "提高", "增长", "增加", "上升", "翻倍"]
_DOWN_WORDS = ["下降", "暴跌", "降低", "减少", "下滑", "回落", "跌破"]


@dataclass
class VerifierIssue:
    check: str
    detail: str


@dataclass
class VerdictAdv:
    passed: bool
    issues: list[VerifierIssue] = field(default_factory=list)


class AdversarialVerifier:
    """不读 planner 上下文：只拿 (答案, 工具输出, 实测指标方向, 已执行步骤) 做审查。"""

    def check(self, answer: str, tool_outputs: list[str],
              measured: dict[str, float] | None = None,
              executed_steps: list[str] | None = None,
              required_steps: list[str] | None = None) -> VerdictAdv:
        measured = measured or {}
        issues: list[VerifierIssue] = []

        corpus = " ".join(tool_outputs)
        # 1) 数字溯源（答案里紧跟 %/pp 的数字是比率，跨单位比对意义不大，跳过）
        corpus_nums = _nums(corpus)
        fabricated = []
        for v in _nums(answer, skip_pct=True):
            if not any(abs(v - c) <= 0.01 * max(1.0, abs(c)) for c in corpus_nums):
                fabricated.append(v)
        if fabricated:
            issues.append(VerifierIssue(
                "number_traceability",
                f"答案中的数字无法在工具输出中溯源: {fabricated[:5]}"))

        # 2) 方向一致（答案方向词 vs 实测数值）
        for metric, delta in measured.items():
            if metric not in answer:
                continue
            claims_up = any(w in answer for w in _UP_WORDS)
            claims_down = any(w in answer for w in _DOWN_WORDS)
            if delta > 0 and claims_down and not claims_up:
                issues.append(VerifierIssue(
                    "direction_consistency",
                    f"答案称下降但实测 {metric} 上升（Δ={delta}），方向倒置"))
            elif delta < 0 and claims_up and not claims_down:
                issues.append(VerifierIssue(
                    "direction_consistency",
                    f"答案称上升但实测 {metric} 下降（Δ={delta}），方向倒置"))

        # 3) 有数据支撑：答案里有数字断言，但工具输出里连一个数字都没有
        if not corpus_nums and _nums(answer, skip_pct=True):
            issues.append(VerifierIssue(
                "data_support",
                "答案包含数值结论但没有任何非空工具输出支撑"))

        # 4) 证据链完整
        if required_steps:
            done = set(executed_steps or [])
            missing = [s for s in required_steps if s not in done]
            if missing:
                issues.append(VerifierIssue(
                    "evidence_chain",
                    f"声称执行但未实际执行的验证步骤: {missing}"))

        return VerdictAdv(passed=not issues, issues=issues)
