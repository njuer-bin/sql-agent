"""W4 对抗式 Verifier：独立于 planner 上下文，只看「答案 ↔ 工具输出」挑刺。

对应 PLAN.md W4「对抗式 Verifier（不共享 planner 上下文）」。四类检查：
  1. 数字溯源：答案里的数字必须能在工具输出里找到（编造数字 → 拦）
  2. 方向一致：答案声称的涨跌方向必须与实测数值一致（因果/方向倒置 → 拦）
  3. 有数据支撑：声称了数值结论但工具输出为空（无中生有 → 拦）
  4. 证据链完整：声明的验证步骤必须真的执行过（跳步 → 拦）

Verifiable 拦下时给出 issue 列表；pipeline 必须把 issue 写回答案而不是硬发。
"""

from __future__ import annotations

import json
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
        # 1) 数字溯源（单位感知：%/pp 数字不再跳过，按证据形态验证）
        corpus_nums = _nums(corpus)
        # corpus 侧数字的「证据形态」：是否直接带比率单位 / 是否带小数
        corpus_num_forms: list[tuple[float, bool]] = []   # (value, ratio_form)
        for m in _NUM.finditer(corpus):
            try:
                raw = m.group(0).replace(",", "")
                val = float(raw)
            except ValueError:
                continue
            formed = bool(_PCT_TAIL.match(corpus[m.end():])) or "." in raw
            corpus_num_forms.append((val, formed))
        # 差值证据池：corpus 数字两两差（支持「变化约 1.47 个百分点」）
        delta_pool = set()
        # 比率语境差值池：两个「带比率形态」的数字之差
        # （日期碎片（2026-09-01 → 2026/09/01）随意互差不可冒充比率证据）
        pct_delta_pool = set()
        formed_vals = [v for v, formed in corpus_num_forms if formed]
        for i, a in enumerate(formed_vals):
            for b in formed_vals[i + 1:]:
                pct_delta_pool.add(a - b)
                pct_delta_pool.add(b - a)
        for i, a in enumerate(corpus_nums):
            for b in corpus_nums[i + 1:]:
                delta_pool.add(a - b)
                delta_pool.add(b - a)
        for t in tool_outputs:
            # 比率证据：baseline 留痕里的 (current, previous) 可推导变化率
            try:
                b = (json.loads(t) or {}).get("baseline") or {}
            except (ValueError, TypeError):
                continue
            c, p = b.get("current"), b.get("previous")
            try:
                c_ll, p_ll = float(c), float(p)
            except (TypeError, ValueError):
                continue
            if p_ll:
                pct_delta_pool.add((c_ll - p_ll) / abs(p_ll) * 100.0)
        fabricated = []
        fabricated_pct = []
        for m in _NUM.finditer(answer):
            tail = _PCT_TAIL.match(answer[m.end():])
            try:
                v = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            if tail:
                # 带比率单位的答案数字只能匹配：
                #   a) corpus 中同样带单位/带小数的数字（整数日期碎片不可冒充）
                #   b) 比率语境差值/比率证据池
                ok = any(abs(v - c) <= 0.01 * max(1.0, abs(c))
                         for c, formed in corpus_num_forms if formed)
                if not ok and any(abs(v - d) <= 0.01 * max(1.0, abs(d))
                                  or abs(v - abs(d)) <= 0.01 * max(1.0, abs(d))
                                  for d in pct_delta_pool):
                    ok = True
                if not ok:
                    fabricated_pct.append(v)
            else:
                if not any(abs(v - d) <= 0.01 * max(1.0, abs(d))
                           for d in list(corpus_nums) + list(delta_pool)):
                    fabricated.append(v)
        if fabricated:
            issues.append(VerifierIssue(
                "number_traceability",
                f"答案中的数字无法在工具输出中溯源: {fabricated[:5]}"))
        if fabricated_pct:
            issues.append(VerifierIssue(
                "pct_traceability",
                f"答案中的百分比/百分点数字无法溯源（同值或差值证据均可）: "
                f"{fabricated_pct[:5]}"))

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

        # 4) 证据链完整（只对真实执行记录做断言，不认流水臆报）
        if required_steps:
            done = set(executed_steps or [])
            missing = [s for s in required_steps if s not in done]
            if missing:
                issues.append(VerifierIssue(
                    "evidence_chain",
                    f"声称执行但未实际执行的验证步骤: {missing}"))

        # 5) 时间窗口错配：结论首句声称的月份必须 ⊆ 留痕窗口
        #    （时间窗 + 基线窗）；跨月错配（问 7 月答 9 月事件）→ 拦
        allow: set[str] = set()
        for t in tool_outputs:
            try:
                d = json.loads(t)
            except (ValueError, TypeError):
                continue
            tw = d.get("time_window") or {}
            if tw.get("start"):
                allow.add(tw["start"][:7])
                allow.add(tw["end"][:7])
            b = d.get("baseline") or {}
            bw = b.get("baseline_window") or []
            for x in bw:
                if isinstance(x, str) and re.match(r"\d{4}-\d{2}", x):
                    allow.add(x[:7])
        if allow:
            head_sentence = answer.split("。", 1)[0]
            claimed = set(re.findall(r"\d{4}-\d{2}", head_sentence)) | set(
                re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月", head_sentence) and
                [f"{y}-{int(m):02d}" for y, m in
                 re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月", head_sentence)] or [])
            stray = claimed - allow
            if stray:
                issues.append(VerifierIssue(
                    "time_window_mismatch",
                    f"结论首句声称的时间窗 {sorted(stray)} 不在留痕窗口 {sorted(allow)} 内"))

        # 6) 归因必须有「具体事件」证据：提到根因/主因/事故/直接因果时，
        #    corpus 里必须存在与结论窗口月份匹配的 episodic 档案（ep-YYYY-MM）。
        #    程序记忆兜底（pr- playbook）只能证明「用了什么方法」，
        #    不能冒充「事件证据」；相关性语句不算因果结论。
        allow: set[str] = set()
        for t in tool_outputs:
            try:
                d = json.loads(t)
            except (ValueError, TypeError):
                continue
            tw = d.get("time_window") or {}
            if tw.get("start"):
                allow.add(tw["start"][:7])
                allow.add(tw["end"][:7])
            b = d.get("baseline") or {}
            bw = b.get("baseline_window") or []
            for x in bw:
                if isinstance(x, str) and re.match(r"\d{4}-\d{2}", x):
                    allow.add(x[:7])

        strong_claim = re.search(
            r"(根因|主因|事故原因|到底是.{0,6}导致的|证明了.{0,8}导致|"
            r"因果关系|归因于|由.{0,10}导致)", answer)
        not_defining = not re.search(
            r"(不下根因结论|无法确定归因|不能确定|不下结论)", answer)
        ep_evidence = False
        if strong_claim and not_defining:
            ep_ids = re.findall(r"ep-\d{4}-\d{2}", corpus)
            ep_months = {t[3:10] for t in ep_ids}
            ep_evidence = any(m in allow for m in ep_months)
            if not ep_evidence:
                issues.append(VerifierIssue(
                    "attribution_without_evidence",
                    "答案给出强归因结论，但工具输出中没有与本时间窗口月份"
                    "匹配的事件档案（ep-YYYY-MM）；程序/口径记忆不能冒充"
                    "事件证据"))

        # 7) 记忆版本冲突：答案坚持旧口径语义而 corpus 是新版口径 → 拦
        if re.search(r"不剔除退款用户|不用剔除", answer) and \
                "剔除退款" in corpus and "sem-repurchase-v1" not in corpus:
            issues.append(VerifierIssue(
                "memory_version_conflict",
                "答案使用「不剔除退款」的旧口径表述，但记忆证据是新版口径"
                "（剔除退款用户）——口径版本冲突"))

        return VerdictAdv(passed=not issues, issues=issues)
