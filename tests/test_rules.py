"""领域纯规则测试：抽查证据适用性、成活事实、金额与比例。"""
import unittest
from decimal import Decimal

from forest.rules import RETENTION_RATE, evaluate_evidence, group_survival, money


def survey(sid, at, alive, dead, measured=1000):
    return {"id": sid, "surveyed_at": at, "alive_count": alive, "dead_count": dead,
            "measured_m2": measured, "group_no": 1, "sample_size": alive + dead}


class EvidenceRuleTests(unittest.TestCase):
    def test_compliant_two_groups(self):
        groups = [survey("s1", "2026-10-15T09:00:00+08:00", 10, 0),
                  survey("s2", "2026-11-02T09:00:00+08:00", 9, 1)]
        result = evaluate_evidence(groups, "2026-10-01T08:00:00+08:00")
        self.assertTrue(result["applicable"], result["failures"])
        self.assertEqual(result["group_count"], 2)

    def test_missing_group_and_no_observation_not_death(self):
        result = evaluate_evidence([survey("s1", "2026-10-15T09:00:00+08:00", 10, 0)],
                                   "2026-10-01T08:00:00+08:00")
        self.assertFalse(result["applicable"])
        self.assertIn("insufficient_groups", [f["code"] for f in result["failures"]])

    def test_interval_less_than_30_days(self):
        groups = [survey("s1", "2026-10-15T09:00:00+08:00", 10, 0),
                  survey("s2", "2026-10-25T09:00:00+08:00", 10, 0)]
        result = evaluate_evidence(groups, "2026-10-01T08:00:00+08:00")
        self.assertFalse(result["applicable"])
        self.assertIn("interval_not_met", [f["code"] for f in result["failures"]])

    def test_sample_too_small_and_survival_below_90(self):
        groups = [survey("s1", "2026-10-15T09:00:00+08:00", 8, 1),
                  survey("s2", "2026-11-02T09:00:00+08:00", 8, 2)]
        result = evaluate_evidence(groups, "2026-10-01T08:00:00+08:00")
        codes = [f["code"] for f in result["failures"]]
        self.assertIn("sample_too_small", codes)
        self.assertIn("survival_below_threshold", codes)

    def test_same_date_groups_rejected(self):
        groups = [survey("s1", "2026-10-15T09:00:00+08:00", 10, 0),
                  survey("s2", "2026-10-15T15:00:00+08:00", 10, 0)]
        result = evaluate_evidence(groups, "2026-10-01T08:00:00+08:00")
        self.assertIn("groups_same_date", [f["code"] for f in result["failures"]])

    def test_survival_ratio_and_money(self):
        self.assertEqual(group_survival(survey("s", "x", 9, 1)), Decimal("0.9"))
        self.assertEqual(money("200000"), 20000000)
        self.assertEqual(money(180000), 18000000)
        self.assertEqual(RETENTION_RATE, Decimal("0.20"))


if __name__ == "__main__":
    unittest.main()
