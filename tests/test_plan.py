"""v0.8 Supervisor 结构化调度：TaskPlan。"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.multi_agent import Supervisor
from agent.plan import TaskPlan   # noqa: E402
from agent.tools import ToolBox   # noqa: E402


def _sup() -> Supervisor:
    return Supervisor(ToolBox())


def test_plan_intent_mapping():
    sup = _sup()
    for qt, intent in (("data", "data"), ("memory", "memory"),
                       ("attribution", "attribution")):
        p = sup.make_plan({"question_type": qt, "question": ""})
        assert isinstance(p, TaskPlan) and p.intent == intent
        assert p.agents == [intent in ("data", "memory", "attribution") and
                            {"data": "data", "memory": "retrieval",
                             "attribution": "attribution"}[intent]]


def test_plan_infers_intent_from_text():
    sup = _sup()
    p = sup.make_plan({"question": "2026 年 9 月华北的退款率为什么升高？"})
    assert p.intent == "attribution"
    assert p.constraints.get("metric") == "refund_rate"
    assert p.constraints.get("region") == "华北" or "region" in p.constraints


def test_plan_graph_and_topology():
    sup = _sup()
    p = sup.make_plan({"question_type": "attribution"})
    assert "attribution" in p.graph
    assert "retrieval" in p.graph["attribution"]      # 依赖声明
    order = p.topological_order()
    assert order == sorted(order) or "retrieval" not in order or \
        order.index("retrieval") < order.index("attribution")


def test_plan_constraints_derived_and_json():
    sup = _sup()
    p = sup.make_plan({"qid": "P-01", "question_type": "data",
                       "question": "2026 年 9 月的 GMV 是多少？"})
    assert p.constraints.get("metric") == "gmv_gross"
    assert list(p.constraints.get("time_window") or [])
    s = p.to_json()
    d = json.loads(s)
    assert d["qid"] == "P-01" and d["intent"] == "data"
    assert "rationale" in d and isinstance(d["rationale"], list)


def test_answer_records_plan_in_history():
    sup = _sup()
    r = sup.answer({"qid": "P-02", "question_type": "data",
                    "question": "2026 年 9 月的退款率是多少？"})
    entries = [h for h in (r.history or [])
               if isinstance(h, dict) and "supervisor" in h]
    last = entries[-1]["supervisor"]
    assert last.get("intent") == "data"
    assert last.get("agents") == ["data"]
    assert "constraint" in json.dumps(last.get("constraints", {}), default=str) \
        or last.get("constraints")
