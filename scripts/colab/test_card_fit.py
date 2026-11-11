#!/usr/bin/env python3
"""card-fit.py 的纯逻辑测试：不联网、不租卡。"""

from __future__ import annotations

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "card-fit.py"
_spec = spec_from_file_location("card_fit", SCRIPT)
if _spec is None or _spec.loader is None:
    raise ImportError(f"加载不到 {SCRIPT}")
card_fit = module_from_spec(_spec)
_spec.loader.exec_module(card_fit)


class FlashAttentionGateTest(unittest.TestCase):
    def test_turing_has_no_flash_attention(self):
        # T4 是 sm75：这是它开不了加速的根本原因
        self.assertFalse(card_fit.flash_attention_supported(7.5))

    def test_ampere_and_above_have_flash_attention(self):
        for cap in (8.0, 8.6, 8.9, 9.0):
            self.assertTrue(card_fit.flash_attention_supported(cap), cap)


class PlanTest(unittest.TestCase):
    def test_t4_is_eager_only(self):
        plan = card_fit.plan_for(7.5, 16)
        self.assertEqual(plan["mode"], "eager-only")
        self.assertFalse(plan["flash_attention"])
        # 没有加速栈时不能拿官方 0.32 当预期，必须按慢档估
        self.assertAlmostEqual(plan["rtf_best_guess"], card_fit.RTF_SLOW)

    def test_l4_gets_fast_all(self):
        plan = card_fit.plan_for(8.9, 24)
        self.assertEqual(plan["mode"], "fast-all")
        self.assertTrue(plan["flash_attention"])
        self.assertTrue(plan["fast_all"])
        self.assertAlmostEqual(plan["rtf_best_guess"], card_fit.RTF_FAST)

    def test_a100_gets_fast_all(self):
        self.assertEqual(card_fit.plan_for(8.0, 40)["mode"], "fast-all")

    def test_h100_gets_fast_all(self):
        self.assertEqual(card_fit.plan_for(9.0, 80)["mode"], "fast-all")

    def test_small_card_is_unsupported(self):
        # 8 GB 装不下 eager 的 7.7 GiB 加余量
        self.assertEqual(card_fit.plan_for(8.9, 8)["mode"], "unsupported")

    def test_fast_all_without_flash_attention_is_degraded(self):
        plan = card_fit.plan_for(7.5, 32)
        self.assertEqual(plan["mode"], "fast-all-no-flash")
        self.assertAlmostEqual(plan["rtf_best_guess"], card_fit.RTF_SLOW)


class CostRangeTest(unittest.TestCase):
    def test_real_volume_both_ends(self):
        # 本卷实测 1317 分钟 = 21.95 小时
        cost = card_fit.cost_range(1317, balance=None)
        self.assertAlmostEqual(cost["audio_hours"], 21.95, places=2)
        self.assertAlmostEqual(cost["hours"]["optimistic"], 7.0, places=1)
        self.assertAlmostEqual(cost["hours"]["pessimistic"], 45.7, places=1)

    def test_units_scale_with_rate(self):
        # 之前成本表里 A100 的 538 单元 ≈ 45.7 小时 × 11.8 单元/小时
        cost = card_fit.cost_range(1317, balance=265.33, unit_rate=11.8)
        self.assertAlmostEqual(cost["units"]["pessimistic"], 539.4, places=0)
        self.assertFalse(cost["fits"]["pessimistic"])

    def test_cheap_rate_fits_balance(self):
        cost = card_fit.cost_range(1317, balance=265.33, unit_rate=1.0)
        self.assertTrue(cost["fits"]["optimistic"])
        self.assertTrue(cost["fits"]["pessimistic"])

    def test_zero_minutes_raises(self):
        with self.assertRaises(ValueError):
            card_fit.cost_range(0, balance=None)


class TableSanityTest(unittest.TestCase):
    def test_known_cards_are_consistent(self):
        for name, spec in card_fit.KNOWN_CARDS.items():
            self.assertGreater(spec["compute_cap"], 0, name)
            self.assertGreater(spec["vram_gb"], 0, name)

    def test_g4_is_deliberately_absent(self):
        # G4 代次未确认：宁可让用户实测后传参，也不要猜一个可能错的档位
        self.assertNotIn("G4", card_fit.KNOWN_CARDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
