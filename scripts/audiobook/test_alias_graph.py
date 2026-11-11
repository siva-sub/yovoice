#!/usr/bin/env python3
"""alias-graph.py 的行为测试：合成角色表 / 关系表 / 正文，不依赖本卷数据。

    python3 scripts/audiobook/test_alias_graph.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("alias_graph", HERE / "alias-graph.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 alias-graph.py")
alias_graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alias_graph)

CAST = {
    "_meta": {"note": "synthetic"},
    "sunny": {"aliases": ["Sunny", "Sunless", "Lost from Light"]},
    # 与 sunny 共用 Sunless：应当合并成同一个身份，而不是两个角色。
    "sunless": {"aliases": ["Sunless"]},
    "jet": {"aliases": ["Soul Reaper", "Reaper"]},
    "shadow_saint": {"aliases": ["Shadow Saint", "Stone Saint"]},
    "tyris": {"aliases": ["Saint Tyris"]},
    "rain": {"aliases": ["Rain"]},
}
RELATIONS = {
    "_meta": {"note": "synthetic"},
    "anvil": {
        "aliases": ["King of Swords"],
        "family": {"son": ["mordret"], "daughter": ["morgan"]},
        "clan": "head of Valor",
    },
    "morgan": {
        "aliases": ["Princess of Valor"],
        "family": {"father": ["anvil"], "sister": ["nephis"]},
        "clan": "Valor",
    },
    "nephis": {
        "aliases": ["Changing Star"],
        "family": {"father": ["anvil"], "sister": ["morgan"]},
        "clan": "Valor by adoption",
    },
    "seishan": {
        "aliases": ["Song Seishan"],
        "family": {"sister": ["beastmaster"]},
        "clan": "Song (adopted daughter)",
    },
    "sunny": {
        "aliases": ["Sunless", "Lost from Light"],
        "family": {"sister": ["rain"]},
        "clan": "government",
    },
    "_clan_address": {"Valor": ["brother", "sister"], "Song": ["sister"]},
    "_groups": {
        "valor": {"members": ["anvil", "morgan", "nephis"]},
        "song": {"members": ["seishan", "beastmaster"]},
    },
}
TEXTS = [
    ("1", "Sunny met the Soul Reaper. Sunless was Lost from Light. The Reaper watched."),
    ("2", "Saint Tyris nodded. A Saint watched. Princess of Valor greeted Shadow Saint."),
    ("2", "It was a sunny day. Her brother and sister waited."),
]


def build(cast: dict | None = None, relations: dict | None = None) -> dict:
    texts = list(TEXTS)
    return alias_graph.build_graph(
        alias_graph.cast_entries(cast or CAST),
        RELATIONS if relations is None else relations,
        texts,
        [],
    )


def identity(result: dict, identity_id: str) -> dict:
    return next(item for item in result["identities"] if item["id"] == identity_id)


def alias_row(result: dict, alias: str) -> dict:
    return next(item for item in result["aliases"] if item["alias"] == alias)


class AliasMatchTest(unittest.TestCase):
    def test_longest_alias_wins_and_short_one_is_not_double_counted(self) -> None:
        result = build()
        self.assertEqual(alias_row(result, "Soul Reaper")["occurrences"], 1)
        self.assertEqual(alias_row(result, "Reaper")["occurrences"], 1)
        self.assertEqual(identity(result, "jet")["occurrences"], 2)

    def test_saint_inside_longer_alias_is_not_counted_bare(self) -> None:
        result = build()
        saint = next(item for item in result["disambiguation"] if item["term"] == "Saint")
        # 正文里有 3 处 Saint：Saint Tyris、A Saint、Shadow Saint；只有第二处是裸头衔。
        self.assertEqual(saint["occurrences"], 1)
        self.assertEqual(saint["candidates"], ["shadow_saint", "tyris"])

    def test_case_sensitive_matching_keeps_sunny_and_lowercase_sunny_apart(self) -> None:
        result = build()
        # 正文的 "sunny day" 不应计入 Sunny 这个角色。
        self.assertEqual(identity(result, "sunny")["occurrences"], 3)

    def test_matches_alias_from_relations_only_identity(self) -> None:
        result = build()
        self.assertEqual(identity(result, "morgan")["occurrences"], 1)
        self.assertEqual(alias_row(result, "Princess of Valor")["chapters"], ["2"])


class IdentityMergeTest(unittest.TestCase):
    def test_shared_alias_merges_identities(self) -> None:
        result = build()
        sunny = identity(result, "sunny")
        self.assertEqual(sorted(sunny["merged_from"]), ["sunless", "sunny"])
        self.assertEqual(sunny["aliases"], ["Sunny", "Sunless", "Lost from Light"])
        self.assertFalse(any(item["id"] == "sunless" for item in result["identities"]))

    def test_identity_count_matches_merged_groups(self) -> None:
        result = build()
        self.assertEqual(result["identity_count"], len(result["identities"]))
        self.assertEqual(result["proposal_only"], True)


class RelationTest(unittest.TestCase):
    def test_family_edges_are_emitted(self) -> None:
        result = build()
        edges = {(edge["from"], edge["to"], edge["relation"], edge["sense"]) for edge in result["relations"]}
        self.assertIn(("anvil", "mordret", "son", "family"), edges)
        self.assertIn(("morgan", "nephis", "sister", "family"), edges)

    def test_adoption_is_derived_from_clan_note(self) -> None:
        result = build()
        edges = {(edge["from"], edge["to"], edge["relation"], edge["sense"]) for edge in result["relations"]}
        self.assertIn(("nephis", "valor", "adopted-by", "family"), edges)
        self.assertIn(("seishan", "song", "adopted-by", "family"), edges)

    def test_kin_terms_with_clan_and_family_senses_are_disambiguated(self) -> None:
        result = build()
        sister = next(item for item in result["disambiguation"] if item["term"] == "sister")
        self.assertEqual(sister["kind"], "kin-term")
        self.assertEqual(set(sister["senses"]), {"clan", "family"})
        self.assertIn("rain", sister["candidates"])
        self.assertIn("nephis", sister["candidates"])
        self.assertIn("beastmaster", sister["candidates"])
        brother = next(item for item in result["disambiguation"] if item["term"] == "brother")
        self.assertIn("anvil", brother["candidates"])


class CliTest(unittest.TestCase):
    def test_cli_writes_json_and_does_not_touch_cast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cast_path = root / "cast.json"
            relations_path = root / "relations.json"
            records_path = root / "vol.jsonl"
            out_path = root / "graph.json"
            cast_path.write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
            relations_path.write_text(json.dumps(RELATIONS, ensure_ascii=False), encoding="utf-8")
            records_path.write_text(
                "\n".join(json.dumps({"ch": ch, "idx": index, "text": text})
                          for index, (ch, text) in enumerate(TEXTS)),
                encoding="utf-8",
            )
            before = cast_path.read_text(encoding="utf-8")
            code = alias_graph.main([
                "--cast", str(cast_path),
                "--relations", str(relations_path),
                "--records", str(records_path),
                "--json", str(out_path),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(cast_path.read_text(encoding="utf-8"), before)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["proposal_only"])
            self.assertGreaterEqual(payload["identity_count"], 5)
            self.assertTrue(any(item["term"] == "Saint" for item in payload["disambiguation"]))


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
