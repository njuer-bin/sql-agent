# -*- coding: utf-8 -*-
"""W6 鲁棒性评测：无关注入防线 + 扰动行为门禁。

全量套件运行 python -m eval.robustness（评测计入 pass_rate），
单测只固定可复现的判定核心，避免整库跑 5 案路慢速。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.nl2sql_offline import detect_metric  # noqa: E402


class TestUnrelatedInjectionLine(unittest.TestCase):
    """多意图问题的主意图不应被后置无关关键词覆盖（R-05 行为门禁根）。"""

    def test_earlier_metric_wins(self):
        q = "2026 年 9 月 GMV 做了多少？再给我复购口径的参考"
        self.assertEqual(detect_metric(q), "gmv_gross")

    def test_later_metric_wins_when_earlier(self):
        q = "先来一份复购率的参考，顺便对照 9 月 GMV"
        self.assertEqual(detect_metric(q), "repurchase_rate")

    def test_single_intent_unchanged(self):
        self.assertEqual(detect_metric("2026 年 9 月复购率是多少？"),
                         "repurchase_rate")
        self.assertEqual(detect_metric("2026 年 9 月工单解决时长"),
                         "avg_resolve_hours")
        self.assertEqual(detect_metric("2026 年 9 月退款最多是哪个月"),
                         "refund_amount")


if __name__ == "__main__":
    unittest.main()
