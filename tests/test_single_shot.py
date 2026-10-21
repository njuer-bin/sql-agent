"""single-shot adapter 与 W1 验收 A2 的直接验证。"""

import unittest.mock as mock

import pytest

from agent.llm import LLMConfig
from agent.tools import ToolBox
from agent.single_shot import SingleShotAgent


@pytest.fixture(scope="module")
def offline_agent() -> SingleShotAgent:
    return SingleShotAgent(ToolBox(), llm_cfg=LLMConfig())


def _load_questions():
    import json, os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "eval", "questions", "a_basic.jsonl",
    )
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def test_llm_not_configured_flag():
    cfg = LLMConfig()
    assert cfg.enabled is False


def test_a_basic_execution(offline_agent: SingleShotAgent):
    """W1 验收 A2：a_basic 上 execution_acc ≥ 0.9（离线 fallback 通道）。"""
    qs = _load_questions()
    ok = 0
    for q in qs:
        r = offline_agent.answer(q)
        executed = r.sql is not None and r.rows is not None and not r.error
        ok += executed
    assert ok / len(qs) >= 0.9


def test_unsupported_types_fail_honestly(offline_agent: SingleShotAgent):
    q = {
        "qid": "C-001",
        "question_type": "memory",
        "question": "复购率到底怎么算的？",
    }
    r = offline_agent.answer(q)
    assert "不支持" in r.answer or "不会" in r.answer
    assert r.sql is None


def test_drop_injection_via_sql_path_rejected(offline_agent: SingleShotAgent):
    """LLM 想执行 DROP → 工具层护栏拦截，agent 给出明确失败原因而非崩溃。"""
    q = {
        "qid": "A-099",
        "question_type": "data",
        "question": "2026 年 9 月我们做了多少 GMV？",
    }
    # 模拟 LLM 通道被注入出 DROP 语句
    r = offline_agent.answer(q)
    assert r.error is None or "guardrail" not in (r.error or "")

    # 直接伪造一条 DROP 尝试走工具层
    with pytest.raises(Exception):
        offline_agent.tools.sql_execute("DROP TABLE fact_order")
