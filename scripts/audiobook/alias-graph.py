#!/usr/bin/env python3
"""从角色表与正文构建别名 / 亲属图（ADR-0008），只输出提议，不改角色表。

三件事：

  * **最长优先匹配**：`Soul Reaper` 命中后不再把里面的 `Reaper` 记一次；
    `Saint Tyris` 同样盖住单独的 `Saint`。
  * **同一身份合并**：角色表里多个 id 共用同一个别名时合并为一组；
    `Sunny / Sunless / Lost from Light` 这类本来就在同一 `aliases` 里的自然归并。
  * **消歧项**：`Saint` 这类既是阶级/头衔、又是一批角色别名组成部分的词，
    单独列出候选身份，不直接归给任何角色；同门称谓（`brother` / `sister`）
    与血亲、收养关系重叠时也单独列出。

输入与 `ss_audiobook/data/cast.json`、`relations.json` 的形状兼容。角色表的 `aliases` 是
正文匹配的来源（`cast.json` 的 `_meta` 要求它覆盖正文里的所有写法）；条目带 `name` 时
也会一并匹配。关系表没有显式名字的条目按 id 生成显示名，只用于展示，不凭空造别名。

    python3 scripts/audiobook/alias-graph.py \
        --cast ~/ss_audiobook/data/cast.json \
        --relations ~/ss_audiobook/data/relations.json \
        --records ~/ss_audiobook/data/vol6.jsonl \
        --json alias-graph.json

输出 JSON 里的 `identities`、`aliases`、`relations`、`disambiguation` 都是**提议**；
脚本从不写回 `--cast` 指向的文件，是否采纳由角色页/人工确认。
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

# 阶级/头衔词：它们出现在正文里时未必指某个具体角色，必须按上下文消歧。
TITLE_WORDS = (
    "Saint", "King", "Queen", "Prince", "Princess", "Lord", "Lady",
    "Sir", "Master", "Mistress", "Colonel", "Major", "General", "Captain",
)
WORD_EDGE = r"(?<![A-Za-z0-9])"
WORD_END = r"(?![A-Za-z0-9])"
TITLE_PATTERN = re.compile(
    WORD_EDGE + "(?:" + "|".join(TITLE_WORDS) + ")" + WORD_END,
)
KIN_WORDS = (
    "father", "mother", "son", "daughter", "brother", "sister",
    "uncle", "aunt", "nephew", "niece", "husband", "wife",
)


def load_json(path: Path, what: str) -> dict:
    """读 UTF-8 JSON；坏文件直接报路径，不静默当成空表。"""
    if not path.exists():
        raise SystemExit(f"找不到{what} {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} 不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"{what} {path} 顶层应该是对象，实际是 {type(payload).__name__}")
    return payload


def _alias_list(value: object) -> list[str]:
    """把 aliases 字段收敛成去重后的字符串数组，保持原始顺序。"""
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = [str(item) for item in value if str(item).strip()]
    else:
        raw = []
    seen: set[str] = set()
    names: list[str] = []
    for alias in raw:
        alias = alias.strip()
        if alias and alias not in seen:
            seen.add(alias)
            names.append(alias)
    return names


def _display_name(identity: str) -> str:
    """没有显式 name 时把 id 读成人名：shadow_saint → Shadow Saint。"""
    return " ".join(part.capitalize() for part in re.split(r"[_-]+", identity) if part)


def cast_entries(payload: dict) -> list[dict]:
    """兼容 {id: {...}}、{id: [...]}、{"characters": [...]} 三种角色表。"""
    entries: list[dict] = []
    characters = payload.get("characters")
    if isinstance(characters, list):
        for index, item in enumerate(characters):
            if not isinstance(item, dict):
                continue
            identity = str(item.get("id", "")).strip()
            if not identity:
                continue
            entries.append({
                "id": identity,
                "name": str(item.get("name") or item.get("canonical") or ""),
                "aliases": _alias_list(item.get("aliases")),
                "order": index,
                "in_cast": True,
            })
        return entries
    wrapped = payload.get("cast")
    source: dict = wrapped if isinstance(wrapped, dict) else payload
    for index, (identity, value) in enumerate(source.items()):
        if identity.startswith("_"):
            continue
        if isinstance(value, dict):
            entries.append({
                "id": identity,
                "name": str(value.get("name") or value.get("canonical") or ""),
                "aliases": _alias_list(value.get("aliases")),
                "order": index,
                "in_cast": True,
            })
        elif isinstance(value, list):
            entries.append({
                "id": identity,
                "name": "",
                "aliases": _alias_list(value),
                "order": index,
                "in_cast": True,
            })
    return entries


def relation_entries(payload: dict) -> list[dict]:
    """把 relations.json 里非下划线开头的条目收敛成同一形状。"""
    entries: list[dict] = []
    for index, (identity, value) in enumerate(payload.items()):
        if identity.startswith("_") or not isinstance(value, dict):
            continue
        entries.append({
            "id": identity,
            "name": str(value.get("name") or ""),
            "aliases": _alias_list(value.get("aliases")),
            "order": 10_000 + index,
            "in_cast": False,
            "family": value.get("family") if isinstance(value.get("family"), dict) else {},
            "figurative": value.get("figurative") if isinstance(value.get("figurative"), dict) else {},
            "clan": value.get("clan") if isinstance(value.get("clan"), str) else "",
        })
    return entries


class UnionFind:
    """并查集：共用别名的多个 id 视为同一身份。"""

    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def build_identities(cast: list[dict], relations: list[dict]) -> list[dict]:
    """按别名合并身份，返回按出现顺序排列的身份组。"""
    info: dict[str, dict] = {}
    for entry in cast + relations:
        identity = entry["id"]
        item = info.setdefault(identity, {
            "id": identity,
            "name": "",
            "aliases": [],
            "in_cast": False,
            "order": entry["order"],
        })
        if entry["name"] and not item["name"]:
            item["name"] = entry["name"]
        for alias in entry["aliases"]:
            if alias not in item["aliases"]:
                item["aliases"].append(alias)
        # cast 条目的 order 更小，优先作为组的规范 id。
        if entry["in_cast"]:
            item["in_cast"] = True
            item["order"] = min(item["order"], entry["order"])

    union = UnionFind(list(info))
    owners: dict[str, set[str]] = defaultdict(set)
    for identity, item in info.items():
        for alias in item["aliases"]:
            owners[alias].add(identity)
    for identities in owners.values():
        ordered = sorted(identities, key=lambda identity: (info[identity]["order"], identity))
        for other in ordered[1:]:
            union.union(ordered[0], other)

    groups: dict[str, list[str]] = defaultdict(list)
    for identity in info:
        groups[union.find(identity)].append(identity)
    merged: list[dict] = []
    for members in groups.values():
        members.sort(key=lambda identity: (not info[identity]["in_cast"], info[identity]["order"], identity))
        aliases: list[str] = []
        for member in members:
            for alias in info[member]["aliases"]:
                if alias not in aliases:
                    aliases.append(alias)
        name = next((info[member]["name"] for member in members if info[member]["name"]), "")
        merged.append({
            "id": members[0],
            "name": name or _display_name(members[0]),
            "aliases": aliases,
            "in_cast": any(info[member]["in_cast"] for member in members),
            "merged_from": members if len(members) > 1 else [],
        })
    merged.sort(key=lambda item: (not item["in_cast"], item["id"]))
    return merged


def alias_pattern(aliases: list[str]) -> re.Pattern[str] | None:
    """最长优先的别名正则；同一位置先试长别名，短别名不会重复计数。"""
    unique = sorted(set(aliases), key=lambda alias: (-len(alias), alias))
    if not unique:
        return None
    body = "|".join(re.escape(alias) for alias in unique)
    return re.compile(WORD_EDGE + "(?:" + body + ")" + WORD_END)


def text_rows(records_path: Path | None, text_path: Path | None) -> list[tuple[str, str]]:
    """读正文：JSONL 记录（取 ch/text）或纯文本文件，两者可同时给。"""
    rows: list[tuple[str, str]] = []
    if records_path is not None:
        if not records_path.exists():
            raise SystemExit(f"找不到记录文件 {records_path}")
        with records_path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SystemExit(f"{records_path}:{number} 不是合法 JSON：{error}") from error
                if not isinstance(row, dict) or "text" not in row:
                    raise SystemExit(f"{records_path}:{number} 缺少 text 字段")
                rows.append((str(row.get("ch", "")), str(row["text"])))
    if text_path is not None:
        if not text_path.exists():
            raise SystemExit(f"找不到正文文件 {text_path}")
        rows.append(("", text_path.read_text(encoding="utf-8")))
    return rows


def count_term(texts: list[tuple[str, str]], term: str) -> tuple[int, list[str]]:
    """按整词统计一个词的出现次数与章节；忽略大小写，用于 brother/sister 这类普通名词。"""
    pattern = re.compile(WORD_EDGE + re.escape(term) + WORD_END, re.IGNORECASE)
    count = 0
    chapters: set[str] = set()
    for chapter, text in texts:
        found = list(pattern.finditer(text))
        if found:
            count += len(found)
            if chapter:
                chapters.add(chapter)
    return count, sorted(chapters)


def match_text(
    texts: list[tuple[str, str]],
    pattern: re.Pattern[str] | None,
    alias_to_character: dict[str, str],
) -> tuple[dict[str, int], dict[str, set[str]], dict[str, int], dict[str, set[str]]]:
    """扫描正文，返回别名计数与未被别名覆盖的头衔计数。"""
    alias_counts: dict[str, int] = defaultdict(int)
    alias_chapters: dict[str, set[str]] = defaultdict(set)
    title_counts: dict[str, int] = defaultdict(int)
    title_chapters: dict[str, set[str]] = defaultdict(set)
    for chapter, text in texts:
        matches = list(pattern.finditer(text)) if pattern is not None else []
        covered = bytearray(len(text))
        for match in matches:
            alias = match.group(0)
            if alias not in alias_to_character:
                continue
            alias_counts[alias] += 1
            if chapter:
                alias_chapters[alias].add(chapter)
            covered[match.start():match.end()] = b"\x01" * (match.end() - match.start())
        for match in TITLE_PATTERN.finditer(text):
            if any(covered[match.start():match.end()]):
                continue
            term = match.group(0)
            title_counts[term] += 1
            if chapter:
                title_chapters[term].add(chapter)
    return alias_counts, alias_chapters, title_counts, title_chapters


def _targets(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def normalize_relation(value: object) -> str:
    """把 adopted_by / adopted-by 统一成小写连字符形式。"""
    return str(value).strip().replace("_", "-").lower()


def adoption_target(clan: str) -> str:
    """从 “Valor by adoption” / “Song (adopted daughter)” 里取被收养的家族名。"""
    match = re.match(r"([A-Za-z]+)", clan.strip())
    return match.group(1).lower() if match else ""


def relation_edges(payload: dict, identity_of: dict[str, str]) -> list[dict]:
    """把 family / figurative / 收养说明展平成有向边。"""
    edges: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(source: str, target: str, relation: str, sense: str, evidence: str = "") -> None:
        source_id = identity_of.get(source, source)
        target_id = identity_of.get(target, target)
        key = (source_id, target_id, relation, sense)
        if key in seen:
            return
        seen.add(key)
        edges.append({
            "from": source_id,
            "to": target_id,
            "relation": relation,
            "sense": sense,
            "evidence": evidence,
        })

    for identity, entry in payload.items():
        if identity.startswith("_") or not isinstance(entry, dict):
            continue
        for section, sense in (("family", "family"), ("figurative", "figurative")):
            mapping = entry.get(section)
            if not isinstance(mapping, dict):
                continue
            for relation, targets in mapping.items():
                for target in _targets(targets):
                    add(identity, target, normalize_relation(relation), sense)
        clan = entry.get("clan")
        if isinstance(clan, str) and "adopt" in clan.lower():
            target = adoption_target(clan)
            if target:
                add(identity, target, "adopted-by", "family", clan)
    edges.sort(key=lambda edge: (edge["from"], edge["to"], edge["relation"], edge["sense"]))
    return edges


def kin_disambiguation(
    payload: dict,
    identity_of: dict[str, str],
    texts: list[tuple[str, str]],
) -> list[dict]:
    """同门称谓与血亲/收养关系重叠时给出候选；不自动归人。"""
    address = payload.get("_clan_address")
    groups = payload.get("_groups")
    address = address if isinstance(address, dict) else {}
    groups = groups if isinstance(groups, dict) else {}
    senses: dict[str, set[str]] = defaultdict(set)
    candidates: dict[str, set[str]] = defaultdict(set)

    for identity, entry in payload.items():
        if identity.startswith("_") or not isinstance(entry, dict):
            continue
        for section, sense in (("family", "family"), ("figurative", "figurative")):
            mapping = entry.get(section)
            if not isinstance(mapping, dict):
                continue
            for relation, targets in mapping.items():
                term = normalize_relation(relation)
                if term not in KIN_WORDS:
                    continue
                senses[term].add(sense)
                for target in _targets(targets):
                    candidates[term].add(identity_of.get(target, target))

    clan_terms: set[str] = set()
    for clan, terms in address.items():
        group = groups.get(clan)
        if group is None:
            group = groups.get(str(clan).lower())
        members = group.get("members") if isinstance(group, dict) else None
        for term in _targets(terms):
            term = normalize_relation(term)
            if term not in KIN_WORDS:
                continue
            senses[term].add("clan")
            clan_terms.add(term)
            for member in _targets(members):
                candidates[term].add(identity_of.get(member, member))

    entries: list[dict] = []
    for term in sorted(senses):
        if "clan" not in senses[term]:
            continue
        count, chapters = count_term(texts, term)
        entries.append({
            "term": term,
            "kind": "kin-term",
            "reason": "同门称谓与血亲/收养关系重叠，按说话人上下文判定",
            "senses": sorted(senses[term]),
            "candidates": sorted(candidates[term]),
            "occurrences": count,
            "chapters": chapters,
        })
    return entries


def title_disambiguation(
    identities: list[dict],
    texts: list[tuple[str, str]],
    title_counts: dict[str, int],
    title_chapters: dict[str, set[str]],
    explicit: list[str],
) -> list[dict]:
    """头衔词 / 显式消歧词：列出候选身份与正文出现次数。"""
    candidates: dict[str, set[str]] = defaultdict(set)
    for identity in identities:
        words = set(re.findall(r"[A-Za-z]+", " ".join(identity["aliases"])))
        for word in TITLE_WORDS:
            if word in words:
                candidates[word].add(identity["id"])
    entries: list[dict] = []
    structural = {term for term, cands in candidates.items() if len(cands) >= 2}
    terms = set(title_counts) | {term for term in explicit if term.strip()} | structural
    for term in sorted(terms):
        term = term.strip()
        if not term:
            continue
        count = title_counts.get(term, 0)
        chapters = sorted(title_chapters.get(term, set()))
        if term in explicit and term not in title_counts:
            count, chapters = count_term(texts, term)
        entries.append({
            "term": term,
            "kind": "rank-or-role",
            "reason": "既是阶级/头衔，又是多条角色别名的组成部分",
            "candidates": sorted(candidates.get(term, set())),
            "occurrences": count,
            "chapters": chapters,
        })
    return entries


def build_graph(
    cast: list[dict],
    relations_payload: dict,
    texts: list[tuple[str, str]],
    explicit_terms: list[str],
) -> dict:
    """构建身份、别名计数、关系边与消歧项。"""
    relations = relation_entries(relations_payload)
    identities = build_identities(cast, relations)
    alias_to_character: dict[str, str] = {}
    for identity in identities:
        for alias in identity["aliases"]:
            alias_to_character[alias] = identity["id"]
    pattern = alias_pattern(list(alias_to_character))
    alias_counts, alias_chapters, title_counts, title_chapters = match_text(
        texts, pattern, alias_to_character,
    )

    alias_rows: list[dict] = []
    for identity in identities:
        total = 0
        for alias in identity["aliases"]:
            count = alias_counts.get(alias, 0)
            total += count
            alias_rows.append({
                "alias": alias,
                "character": identity["id"],
                "occurrences": count,
                "chapters": sorted(alias_chapters.get(alias, set())),
            })
        identity["occurrences"] = total
        identity["chapters"] = sorted({
            chapter
            for alias in identity["aliases"]
            for chapter in alias_chapters.get(alias, set())
        })
    alias_rows.sort(key=lambda row: (-row["occurrences"], row["alias"]))

    identity_of = {identity["id"]: identity["id"] for identity in identities}
    for identity in identities:
        for member in identity["merged_from"]:
            identity_of[member] = identity["id"]
    edges = relation_edges(relations_payload, identity_of)

    disambiguation = kin_disambiguation(relations_payload, identity_of, texts)
    disambiguation += title_disambiguation(
        identities, texts, title_counts, title_chapters, explicit_terms,
    )
    disambiguation.sort(key=lambda item: (item["kind"], item["term"]))

    return {
        "proposal_only": True,
        "identity_count": len(identities),
        "identities": identities,
        "aliases": alias_rows,
        "relations": edges,
        "disambiguation": disambiguation,
        "text_units": len(texts),
        "note": "只输出提议；本脚本不写回角色表，别名合并、关系边与消歧都要人工确认。",
    }


def print_graph(result: dict, cast_path: Path) -> None:
    print(
        f"别名图：{result['identity_count']} 个身份 / {len(result['aliases'])} 条别名 / "
        f"{len(result['relations'])} 条关系边 / {len(result['disambiguation'])} 个消歧项"
    )
    for identity in result["identities"]:
        aliases = "、".join(identity["aliases"]) or "（无别名）"
        merged = f"（合并 {len(identity['merged_from'])} 个 id）" if identity["merged_from"] else ""
        print(f"  {identity['id']:16} {identity['occurrences']:5} 次  {aliases}{merged}")
    for item in result["disambiguation"]:
        candidates = "、".join(item["candidates"]) or "无候选"
        print(
            f"  消歧 {item['term']:12} {item['occurrences']:5} 次  "
            f"{item['reason']} → {candidates}"
        )
    print(f"\n只输出提议，未写入 {cast_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从角色表与正文构建别名与亲属图（只产提议）")
    parser.add_argument("--cast", required=True, help="角色表 JSON（cast.json 形状）")
    parser.add_argument("--relations", default="", help="关系表 JSON（relations.json 形状）")
    parser.add_argument("--records", default="", help="正文 JSONL，逐行含 ch/text")
    parser.add_argument("--text", default="", help="纯文本正文；可与 --records 同时给")
    parser.add_argument("--ambiguous", action="append", default=[], help="额外要消歧的词，可重复")
    parser.add_argument("--json", default="", help="结果写入的 JSON 路径")
    args = parser.parse_args(argv)

    cast_path = Path(args.cast)
    relations_path = Path(args.relations) if args.relations else None
    records_path = Path(args.records) if args.records else None
    text_path = Path(args.text) if args.text else None
    texts = text_rows(records_path, text_path)
    relations_payload = load_json(relations_path, "关系表") if relations_path is not None else {}
    result = build_graph(cast_entries(load_json(cast_path, "角色表")), relations_payload, texts, args.ambiguous)
    result["cast"] = str(cast_path)
    result["relations_source"] = str(relations_path) if relations_path is not None else ""
    result["records"] = args.records
    result["text"] = args.text
    print_graph(result, cast_path)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"结果写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
