"""v0.8 Agent 间通信协议：AgentMessage / Evidence / Claim / Provenance。

把松散字典结果统一为结构化消息，令「多智能体协作」可被 verifiable 地追溯：

  RetrievalExpert → MemoryEvidence（检索到的记忆条目及其出处）
  DataExpert      → SQLResult（SQL + 执行结果 + 执行留痕出处）
  AttributionExpert → Claim（结论声明 + 证据链 evidence_ids）
  Verifier        → 校验 Claim ↔ Evidence 关系（数字/方向/窗口/归因）

每条 Evidence 带 provenance（来源工具输出下标 + 摘要）；
Claim 只能引用存在的 evidence_ids —— 计划层（plan.py）、执行层（本层）、
验证层（verifier_adv.py）三者从此对齐同一套结构。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """一条可追问出处的证据（数字锚点 / SQL / 记忆条目）。"""
    evidence_id: str
    source: str                 # sql / memory / tool
    ref: str                    # 定位：tool_outputs[i] 或 memory_id
    content: str                # 摘要（SQL 或数值片段或记忆正文片段）
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id, "source": self.source,
            "ref": self.ref, "content": self.content,
            "provenance": self.provenance,
        }


@dataclass
class Claim:
    """一条结论声明及其引用的证据 id（引用必须存在，否则是幽灵断言）。"""
    agent: str
    statement: str
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"agent": self.agent, "statement": self.statement,
                "evidence_ids": list(self.evidence_ids)}


@dataclass
class AgentMessage:
    """一个专家的统一输出信封。"""
    agent: str
    plan_ref: str               # 对应 TaskPlan 的阶段 / 专家名
    status: str = "ok"          # ok / partial / failed
    payload: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    claim: Claim | None = None

    def evidence_ids(self) -> list[str]:
        return [e.evidence_id for e in self.evidence]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "plan_ref": self.plan_ref,
            "status": self.status, "payload": self.payload,
            "evidence": [e.to_dict() for e in self.evidence],
            "claim": self.claim.to_dict() if self.claim else None,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


def _tool_index(out) -> int | None:
    """把执行留痕的下标塞进 provenance（首个非 verifier 输出）。"""
    errs = getattr(out, "tool_outputs", None) or []
    for i, t in enumerate(errs):
        if "verifier" not in t:
            return i
    return None


def message_from_retrieval(out) -> AgentMessage:
    """MemoryAgent/RetrievalExpert 输出 → MemoryEvidence 消息。"""
    ids = list(getattr(out, "retrieved", None) or [])
    msg = AgentMessage(
        agent="retrieval", plan_ref="memory_retrieve",
        status="failed" if getattr(out, "error", None) else "ok",
        payload={"memory_ids": ids, "answer": str(getattr(out, "answer", ""))},
    )
    dump_idx = None
    for i, t in enumerate(getattr(out, "tool_outputs", None) or []):
        if '"memory"' in t:
            dump_idx = i
            break
    for k, mid in enumerate(ids):
        prov = {"tool_index": dump_idx, "slot": k} if dump_idx is not None else {}
        msg.evidence.append(Evidence(
            evidence_id=mid, source="memory", ref=str(mid),
            content=str(getattr(out, "answer", ""))[:256], provenance=prov))
    return msg


def message_from_data(out) -> AgentMessage:
    """取数专家输出 → SQLResult 消息（SQL 与结果均为一等证据）。"""
    sql = getattr(out, "sql", None)
    cols = getattr(out, "columns", None)
    rows = getattr(out, "result", None)
    err = getattr(out, "execution_error", None) or getattr(out, "error", None)
    msg = AgentMessage(
        agent="data", plan_ref="sql_generate",
        status="failed" if err else "ok",
        payload={"sql": sql, "columns": cols, "rows": rows,
                 "execution_error": err},
    )
    if sql:
        msg.evidence.append(Evidence(
            evidence_id=f"sql:{hash(sql) % 10**8:08x}", source="sql",
            ref=f"tool_outputs[{0}]", content=sql,
            provenance={"kind": "query"}))
    if rows:
        idx = _tool_index(out)
        msg.evidence.append(Evidence(
            evidence_id=f"rows:{len(rows)}", source="tool",
            ref=f"tool_outputs[{idx}]" if idx is not None else "tool_outputs",
            content=json.dumps({"columns": cols, "rows": rows[:5]},
                               ensure_ascii=False, default=str),
            provenance={"kind": "result_rows", "n_rows": len(rows),
                        **({"tool_index": idx} if idx is not None else {})}))
    return msg


def message_from_attribution(out) -> AgentMessage:
    """归因专家输出 → Claim 消息（结论 + 证据链，不缺证据步骤）。"""
    ans = str(getattr(out, "answer", ""))
    msg = AgentMessage(
        agent="attribution", plan_ref="answer_compose",
        status="failed" if getattr(out, "error", None) else "ok",
        payload={"answer": ans, "metric_evidence_steps":
                 list(getattr(out, "evidence", None) or [])},
    )
    evids: list[str] = []
    # 数据证据
    for e in message_from_data(out).evidence:
        e.evidence_id = f"data:{e.evidence_id}"
        e.provenance["from"] = "attribution"
        msg.evidence.append(e)
        evids.append(e.evidence_id)
    # 记忆证据
    for mid in list(getattr(out, "retrieved", None) or []):
        ev = Evidence(evidence_id=f"memory:{mid}", source="memory", ref=mid,
                      content="", provenance={"from": "attribution"})
        for i, t in enumerate(getattr(out, "tool_outputs", None) or []):
            if '"memory"' in t and mid and mid in t:
                ev.provenance["tool_index"] = i
                break
        msg.evidence.append(ev)
        evids.append(ev.evidence_id)
    blocked = "对抗验证未通过" in ans
    msg.claim = Claim(
        agent="attribution", statement=ans,
        evidence_ids=evids if not blocked else [],
    )
    return msg


def verify_claim_contacts(msg: AgentMessage) -> list[str]:
    """Claim ↔ Evidence 结构校验：引用必须存在（幽灵断言 → issue 列表）。"""
    issues: list[str] = []
    if msg.claim is None:
        return issues
    ids = set(msg.evidence_ids())
    for cid in msg.claim.evidence_ids:
        if cid not in ids:
            issues.append(f"claim 引用了不存在的证据 {cid}")
    if msg.status == "ok" and not msg.claim.evidence_ids:
        issues.append("claim 没有任何证据支撑（幽灵断言）")
    return issues
