# -*- coding: utf-8 -*-
"""AST SQL 护栏测试：白名单 + 逃逸向量。"""
import unittest

from agent.errors import GuardrailError
from agent.sqlguard import validate_sql_ast

OK = [
    "SELECT COUNT(*) FROM fact_order WHERE order_date BETWEEN '2026-09-01' "
    "AND '2026-09-30' AND order_status='paid'",
    "WITH t AS (SELECT region_id FROM fact_order WHERE order_date >= '2026-09-01') "
    "SELECT COUNT(*) FROM t",
    "SELECT r.region_name, ROUND(SUM(o.pay_amount), 2) FROM fact_order o "
    "JOIN dim_region r ON r.region_id=o.region_id "
    "WHERE o.order_date >= '2026-09-01' GROUP BY r.region_name",
]
ESCAPES = [
    "ATTACH DATABASE 'evil.db' AS evil",                                  # 附加库
    "SELECT load_extension('x')",                                          # 动态库加载
    "PRAGMA database_list",                                                # 环境探查
    "SELECT name FROM sqlite_master', 2026-09-01)",          # 系统表（含干扰）
    "DROP TABLE fact_order",                                                          # DDL
    "SELECT 1; DELETE FROM fact_order WHERE order_date >= '2026-09-01'",  # 多语句
    "SELECT * FROM (WITH u AS (DELETE FROM dim_region WHERE region_id=1) "
    "SELECT 1)",                                       # 内嵌 DML（解析失败也拦）
    "SELECT readfile('C:/Windows/win.ini')",                            # 文件读取
    "sElEcT name FROM sqlite_temp_maSter",                                          # 大小写混淆
    "ATTACH DATABASE 'file:x' AS y; SELECT 1",                            # 附加库+查询
    "SELECT writefile('a', 'b')",
]


class TestSqlGuardAst(unittest.TestCase):
    def test_readonly_whitelisted_sql_passes(self):
        for sql in OK:
            self.assertEqual(validate_sql_ast(sql), sql)

    def test_escape_vectors_rejected(self):
        for sql in ESCAPES:
            with self.assertRaises(GuardrailError, msg=sql):
                validate_sql_ast(sql)

    def test_system_table_probe_blocked(self):
        with self.assertRaises(GuardrailError):
            validate_sql_ast(
                "SELECT COUNT(*) FROM sqlite_master WHERE order_date >= '2026-09-01'")

    def test_dml_blocked(self):
        with self.assertRaises(GuardrailError):
            validate_sql_ast("INSERT INTO dim_region VALUES (99, 'x')")

    def test_unparsable_payload_blocked(self):
        with self.assertRaises(GuardrailError):
            validate_sql_ast("SELECT 1))) UNION SELECT 'payload'--")


if __name__ == "__main__":
    unittest.main()
