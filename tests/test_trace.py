# -*- coding: utf-8 -*-
"""W5+ 可追溯执行图测试：答案数字反查 + 执行图压缩。"""
import json
from types import SimpleNamespace
import unittest

from agent.trace import execution_graph, trace_numbers


class TestTraceNumbers(unittest.TestCase):
    def test_corpus_value_and_derived_delta(self):
        tools = [json.dumps({"baseline": {"current": 2.44,
                                         "previous": 1.55}})]
        nums = trace_numbers("退款率 2.44%（上月 1.55%），上升 0.89 个百分点。", tools)
        src = {n["value"]: n["source"] for n in nums}
        self.assertEqual(src["2.44"], "corpus_value")
        self.assertEqual(src["1.55"], "corpus_value")
        self.assertEqual(src["0.89"], "derived")

    def test_ratio_sign_agnostic(self):
        tools = [json.dumps({"baseline": {"current": 3514.54,
                                         "previous": 3780.40}})]
        nums = trace_numbers("变化约 7.03%。", tools)
        self.assertEqual(nums[0]["source"], "derived")

    def test_unknown_number_flagged(self):
        tools = [json.dumps({"baseline": {"current": 2.44,
                                         "previous": 1.55}})]
        nums = trace_numbers("退款率 9.99%（上月 1.55%）。", tools)
        src = {n["value"]: n["source"] for n in nums}
        self.assertEqual(src["9.99"], "unknown")
        self.assertEqual(src["1.55"], "corpus_value")

    def test_date_fragment_not_misused_for_pct(self):
        """带单位数字不可借用日期碎片的整数（9 ≠ 9.00% 证据）。"""
        tools = [json.dumps({"time_window": {"start": "2026-09-01",
                                            "end": "2026-09-30"}})]
        nums = trace_numbers("退款率 9.00%。", tools)
        self.assertEqual(nums[0]["source"], "unknown")


class TestExecutionGraph(unittest.TestCase):
    def _fake(self, tool_outputs, answer="答案", error=None):
        return SimpleNamespace(qid="T-1", answer=answer, error=error,
                               history=[{}, {}], tool_outputs=tool_outputs,
                               steps=2)

    def test_stage_kinds(self):
        g = execution_graph(self._fake([
            json.dumps({"time_window": {"start": "2026-09-01"}}),
            json.dumps({"columns": ["d"], "rows": [["2026-09-01", 1.0]]}),
            json.dumps({"memory": []}),
        ]))
        self.assertEqual([s["kind"] for s in g["stages"]],
                         ["time_window", "sql_result", "memory"])
        self.assertFalse(g["blocked"])

    def test_blocked_graph_has_no_numbers(self):
        g = execution_graph(self._fake(
            [json.dumps({"verifier": {"passed": False, "issues": ["x"]}})],
            answer="被拦截的答案 9.99%"))
        self.assertTrue(g["blocked"])
        self.assertEqual(g["numbers"], [])
        self.assertEqual(g["n_untraceable"], 0)

    def test_clean_graph_numbers_traced(self):
        g = execution_graph(self._fake(
            [json.dumps({"baseline": {"current": 2.44, "previous": 1.55}})],
            answer="退款率 2.44%（上月 1.55%）。"))
        self.assertFalse(g["blocked"])
        self.assertEqual(g["n_untraceable"], 0)


if __name__ == "__main__":
    unittest.main()
