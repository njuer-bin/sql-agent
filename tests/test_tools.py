"""工具层行为：限额 / 超时 / schema / metric / 工具接住护栏异常。"""

import time

import pytest
from agent.errors import GuardrailError, SqlToolError
from agent.tools import ToolBox


@pytest.fixture(scope="module")
def tools() -> ToolBox:
    t = ToolBox()
    yield t
    t.close()


def test_row_limit_1000(tools: ToolBox):
    r = tools.sql_execute("SELECT order_id FROM fact_order WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30'")
    assert r["rowcount"] == 1000
    assert r["truncated"] is True


def test_guardrail_error_propagates(tools: ToolBox):
    with pytest.raises(GuardrailError):
        tools.sql_execute("DROP TABLE fact_order")


def test_bad_sql_raises_sqltoolerror(tools: ToolBox):
    with pytest.raises(SqlToolError):
        tools.sql_execute(
            "SELECT nonexistent_col FROM fact_order WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30'"
        )


def test_schema_lookup_all_and_filtered(tools: ToolBox):
    all_schema = tools.schema_lookup()
    assert "CREATE TABLE fact_order" in all_schema
    filtered = tools.schema_lookup(["fact_service_ticket"])
    assert "fact_service_ticket" in filtered
    assert "dim_product" not in filtered


def test_metric_lookup_gmv(tools: ToolBox):
    hits = tools.metric_lookup("GMV 是多少")
    assert hits
    assert hits[0]["metric_id"] in ("gmv_gross", "gmv_net")


def test_metric_lookup_repurchase_alias(tools: ToolBox):
    hits = tools.metric_lookup("复购率怎么算")
    assert any(h["metric_id"] == "repurchase_rate" for h in hits)


def test_timeout_free_query_fast(tools: ToolBox):
    t0 = time.monotonic()
    tools.sql_execute(
        "SELECT COUNT(*) FROM fact_order WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30'"
    )
    assert time.monotonic() - t0 < 10
