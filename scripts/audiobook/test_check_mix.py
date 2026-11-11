#!/usr/bin/env python3
"""check-mix.py 的行为测试：用合成混音配置验证 ADR-0004 的五条断言。

    python3 scripts/audiobook/test_check_mix.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_mix", HERE / "check-mix.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 check-mix.py")
check_mix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_mix)


def valid_config() -> dict:
    """三条层都在区间内、侧链 16 dB、hit 前移 40% 的最小配置。"""
    return {
        "sidechain_db": 16,
        "ambience": [{"id": "wind", "gain_db": -27}],
        "bed": [{"id": "rain", "gain_db": -21}],
        "hit": [{
            "id": "clash", "gain_db": -12, "seconds": 4,
            "start_s": 8.4, "anchor_s": 10.0,
        }],
    }


def violations(result: dict) -> list[tuple[str, str]]:
    return [(item["role"], item["field"]) for item in result["violations"]]


class GainTest(unittest.TestCase):
    def test_valid_config_passes(self) -> None:
        result = check_mix.evaluate([valid_config()])
        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(result["checked_items"], 3)
        self.assertEqual(result["violations"], [])

    def test_boundaries_are_inclusive(self) -> None:
        config = {
            "sidechain_db": 16,
            "ambience": [{"gain_db": -30}, {"gain_db": -25}],
            "bed": [{"gain_db": -23}, {"gain_db": -20}],
            "hit": [{"gain_db": -16}, {"gain_db": -8}],
        }
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(result["checked_items"], 6)

    def test_each_gain_rule_is_reported(self) -> None:
        config = valid_config()
        config["ambience"] = [{"id": "amb", "gain_db": -20}]
        config["bed"] = [{"id": "bed", "gain_db": -30}]
        config["hit"] = [{"id": "hit", "gain_db": -20, "seconds": 4, "start_s": 8.4, "anchor_s": 10.0}]
        result = check_mix.evaluate([config])
        self.assertFalse(result["ok"])
        self.assertEqual(
            violations(result),
            [("ambience", "gain_db"), ("bed", "gain_db"), ("hit", "gain_db")],
        )

    def test_missing_gain_is_a_violation(self) -> None:
        config = valid_config()
        config["bed"] = [{"id": "bed"}]
        result = check_mix.evaluate([config])
        self.assertFalse(result["ok"])
        self.assertEqual(violations(result), [("bed", "gain_db")])

    def test_unknown_role_is_skipped_not_failed(self) -> None:
        config = valid_config()
        config["score"] = [{"id": "theme", "gain_db": -5}]
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        self.assertTrue(any("score" in warning for warning in result["warnings"]))


class SidechainTest(unittest.TestCase):
    def test_missing_sidechain_is_a_violation(self) -> None:
        config = valid_config()
        del config["sidechain_db"]
        result = check_mix.evaluate([config])
        self.assertFalse(result["ok"])
        self.assertIn(("effect-bus", "sidechain_db"), violations(result))

    def test_shallow_sidechain_is_a_violation(self) -> None:
        config = valid_config()
        config["sidechain_db"] = 12
        result = check_mix.evaluate([config])
        self.assertFalse(result["ok"])
        self.assertEqual(violations(result), [("effect-bus", "sidechain_db")])

    def test_track_duck_db_counts_as_sidechain(self) -> None:
        config = {
            "tracks": [{"id": "fx", "role": "bed", "gainDb": -21, "duckDb": 18}],
        }
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(len(result["sidechain"]), 1)
        self.assertTrue(result["sidechain"][0]["ok"])


class HitLeadTest(unittest.TestCase):
    def test_lead_is_computed_from_start_and_anchor(self) -> None:
        result = check_mix.evaluate([valid_config()])
        lead = result["hit_leads"][0]
        self.assertAlmostEqual(lead["lead_seconds"], 1.6)
        self.assertAlmostEqual(lead["ratio"], 0.4)
        self.assertTrue(lead["ok"])

    def test_lead_tolerance_edges(self) -> None:
        for start, expected in ((8.8, 0.3), (8.0, 0.5)):
            config = valid_config()
            config["hit"] = [{
                "id": "clash", "gain_db": -12, "seconds": 4,
                "start_s": start, "anchor_s": 10.0,
            }]
            result = check_mix.evaluate([config])
            self.assertTrue(result["hit_leads"][0]["ok"], expected)

    def test_too_little_or_too_much_lead_is_a_violation(self) -> None:
        for lead_seconds in (0.2, 3.0):
            config = valid_config()
            config["hit"] = [{
                "id": "clash", "gain_db": -12, "seconds": 4,
                "lead_seconds": lead_seconds,
            }]
            result = check_mix.evaluate([config])
            self.assertFalse(result["ok"])
            self.assertEqual(violations(result), [("hit", "lead_ratio")])

    def test_missing_timing_only_warns(self) -> None:
        config = valid_config()
        config["hit"] = [{"id": "clash", "gain_db": -12, "seconds": 3}]
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        self.assertIsNone(result["hit_leads"][0]["ok"])
        self.assertTrue(result["warnings"])

    def test_start_clamped_at_zero_is_warned_not_failed(self) -> None:
        config = valid_config()
        config["hit"] = [{
            "id": "opening", "gain_db": -12, "seconds": 4,
            "start_s": 0, "anchor_s": 1.0,
        }]
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        lead = result["hit_leads"][0]
        self.assertTrue(lead["clamped"])
        self.assertTrue(any("章首钳制" in warning for warning in result["warnings"]))


class LegacyShapeTest(unittest.TestCase):
    def test_places_and_cues_files_are_understood(self) -> None:
        config = {
            "sidechain_db": 16,
            "places": [{"id": "antarctic", "gain_db": -28}],
            "cues": [
                {"id": "rain", "role": "bed", "gain_db": -21},
                {"id": "clash", "role": "hit", "gain_db": -10, "seconds": 3},
            ],
        }
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(result["checked_items"], 3)

    def test_placements_take_precedence_over_cues(self) -> None:
        config = {
            "sidechain_db": 16,
            "cues": [{"id": "clash", "role": "hit", "gain_db": 0}],
            "placements": [{
                "id": "clash", "role": "hit", "gain_db": -12,
                "seconds": 4, "start_s": 8.4, "anchor_s": 10.0,
            }],
        }
        result = check_mix.evaluate([config])
        self.assertTrue(result["ok"], result["violations"])
        self.assertEqual(len(result["items"]), 1)


class CliTest(unittest.TestCase):
    def test_cli_exit_codes_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.json"
            bad = root / "bad.json"
            out = root / "check.json"
            good.write_text(json.dumps(valid_config()), encoding="utf-8")
            broken = valid_config()
            broken["sidechain_db"] = 8
            bad.write_text(json.dumps(broken), encoding="utf-8")

            self.assertEqual(check_mix.main([str(good), "--json", str(out)]), 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])

            self.assertEqual(check_mix.main([str(bad), "--json", str(out)]), 1)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["violations"][0]["field"], "sidechain_db")


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
