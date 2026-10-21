"""
评分器（Verifier）
============================================================================
把 agent 的实际输出与 gold 答案比对，输出可写进简历的分项指标。

设计原则：
  1. **分层给分** —— 执行/结构/结果/结论 四层独立，避免"全对或全错"
  2. **幻觉单独检测** —— 答案里的数字必须能在工具输出里找到出处
  3. **记忆归因** —— 记忆题单独算检索命中率，用于证明记忆模块的价值
  4. **权重可按题型配置** —— 归因题更看重证据链，口径题更看重结论

指标口径（简历用语）：
  execution_acc  SQL 可执行率
  result_acc     结果正确率（含部分分）
  answer_acc     结论正确率
  hallucination  幻觉率（编造数字的比例）
  memory_hit     记忆检索命中率
  final          加权总分
============================================================================
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------- 常量 ---

# 数字提取：支持 1,234.56 / 12.3万 / 1.2亿 / 30% / 30
NUM_RE = re.compile(
    r"(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(万|亿|%|个百分点|pp)?"
)

UNIT_FACTOR = {"万": 1e4, "亿": 1e8, "%": 0.01, None: 1.0, "个百分点": 1.0, "pp": 1.0}


def _to_float(text: str) -> float | None:
    """把 '1,234.56' / '12.3万' / '30%' 归一化成 float。"""
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_numbers(text: str) -> list[tuple[float, str | None]]:
    """抽出文本里所有数字及其单位，归一化到基本单位。"""
    out = []
    if not text:
        return out
    for m in NUM_RE.finditer(str(text)):
        raw, unit = m.group(1), m.group(2)
        v = _to_float(raw)
        if v is None:
            continue
        v *= UNIT_FACTOR.get(unit, 1.0)
        out.append((v, unit))
    return out


def _cell_eq(a, b, tol: float) -> bool:
    """单元格相等判断：数值用容差，字符串忽略大小写与空白。"""
    fa, fb = _to_float(a), _to_float(b)
    if fa is not None and fb is not None:
        if abs(fa) < 1e-9 and abs(fb) < 1e-9:
            return True
        return abs(fa - fb) <= tol * max(1.0, abs(fb))
    sa, sb = str(a).strip().lower(), str(b).strip().lower()
    return sa == sb


# ---------------------------------------------------------------- 结构 ---

@dataclass
class AgentOutput:
    """agent 的一次回答。由 adapters.py 负责填充。"""
    answer: str = ""
    sql: str | None = None
    columns: list[str] | None = None
    result: list[list[Any]] | None = None
    execution_error: str | None = None
    retrieved_memory_ids: list[str] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0
    latency_ms: int = 0


@dataclass
class Verdict:
    qid: str
    category: str
    question_type: str
    difficulty: str
    scores: dict[str, float] = field(default_factory=dict)
    final: float = 0.0
    hallucination: bool = False
    hallucinated_numbers: list[str] = field(default_factory=list)
    memory_hit: float = 0.0
    matched_memory_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 评分 ---

def score_execution(out: AgentOutput) -> float:
    if out.execution_error:
        return 0.0
    if out.sql and out.result is None:
        return 0.0
    return 1.0 if out.sql else 0.0


def score_schema(expected: dict, out: AgentOutput) -> float:
    exp_cols = expected.get("columns")
    if not exp_cols or not out.columns:
        return 0.0
    exp_norm = {c.strip().lower() for c in exp_cols}
    act_norm = {c.strip().lower() for c in out.columns}
    if exp_norm <= act_norm:
        return 1.0
    hit = len(exp_norm & act_norm) / len(exp_norm)
    return hit


def score_result(expected: dict, out: AgentOutput, tol: float | None = None) -> float:
    """逐单元格比对，给出 0~1 的部分分。行序无关，除非 expected['ordered']。"""
    exp_rows = expected.get("result")
    act_rows = out.result
    if exp_rows is None:
        return 0.0
    if not exp_rows:
        return 1.0 if not act_rows else 0.0
    if not act_rows:
        return 0.0

    tol = tol if tol is not None else expected.get("tolerance", 0.01)
    ordered = expected.get("ordered", False)

    if ordered:
        n = min(len(exp_rows), len(act_rows))
        if n == 0:
            return 0.0
        per_row = []
        for i in range(n):
            per_row.append(_row_score(exp_rows[i], act_rows[i], tol))
        cover = n / max(len(exp_rows), len(act_rows))
        return (sum(per_row) / len(per_row)) * cover

    # 无序：对每个期望行找最匹配的实际行
    used = [False] * len(act_rows)
    total = 0.0
    for er in exp_rows:
        best, best_j = 0.0, -1
        for j, ar in enumerate(act_rows):
            if used[j]:
                continue
            s = _row_score(er, ar, tol)
            if s > best:
                best, best_j = s, j
        if best_j >= 0:
            used[best_j] = True
        total += best
    return total / len(exp_rows)


def _row_score(exp_row, act_row, tol: float) -> float:
    if len(exp_row) != len(act_row):
        # 列数不同：按最短的算，并惩罚
        n = min(len(exp_row), len(act_row))
        if n == 0:
            return 0.0
        hits = sum(1 for i in range(n) if _cell_eq(exp_row[i], act_row[i], tol))
        return (hits / max(len(exp_row), len(act_row))) * 0.5
    if not exp_row:
        return 1.0
    hits = sum(1 for a, b in zip(exp_row, act_row) if _cell_eq(a, b, tol))
    return hits / len(exp_row)


# 否定前缀：答案里出现「不是X」不等于「肯定了 X」，禁止项检查必须先判断否定语境。
# 例：题目要求必须说「不是老客流失」，若禁止项是「是老客流失」，
#     朴素子串匹配会把正确答案判成违规 —— 这是评测集里真实踩过的坑。
#     注意「不是」与「是老客流失」共享一个「是」字，所以不能简单做
#     「否定词 + 禁止项」的字符串替换，必须看匹配位置前的字符。
NEGATIONS = ("不是", "并非", "并不是", "绝非", "不一定", "没有")


def _is_negated(text: str, start: int) -> bool:
    """text[start:] 处的内容是否处在否定语境（前面紧邻否定词）。"""
    prefix = text[:start]
    return prefix.endswith("不") or any(prefix.endswith(n) for n in NEGATIONS)


def has_affirmative(text: str, key: str) -> bool:
    """
    判断 text 里是否存在**肯定语境**下的 key。
    返回 True 表示"确实断言了 key"；False 表示没出现，或只在否定句里出现。
    """
    low = str(text).lower()
    ks = str(key).lower()
    i = 0
    while True:
        j = low.find(ks, i)
        if j < 0:
            return False
        if not _is_negated(low, j):
            return True
        i = j + len(ks)


def score_answer(expected: dict, answer: str) -> float:
    """结论正确率：必需事实覆盖率 − 禁止项惩罚。"""
    required = expected.get("answer_contains") or []
    forbidden = expected.get("answer_forbidden") or []
    # 没答就是没答 —— 不能因为"这题没设答案关键词"就送分
    if not answer:
        return 0.0
    if not required and not forbidden:
        return 1.0
    low = str(answer).lower()

    if required:
        hit = sum(1 for k in required if str(k).lower() in low)
        cov = hit / len(required)
    else:
        cov = 1.0

    penalty = 0.0
    for k in forbidden:
        if has_affirmative(low, k):
            penalty += 0.5
    return max(0.0, min(1.0, cov - penalty))


def score_must_mention(expected: dict, answer: str) -> float:
    keys = expected.get("must_mention") or []
    if not keys:
        return 1.0
    low = str(answer).lower()
    hit = sum(1 for k in keys if str(k).lower() in low)
    return hit / len(keys)


def detect_hallucination(answer: str, tool_outputs: list[str]) -> tuple[bool, list[str]]:
    """
    幻觉检测：答案里的数字必须能在工具输出中找到出处。
    返回 (是否幻觉, 编造的数字列表)。
    """
    if not answer:
        return False, []
    corpus = " ".join(str(t) for t in tool_outputs) if tool_outputs else ""
    if not corpus:
        # 没有工具输出 → 无法验证，保守判为幻觉（除纯记忆题外由调用方决定）
        return True, [str(v) for v, _ in extract_numbers(answer)]

    corpus_nums = [v for v, _ in extract_numbers(corpus)]
    ans_nums = extract_numbers(answer)

    bad = []
    for v, unit in ans_nums:
        # 百分比和百分点不做跨单位比对，跳过
        if unit in ("%", "个百分点", "pp"):
            continue
        if not any(abs(v - c) <= 0.01 * max(1.0, abs(c)) for c in corpus_nums):
            bad.append(f"{v:g}")
    return (len(bad) > 0), bad


def score_evidence(expected: dict, evidence: list[str]) -> float:
    req = expected.get("evidence_required") or []
    if not req:
        return 1.0
    blob = " ".join(str(e) for e in evidence).lower()
    if not blob:
        return 0.0
    hit = sum(1 for k in req if str(k).lower() in blob)
    return hit / len(req)


def score_memory(question: dict, out: AgentOutput) -> tuple[float, list[str]]:
    """
    记忆命中率：召回的期望记忆条数 / 期望条数（Recall 口径，因为漏召回比多召回更致命）。
    期望记忆从题目的 note / memory_id 推导。
    """
    exp_ids = question.get("expected", {}).get("memory_ids") or []
    if not exp_ids:
        return 0.0, []
    got = set(out.retrieved_memory_ids or [])
    hit = [m for m in exp_ids if m in got]
    return len(hit) / len(exp_ids), hit


# ---------------------------------------------------------------- 主入口 ---

def score_question(question: dict, out: AgentOutput) -> Verdict:
    exp = question.get("expected", {})
    weights = (question.get("scoring") or {}).get("weights") or {}
    qtype = question.get("question_type", "data")

    scores: dict[str, float] = {}

    if qtype in ("data", "attribution"):
        scores["execution"] = score_execution(out)
        scores["schema"] = score_schema(exp, out) if qtype == "data" else 1.0
        scores["result"] = score_result(exp, out)
        scores["answer"] = score_answer(exp, out.answer) * 0.6 \
            + score_must_mention(exp, out.answer) * 0.4
        if qtype == "attribution":
            scores["evidence"] = score_evidence(exp, out.evidence)
    else:  # memory
        m_hit, m_ids = score_memory(question, out)
        scores["retrieval"] = m_hit
        scores["answer"] = score_answer(exp, out.answer) * 0.5 \
            + score_must_mention(exp, out.answer) * 0.5
        scores["execution"] = 1.0

    # 幻觉：仅对 data / attribution 题检测（memory 题答案本就不含数字）
    hall, hall_nums = (False, [])
    if qtype in ("data", "attribution"):
        hall, hall_nums = detect_hallucination(out.answer, out.tool_outputs)

    final = 0.0
    wsum = 0.0
    for k, w in weights.items():
        if k in scores:
            final += w * scores[k]
            wsum += w
    if wsum > 0:
        final /= wsum
    # 幻觉硬惩罚
    if hall:
        final *= 0.5

    return Verdict(
        qid=question["qid"],
        category=question["category"],
        question_type=qtype,
        difficulty=question.get("difficulty", "medium"),
        scores=scores,
        final=round(final, 4),
        hallucination=hall,
        hallucinated_numbers=hall_nums,
        memory_hit=round(scores.get("retrieval", 0.0), 4),
        matched_memory_ids=score_memory(question, out)[1],
        notes=[],
    )


# ---------------------------------------------------------------- 汇总 ---

def aggregate(verdicts: list[Verdict]) -> dict:
    """
    按类别汇总，产出对比表需要的所有数字。

    **注意子集口径**：不同分项只在"有意义"的题目子集上求均值，否则会被
    没定义该分项的题型稀释成假数字。例如：
        result_acc 只在 data / attribution 上算（memory 题根本不产 SQL 结果）
        retrieval_acc 只在 memory 上算（其余题没有"期望记忆"这个概念）
    """
    if not verdicts:
        return {}

    def _mean(xs):
        xs = list(xs)
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    data_qs = [v for v in verdicts if v.question_type in ("data", "attribution")]
    schema_qs = [v for v in verdicts if v.question_type == "data"]
    attr_qs = [v for v in verdicts if v.question_type == "attribution"]
    mem_qs = [v for v in verdicts if v.question_type == "memory"]

    overall = {
        "n": len(verdicts),
        "final": _mean(v.final for v in verdicts),
        "hallucination_rate": _mean(1.0 if v.hallucination else 0.0 for v in verdicts),
        "execution_acc": _mean(v.scores.get("execution", 0.0) for v in data_qs),
        "schema_acc": _mean(v.scores.get("schema", 0.0) for v in schema_qs),
        "result_acc": _mean(v.scores.get("result", 0.0) for v in data_qs),
        "answer_acc": _mean(v.scores.get("answer", 0.0) for v in verdicts),
        "retrieval_acc": _mean(v.memory_hit for v in mem_qs),
        "evidence_acc": _mean(v.scores.get("evidence", 0.0) for v in attr_qs),
        "n_data": len(data_qs),
        "n_memory": len(mem_qs),
    }

    by_cat = {}
    for v in verdicts:
        by_cat.setdefault(v.category, []).append(v)

    def _cat_block(vs: list[Verdict]) -> dict:
        d = [v for v in vs if v.question_type in ("data", "attribution")]
        m = [v for v in vs if v.question_type == "memory"]
        return {
            "n": len(vs),
            "final": _mean(v.final for v in vs),
            "hallucination_rate": _mean(1.0 if v.hallucination else 0.0 for v in vs),
            "answer_acc": _mean(v.scores.get("answer", 0.0) for v in vs),
            "result_acc": _mean(v.scores.get("result", 0.0) for v in d),
            "retrieval_acc": _mean(v.memory_hit for v in m),
        }

    overall["by_category"] = {cat: _cat_block(vs) for cat, vs in by_cat.items()}

    # 归因题单独看幻觉率 —— 这是最体现记忆价值的指标
    if attr_qs:
        overall["attribution_hallucination_rate"] = _mean(
            1.0 if v.hallucination else 0.0 for v in attr_qs)
        overall["attribution_answer_acc"] = _mean(v.scores.get("answer", 0.0) for v in attr_qs)
        overall["attribution_result_acc"] = _mean(v.scores.get("result", 0.0) for v in attr_qs)

    if mem_qs:
        overall["memory_answer_acc"] = _mean(v.scores.get("answer", 0.0) for v in mem_qs)
        overall["memory_retrieval_acc"] = _mean(v.memory_hit for v in mem_qs)

    return overall
