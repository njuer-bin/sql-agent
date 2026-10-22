"""v0.8 Agent 间通信协议：AgentMessage / Evidence / Claim / Provenance。"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.protocol import (AgentMessage, Claim, Evidence,   # noqa: E402
                            message_from_attribution, message_from_data,
                            message_from_retrieval, verify_claim_contacts)
from agent.single_shot import SingleShotResult  # noqa: E402


def test_message_from_data_payload_and_evidence():
    out = SingleShotResult()
    out.sql = "SELECT 1"
    out.columns = ["gmv"]
    out.result = [[21506250.66]]
    out.tool_outputs = [json.dumps({"columns": ["gmv"],
                                    "rows": [[21506250.66]]},
                                   ensure_ascii=False)]
    msg = message_from_data(out)
    assert isinstance(msg, AgentMessage) and msg.status == "ok"
    assert msg.payload["sql"] == "SELECT 1"
    srcs = {e.source for e in msg.evidence}
    assert "sql" in srcs and "tool" in srcs
    rows_ev = next(e for e in msg.evidence if e.source == "tool")
    assert "21506250.66" in rows_ev.content
    assert rows_ev.provenance["tool_index"] == 0
    d = msg.to_dict()
    assert json.loads(json.dumps(d, ensure_ascii=False)) == d


def test_message_from_retrieval_memory_evidence():
    out = SingleShotResult()
    out.retrieved = ["sem-repurchase-v2", "pr-repurchase-v2-rule"]
    out.tool_outputs = [json.dumps(
        {"memory": [{"memory_id": "sem-repurchase-v2"}]},
        ensure_ascii=False)]
    msg = message_from_retrieval(out)
    assert msg.agent == "retrieval"
    ids = msg.evidence_ids()
    assert "sem-repurchase-v2" in ids and "pr-repurchase-v2-rule" in ids
    ev = next(e for e in msg.evidence if e.evidence_id == "sem-repurchase-v2")
    assert ev.source == "memory"
    assert ev.provenance.get("tool_index") == 0


def test_message_from_attribution_claim_contacts():
    out = SingleShotResult()
    out.sql = "SELECT ROUND(100.0*SUM(refund)/SUM(pay),2) FROM fact_order"
    out.columns = ["refund_rate"]
    out.result = [[0.97]]
    out.retrieved = ["ep-2026-09-logistics"]
    out.evidence = ["时间窗口定位", "现象确认取数"]
    out.tool_outputs = [
        json.dumps({"columns": ["refund_rate"], "rows": [[0.97]]},
                   ensure_ascii=False),
        json.dumps({"memory": [{"memory_id": "ep-2026-09-logistics"}]},
                   ensure_ascii=False)]
    msg = message_from_attribution(out)
    assert msg.claim is not None
    assert "对抗" not in msg.claim.statement[:5]
    assert verify_claim_contacts(msg) == []          # 证据链吻合
    # 幽灵断言：引用不存在的证据
    msg.claim.evidence_ids = ["memory:ghost"]
    issues = verify_claim_contacts(msg)
    assert issues and "ghost" in issues[0]
    # 凭空结论：无证据支撑
    msg2 = AgentMessage(agent="attribution", plan_ref="answer_compose",
                        status="ok", claim=Claim(agent="attribution",
                                                 statement="x"))
    assert verify_claim_contacts(msg2)


def test_message_from_attribution_blocked_status():
    out = SingleShotResult()
    out.retrieved = ["ep-2026-09-logistics"]
    out.evidence = ["对抗验证拦截"]
    out.answer = "对抗验证未通过，以下结论被拦下：……"
    msg = message_from_attribution(out)
    assert msg.claim.evidence_ids == []              # 被拦的结论不宣称证据


def test_retrieval_failed_status():
    out = SingleShotResult()
    out.error = "boom"
    out.retrieved = []
    msg = message_from_retrieval(out)
    assert msg.status == "failed" and msg.evidence == []
