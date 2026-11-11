#!/usr/bin/env python3
"""legacy-import.py 的行为测试：跑在真实的卷六数据上，缺文件时跳过而不是伪装通过。

只断言行为与不变量，不断言具体文案：
  * cue 总数等于记录数（导入不能丢行）
  * 每个 cue 的 speakerId 都出现在该作品的 speakers 里（界面下拉才不会空）
  * 超长章节被拆段，且拆段后每段都不超过前端上限
  * 复核队列条目数等于 conf < 0.5 的条目数
  * 闸门状态与正文 sha 被原样带出来

    python3 scripts/audiobook/test_legacy_import.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

# 与脚本同目录，避免依赖包结构。
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("legacy_import", HERE / "legacy-import.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 legacy-import.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

DATA = Path.home() / "ss_audiobook" / "data"
RECORDS = DATA / "vol6.jsonl"
# 嗓音圣经在项目根目录，不在 data/ 下；写错会静默跳过全部用例。
BIBLE = Path.home() / "ss_audiobook" / "voice_bible_v5.json"
GATE = DATA / "attribution_state.json"


@unittest.skipUnless(RECORDS.exists() and BIBLE.exists(), "没有本卷数据，跳过")
class LegacyImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = legacy.load_jsonl(RECORDS)
        cls.bible = json.loads(BIBLE.read_text(encoding="utf-8"))
        cls.gate = json.loads(GATE.read_text(encoding="utf-8")) if GATE.exists() else {}

    def works(self) -> list[dict]:
        """按章节构建全部作品，供各个断言复用。"""
        chapters: dict[str, list[dict]] = {}
        for row in self.rows:
            chapters.setdefault(row["ch"], []).append(row)
        result = []
        for chapter, rows in chapters.items():
            result.extend(legacy.build_chapter(chapter, rows, self.bible, self.gate))
        return result

    def test_cue_total_matches_records(self) -> None:
        total = sum(len(work["draft"]["subtitles"]["cues"]) for work in self.works())
        self.assertEqual(total, len(self.rows), "导入过程丢了行")

    def test_every_cue_speaker_exists(self) -> None:
        for work in self.works():
            speakers = {speaker["id"] for speaker in work["draft"]["subtitles"]["speakers"]}
            for cue in work["draft"]["subtitles"]["cues"]:
                self.assertIn(cue["speakerId"], speakers, f"{work['draft']['id']} 的说话人缺失")

    def test_parts_respect_frontend_limits(self) -> None:
        for work in self.works():
            cues = work["draft"]["subtitles"]["cues"]
            self.assertLessEqual(len(cues), legacy.PART_CUE_LIMIT)
            self.assertLessEqual(len(work["draft"]["text"]), legacy.PART_TEXT_LIMIT)

    def test_oversized_chapter_is_split(self) -> None:
        by_chapter: dict[str, list[dict]] = {}
        for row in self.rows:
            by_chapter.setdefault(row["ch"], []).append(row)
        # 取正文最长的一章：它超过 partTextLimit，必须拆成多段。
        longest = max(by_chapter.items(), key=lambda item: sum(len(row["text"]) for row in item[1]))
        works = legacy.build_chapter(longest[0], longest[1], self.bible, self.gate)
        self.assertGreater(len(works), 1, "超长章节没有拆段")
        self.assertEqual({work["gate"]["parts"] for work in works}, {len(works)})

    def test_review_queue_matches_low_confidence(self) -> None:
        expected = sum(1 for row in self.rows if row["conf"] < legacy.REVIEW_CONFIDENCE)
        actual = sum(
            1
            for work in self.works()
            for cue in work["draft"]["subtitles"]["cues"]
            if isinstance(cue.get("confidence"), (int, float)) and cue["confidence"] < legacy.REVIEW_CONFIDENCE
        )
        self.assertEqual(actual, expected)

    def test_gate_and_sha_are_carried_over(self) -> None:
        works = [work for work in self.works() if work["gate"]["status"] == "verified"]
        self.assertTrue(works, "闸门状态没有带出来")
        for work in works:
            self.assertRegex(work["gate"]["textSha"], r"^[0-9a-f]{16}$")

    def test_settings_default_to_clone_without_drift(self) -> None:
        for work in self.works():
            for speaker in work["draft"]["subtitles"]["speakers"]:
                settings = speaker["settings"]
                self.assertEqual(settings["mode"], "clone", "默认必须是 clone-only")
                self.assertEqual(settings["guidanceScale"], 1.0, "默认 guidance 必须为 1.0")


class ManifestTest(unittest.TestCase):
    """跑一次完整导入，确认产出文件与清单自洽。"""

    @unittest.skipUnless(RECORDS.exists() and BIBLE.exists(), "没有本卷数据，跳过")
    def test_full_run_writes_consistent_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "legacy-import.py",
                "--records", str(RECORDS),
                "--bible", str(BIBLE),
                "--gate", str(GATE),
                "--out", tmp,
            ]
            old = sys.argv
            sys.argv = argv
            try:
                self.assertEqual(legacy.main(), 0)
            finally:
                sys.argv = old
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["cues"], manifest["records"])
            self.assertEqual(manifest["works"], len(list((Path(tmp) / "works").glob("*.json"))))
            self.assertEqual(manifest["works"], len(list((Path(tmp) / "gate").glob("*.json"))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
