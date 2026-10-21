"""W2 验收：20 条人为构造的报错查询（五类：语法/列不存在/表不存在/超时/空结果）。

验收口径（PLAN.md W2，离线确定性修复、诚实记录）：
  A1 修复成功率 ≥ 70%（20 条上 ≥14 条恢复）
  A2 同一批 SQL，W1 single-shot 直接执行 vs W2 纠错循环的执行成功率差 ≥ 15pp
  A3 每步执行与修复动作被记录（steps / history）
  A4 不可修复的查询熔断，并以明确原因结束（不是一句"我不知道"）
"""

import pytest
from agent.error_repair import RepairLoop
from agent.tools import ToolBox


@pytest.fixture(scope="module")
def toolbox() -> ToolBox:
    t = ToolBox()
    yield t
    t.close()


OB = "'2026-09-01'"
OE = "'2026-09-30'"

BLUEPRINT = [
    # ---- 列不存在（5，全部可近邻修复）----
    {"id": "C-01", "sql": f"SELECT ROUND(SUM(pay_amounts),2) AS gmv FROM fact_order WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE}"},
    {"id": "C-02", "sql": f"SELECT ROUND(AVG(resolve_hour),2) AS ah FROM fact_service_ticket WHERE is_resolved=1 AND created_date BETWEEN {OB} AND {OE}"},
    {"id": "C-03", "sql": f"SELECT ROUND(100.0*SUM(refund_ratio)/SUM(pay_amount),2) AS rr FROM fact_order WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE}"},
    {"id": "C-04", "sql": "SELECT COUNT(*) AS n FROM dim_customer WHERE first_order_dates BETWEEN '2026-09-01' AND '2026-09-30'"},
    {"id": "C-05", "sql": f"SELECT SUM(quanity) AS qty FROM fact_order WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE}"},
    # ---- 表不存在（4，全部可近邻修复）----
    {"id": "T-01", "sql": f"SELECT COUNT(*) AS c FROM fact_orders WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE}"},
    {"id": "T-02", "sql": f"SELECT COUNT(*) AS c FROM fact_service_tickets WHERE created_date BETWEEN {OB} AND {OE}"},
    {"id": "T-03", "sql": "SELECT COUNT(*) AS c FROM dim_customers"},
    {"id": "T-04", "sql": "SELECT COUNT(*) AS c FROM dim_products"},
    # ---- 语法错（5；S-03 碰巧可执行；S-01/S-02 可机械修复；S-04/S-05 熔断给原因）----
    {"id": "S-01", "sql": f"SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE};;"},
    {"id": "S-02", "sql": f"SELECT COUNT(*) AS c FROM fact_order WHERE order_status=‘paid’ AND order_date BETWEEN {OB} AND {OE}"},
    {"id": "S-03", "sql": f"SELECT COUNT(*) AS cnt，order_status FROM fact_order WHERE order_date BETWEEN {OB} AND {OE}"},
    {"id": "S-04", "sql": f"SELECT ROUND(SUM(pay_amount),2 AS gmv FROM fact_order WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE}"},
    {"id": "S-05", "sql": f"SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN {OB} AND {OE} AND ;"},
    # ---- 超时（1：无时间窗可收窄，必须熔断给原因）----
    {"id": "X-01", "sql": "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 200000000) SELECT COUNT(*) AS c FROM cnt"},
    # ---- 空结果（5；时间窗/等值条件放宽后可恢复；COUNT=0 行也算空）----
    {"id": "E-01", "sql": "SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN '2026-10-16' AND '2026-10-17'"},
    {"id": "E-02", "sql": "SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN '2026-10-16' AND '2026-10-19'"},
    {"id": "E-03", "sql": "SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN '2026-10-20' AND '2026-10-31'"},
    {"id": "E-04", "sql": "SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN '2026-10-16' AND '2027-02-17'"},
    {"id": "E-05", "sql": "SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN '2027-01-01' AND '2027-01-28'"},
]

UNREPAIRABLE = ("S-04", "S-05", "X-01")


def test_a1_repair_rate_at_least_70pct(toolbox: ToolBox):
    loop = RepairLoop(toolbox)
    results = [(c["id"], loop.run(c["sql"]).ok) for c in BLUEPRINT]
    fixed = [rid for rid, ok in results if ok]
    ratio = len(fixed) / len(BLUEPRINT)
    print(f"\n修复成功 {len(fixed)}/{len(BLUEPRINT)} = {ratio:.0%}（熔断: {[r[0] for r in results if not r[1]]}）")
    assert ratio >= 0.70, f"修复率 {ratio:.0%} < 70%"


def test_a2_execution_gain_vs_single_shot_at_least_15pp(toolbox: ToolBox):
    """同一批 SQL：W1 single-shot 不纠错直接执行（成功即 ok）；W2 走纠错循环。
    在该执行面（error corpus）上成功率的提升必须 ≥ 15 个百分点。"""
    w1_ok = 0
    for c in BLUEPRINT:
        try:
            toolbox.sql_execute(c["sql"])
            w1_ok += 1
        except Exception:
            pass
    loop = RepairLoop(toolbox)
    w2_ok = sum(1 for c in BLUEPRINT if loop.run(c["sql"]).ok)
    gain = (w2_ok - w1_ok) / len(BLUEPRINT)
    assert gain >= 0.15, f"execution 提升 {gain:.0%} < 15pp（W1={w1_ok}, W2={w2_ok}）"


def test_a3_steps_and_history_recorded(toolbox: ToolBox):
    loop = RepairLoop(toolbox)
    case = next(c for c in BLUEPRINT if c["id"] == "C-01")  # 列错误至少 1 次修复重试
    oc = loop.run(case["sql"])
    assert oc.ok and oc.result is not None
    assert oc.steps >= 2                     # 首次失败 + 修复后成功
    assert oc.history, "修复动作未写入 history"
    h = oc.history[0]
    assert h["error_type"] == "missing_column"
    assert h["sql"], "修复后的 SQL 未记录"
    assert h["fix"], "修复策略描述未记录"


def test_a4_circuit_breaker_has_explicit_reason(toolbox: ToolBox):
    """不可修复的错误必须熔断，并给出明确失败原因（不是一句'我不知道'）。"""
    loop = RepairLoop(toolbox)
    for cid in UNREPAIRABLE:
        case = next(c for c in BLUEPRINT if c["id"] == cid)
        oc = loop.run(case["sql"])
        assert not oc.ok, f"{cid} 不应被修复成功"
        assert oc.final_sql, f"{cid} 未记录触发熔断的 SQL"
        reason = oc.error or ""
        assert reason, f"{cid} 缺失失败原因"
        assert "我不知道" not in reason
        assert ("无法自动修复" in reason or "无法放宽" in reason
                or "护栏" in reason or "超时" in reason), f"{cid} 原因不明确: {reason}"


def test_budget_never_exceeded(toolbox: ToolBox):
    loop = RepairLoop(toolbox)
    for cid in UNREPAIRABLE:
        oc = loop.run(next(c for c in BLUEPRINT if c["id"] == cid)["sql"])
        assert oc.steps <= loop.max_repairs + 1
        assert len(oc.history) <= loop.max_repairs


def test_guardrail_error_never_auto_repaired(toolbox: ToolBox):
    """安全红线：护栏错误不进修复循环，直接失败并说明是护栏拦截。"""
    loop = RepairLoop(toolbox)
    oc = loop.run("DELETE FROM fact_order")
    assert not oc.ok
    assert "护栏" in (oc.error or "")


def test_empty_result_count_zero_is_empty(toolbox: ToolBox):
    """COUNT/SUM 聚合永远返回一行 → [[0]] 也必须判为空并触发放宽，而不是直接返回成功。"""
    loop = RepairLoop(toolbox)
    oc = loop.run("SELECT COUNT(*) AS c FROM fact_order WHERE order_status='paid' AND order_date BETWEEN '2027-01-01' AND '2027-01-28'")
    assert oc.ok                                   # 放宽时间窗后触达数据
    assert oc.steps >= 2
    assert any(h["error_type"] == "empty_result" for h in oc.history)
