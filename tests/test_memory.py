"""W3 验收：记忆检索（对应 PLAN.md W3 验收 A1–A4）。

A1 四类记忆题（c_metric/d_episode/f_conflict/h_long_horizon + E-004）answer_acc ≥ 0.7
A2 口径对齐：需要计算的记忆题按当前生效口径（复购率 v2 剔除退款）执行
A3 检索命中 recall ≥ 0.8（retrieval_acc 分项）
A4 检索步数计入 steps，可对比
"""

from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.llm import LLMConfig  # noqa: E402
from agent.memory import MemoryStore  # noqa: E402
from agent.memory_agent import MemoryAgent  # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from eval.verifier import detect_hallucination, score_memory  # noqa: E402

MEMORY_QFILES = ["c_metric", "d_episode", "f_conflict", "h_long_horizon", "e_procedure"]


def _load_memory_questions() -> list[dict]:
    qs = []
    for f in MEMORY_QFILES:
        for line in open(os.path.join(ROOT, "eval", "questions", f"{f}.jsonl"),
                         encoding="utf-8"):
            if line.strip():
                q = json.loads(line)
                if q.get("question_type") == "memory":
                    qs.append(q)
    return qs


@pytest.fixture(scope="module")
def agent():
    tb = ToolBox()
    a = MemoryAgent(tb, llm_cfg=LLMConfig())
    yield a
    tb.close()


def test_store_loads_19_entries():
    s = MemoryStore()
    assert len(s.entries) == 19
    types = {e["memory_type"] for e in s.entries}
    assert types == {"semantic", "episodic", "procedural"}


def test_a3_retrieval_recall_at_least_80pct(agent):
    """A3：21 道记忆题的检索命中率（期望 memory_ids 的 recall）≥ 0.8。"""
    qs = _load_memory_questions()
    recalls = []
    for q in qs:
        r = agent.answer(q)
        rec, _ = score_memory(q, _as_output(r))
        recalls.append(rec)
    mean = sum(recalls) / len(recalls)
    assert mean >= 0.8, f"检索命中率 {mean:.3f} < 0.8"


def test_a1_answer_acc_at_least_70pct(agent):
    """A1：answer_acc = answer_contains 关键词覆盖率，四类记忆题均值 ≥ 0.7。"""
    qs = _load_memory_questions()
    accs = []
    for q in qs:
        r = agent.answer(q)
        keys = q.get("expected", {}).get("answer_contains") or []
        if not keys:
            accs.append(1.0)
            continue
        hit = sum(1 for k in keys if k in r.answer)
        accs.append(hit / len(keys))
    mean = sum(accs) / len(accs)
    assert mean >= 0.7, f"answer_acc {mean:.3f} < 0.7（明细：{list(zip([q['qid'] for q in qs], accs))}）"


def test_a2_memory_aligned_sql_executes_v2(agent):
    """A2：需要计算的记忆题按当前生效口径执行（复购率 v2：剔除退款用户）。

    E-004「9 月的复购率，用现在该用的口径」→ SQL 必须包含退款剔除逻辑
    （per_user 子查询里 SUM(refund_amount)，外层 WHERE ref=0），且能跑通。
    """
    q = next(q for q in _load_memory_questions() if q["qid"] == "E-004")
    r = agent.answer(q)
    assert r.sql is not None, "E-004 应按记忆口径生成 SQL"
    assert "ref" in r.sql and "= 0" in r.sql.replace("ref=0", "ref = 0"), \
        f"复购率 SQL 未体现 v2 剔除退款口径: {r.sql}"
    assert r.rows and not all(all(v in (0, 0.0, None, "") for v in row) for row in r.rows)


def test_a4_steps_count_retrieval(agent):
    """A4：检索计 1 步；带取数的记忆题计 检索+执行 步数。"""
    r1 = agent.answer({"qid": "C-002", "question": "客单价的分母到底是订单数还是下单用户数？",
                       "question_type": "memory"})
    assert r1.steps == 1
    q4 = next(q for q in _load_memory_questions() if q["qid"] == "E-004")
    r2 = agent.answer(q4)
    assert r2.steps >= 2


def test_memory_answers_are_traceable_no_hallucination(agent):
    """记忆引用必须作为工具输出留痕；答案数字必须可溯源（防幻觉误判）。"""
    for q in _load_memory_questions():
        r = agent.answer(q)
        assert r.tool_outputs, f"{q['qid']} 无工具输出留痕"
        # 记忆题不走 SQL 时不得有 execution_error 之外的问题
        hall, nums = detect_hallucination(r.answer, r.tool_outputs)
        assert not hall, f"{q['qid']} 幻觉数字: {nums[:5]}"


def test_memory_miss_refuses_honestly(agent):
    """检索不到 → 诚实说不，不编造。"""
    r = agent.answer({"qid": "X-TEST", "question": "隔壁组 III 类产品的库存周转率是多少？",
                      "question_type": "memory"})
    assert r.error == "memory_miss"
    assert "不会瞎猜" in r.answer


def test_data_questions_unchanged_vs_error_fix(agent):
    """W3 不改变 data/attribution 行为（与 W2 error-fix 同路径）。"""
    r = agent.answer({"qid": "A-001", "question": "2026 年 9 月我们做了多少 GMV？",
                      "question_type": "data"})
    assert r.sql is not None and r.rows


# ---------------------------------------------------------------- helper

def _as_output(r):
    """把 SingleShotResult 适配成 score_memory 需要的最小接口。"""
    class _O:
        pass

    o = _O()
    o.retrieved_memory_ids = list(r.retrieved or [])
    return o
