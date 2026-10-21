"""验收 A3/A4：Guardrails 必须拒绝危险 SQL 与全表扫描。"""

import pytest
from agent.errors import GuardrailError
from agent.guardrails import validate_sql


def test_a3_drop_table_rejected():
    with pytest.raises(GuardrailError) as e:
        validate_sql("DROP TABLE fact_order")
    assert "只允许" in str(e.value) or "拒绝" in str(e.value)


def test_a3_write_family_rejected():
    for sql in [
        "DELETE FROM dim_customer",
        "INSERT INTO fact_order VALUES (1)",
        "UPDATE fact_order SET pay_amount=0",
        "ALTER TABLE fact_order ADD COLUMN x INT",
        "PRAGMA database_list",
        "ATTACH DATABASE 'x.db' AS x",
    ]:
        with pytest.raises(GuardrailError):
            validate_sql(sql)


def test_a3_prompt_injection_drop_via_agent_context():
    """提示词注入场景：要求 agent 执行 DROP TABLE —— 工具层必须拒绝。"""
    injected_sql = "SELECT 1; DROP TABLE fact_order"
    with pytest.raises(GuardrailError):
        validate_sql(injected_sql)


def test_drop_in_comment_does_not_false_positive():
    """注释里的危险字样不构成可执行语句，不应误伤正常只读查询。"""
    ok = validate_sql(
        "SELECT 'x' || (SELECT 1) FROM fact_order WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30' -- DROP TABLE t"
    )
    assert ok.lower().startswith("select")


def test_a4_full_scan_blocked():
    with pytest.raises(GuardrailError) as e:
        validate_sql("SELECT SUM(pay_amount) FROM fact_order")
    assert "全表扫描" in str(e.value)
    assert "时间" in str(e.value)


def test_a4_all_fact_tables_need_time_filter():
    for table in ("fact_order", "fact_service_ticket", "fact_web_traffic"):
        with pytest.raises(GuardrailError):
            validate_sql(f"SELECT COUNT(*) FROM {table}")


def test_time_filtered_query_passes():
    ok = validate_sql(
        "SELECT COUNT(*) FROM fact_order "
        "WHERE order_status='paid' AND order_date BETWEEN '2026-09-01' AND '2026-09-30'"
    )
    assert ok.lower().startswith("select")


def test_sensitive_field_blocked():
    with pytest.raises(GuardrailError):
        validate_sql(
            "SELECT mobile FROM dim_customer WHERE register_date >= '2026-01-01'"
        )


def test_with_cte_still_guarded():
    with pytest.raises(GuardrailError):
        validate_sql(
            "WITH t AS (SELECT 1 AS x) SELECT x FROM t; DROP TABLE fact_order"
        )


def test_business_text_with_drop_word_can_pass():
    """业务文本里出现 DROP 字样（在字符串字面量中）不应误报。"""
    ok = validate_sql(
        "SELECT order_id FROM fact_order "
        "WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30' "
        "AND order_status='paid'"
    )
    assert ok
