# -*- coding: utf-8 -*-
"""v0.8 Verifier 攻击测试集回归：发现率 / 误报率硬门禁。"""
import unittest

from eval.verifier_attack import run_attack_suite


class VerifierAttackSuite(unittest.TestCase):
    def setUp(self):
        self.r = run_attack_suite(save=False)

    def test_all_attack_vectors_detected(self):
        self.assertFalse(self.r["missed"])
        self.assertEqual(self.r["detection_rate"], 1.0)

    def test_no_false_positives_on_benign(self):
        self.assertFalse(self.r["false_positives"])
        self.assertEqual(self.r["false_positive_rate"], 0.0)

    def test_pinned_stats_saved(self):
        r2 = run_attack_suite(save=True)
        self.assertEqual(r2["attack_count"], 8)
        self.assertEqual(r2["benign_count"], 5)


if __name__ == "__main__":
    unittest.main()
