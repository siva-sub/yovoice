#!/usr/bin/env python3
"""freeze-gate.py 的行为测试：只测闸门口径与退出码，不依赖本卷锚点。

    python3 scripts/audiobook/test_freeze_gate.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("freeze_gate", HERE / "freeze-gate.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 freeze-gate.py")
freeze_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freeze_gate)


def anchor(**overrides) -> dict:
    """一条默认通过阻断项的审计结果；用例只改自己关心的字段。"""
    row = {
        "file": "anchor_sunny.wav",
        "duration_s": 2.4,
        "median_f0_hz": 118.0,
        "voiced_frac": 0.82,
        "advisory": [],
    }
    row.update(overrides)
    return row


class GateTest(unittest.TestCase):
    def test_clean_anchor_is_allowed(self) -> None:
        result = freeze_gate.gate([anchor()])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "green")
        self.assertEqual(result["blocked_count"], 0)
        self.assertEqual(result["anchor_count"], 1)

    def test_advisory_does_not_block(self) -> None:
        # F0 偏离目标与语速都不可靠，只能提示；它们不能拦住冻结。
        result = freeze_gate.gate([anchor(advisory=["F0 偏离目标", "语速偏低"])])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "yellow")
        self.assertEqual(result["advisory_count"], 1)
        self.assertEqual(result["anchors"][0]["advisory"], ["F0 偏离目标", "语速偏低"])

    def test_f0_and_rate_are_advisory_even_without_audit_messages(self) -> None:
        # 手工裁剪过的审计 JSON 可能没有 advisory；闸门仍要把 F0/语速算成提示，而不是放成绿。
        result = freeze_gate.gate([
            anchor(expect_f0_hz=100.0, median_f0_hz=200.0, words_per_second=1.0),
        ])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "yellow")
        advice = result["anchors"][0]["advisory"]
        self.assertTrue(any("F0" in item for item in advice))
        self.assertTrue(any("语速" in item for item in advice))

    def test_short_anchor_blocks(self) -> None:
        result = freeze_gate.gate([anchor(duration_s=1.0)])
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "red")
        self.assertIn("太短", result["anchors"][0]["blocking"][0])

    def test_missing_f0_blocks(self) -> None:
        result = freeze_gate.gate([anchor(median_f0_hz=0)])
        self.assertFalse(result["allowed"])
        self.assertIn("无可用 F0", result["anchors"][0]["blocking"])

    def test_breathy_anchor_blocks(self) -> None:
        result = freeze_gate.gate([anchor(voiced_frac=0.4)])
        self.assertFalse(result["allowed"])
        self.assertTrue(any("浊音比" in reason for reason in result["anchors"][0]["blocking"]))

    def test_missing_voiced_frac_blocks(self) -> None:
        # 缺字段不能当成“没超标”，否则老版本审计结果会静默放行。
        result = freeze_gate.gate([anchor(voiced_frac=None)])
        self.assertFalse(result["allowed"])
        self.assertTrue(any("缺少浊音比" in reason for reason in result["anchors"][0]["blocking"]))

    def test_one_bad_anchor_blocks_the_batch(self) -> None:
        result = freeze_gate.gate([anchor(), anchor(file="bad.wav", voiced_frac=0.2)])
        self.assertFalse(result["allowed"])
        self.assertEqual(result["blocked_count"], 1)

    def test_empty_audit_is_refused(self) -> None:
        result = freeze_gate.gate([])
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "red")
        self.assertIn("为空", result["reason"])

    def test_thresholds_come_from_anchor_audit(self) -> None:
        self.assertEqual(freeze_gate.MIN_DURATION_S, 1.2)
        self.assertEqual(freeze_gate.MIN_VOICED_FRAC, 0.55)

    def test_custom_thresholds(self) -> None:
        result = freeze_gate.gate([anchor(duration_s=1.0, voiced_frac=0.5)], min_duration=0.9, min_voiced_frac=0.45)
        self.assertTrue(result["allowed"])

    def test_wrapped_payload_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"
            path.write_text(json.dumps({"anchors": [anchor()]}), encoding="utf-8")
            self.assertEqual(len(freeze_gate.load_audit(path)), 1)


class CliTest(unittest.TestCase):
    def test_good_audit_exits_zero_and_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.json"
            out_path = Path(tmp) / "gate.json"
            audit_path.write_text(json.dumps([anchor(advisory=["F0 偏离"])]), encoding="utf-8")
            code = freeze_gate.main([str(audit_path), "--json", str(out_path)])
            self.assertEqual(code, 0)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["allowed"])
            self.assertEqual(payload["advisory_count"], 1)

    def test_blocked_audit_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.json"
            out_path = Path(tmp) / "gate.json"
            audit_path.write_text(
                json.dumps([anchor(), anchor(file="bad.wav", median_f0_hz=0)]),
                encoding="utf-8",
            )
            code = freeze_gate.main([str(audit_path), "--json", str(out_path)])
            self.assertEqual(code, 1)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["allowed"])
            self.assertEqual(payload["blocked_count"], 1)


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
