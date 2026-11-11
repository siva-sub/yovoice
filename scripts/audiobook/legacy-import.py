#!/usr/bin/env python3
"""把脚本时代的归因成果导入成 yovoice 的故事作品。

输入（都在 ss_audiobook/data 下）：
  records  vol6.jsonl            每行 {kind, speaker, conf, evidence, text, ch, idx}
  bible    voice_bible_v5.json   每个角色的音色设计与锚点
  gate     attribution_state.json 每章的闸门状态与正文 sha

输出（--out 目录）：
  works/work-<章节>[-<段>].json   yovoice 的 Draft（kind=story），可直接导入
  gate/<作品 id>.json             闸门与逐条证据的边文件
  manifest.json                   清单：作品数、cue 数、复核队列

为什么要它：重新导入 epub 会丢掉 170 章已完成的归因，而那是整条流程里最贵的人工。
这个脚本让界面直接开在"已完成"的归因上，只把需要复核的 4% 交回给人。

    python3 scripts/audiobook/legacy-import.py \
        --records ~/ss_audiobook/data/vol6.jsonl \
        --bible   ~/ss_audiobook/data/voice_bible_v5.json \
        --gate    ~/ss_audiobook/data/attribution_state.json \
        --out     /tmp/audiobook-import
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

# 与前端 features/create/manuscript.ts 的上限保持一致，超出就拆段。
PART_TEXT_LIMIT = 11000
PART_CUE_LIMIT = 1800
# 还没有音频时给 cue 的合成时间轴：每条 1.5 秒、间隔 0.3 秒；真实时长由生成结果覆盖。
CUE_MS = 1500
CUE_GAP_MS = 300
REVIEW_CONFIDENCE = 0.5
DEFAULT_MODEL = "breeze-tts-2-q8"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number} 不是合法 JSON：{error}") from error
    return rows


def safe_name(value: str) -> str:
    """把章节 id 收敛成安全的文件名，避免数据里的斜杠或点号把写入带出输出目录。"""
    cleaned = "".join(char if char.isalnum() or char in ".-_" else "_" for char in value)
    cleaned = cleaned.strip(".")
    return cleaned or "unnamed"


def text_sha(text: str) -> str:
    """与 attribution_gate.py 一致：对整章正文取 sha256 前 16 位。

    本卷 170 章的哈希全部与原闸门记录逐字相等，因此 `verified` 状态可以随导入一起转移，
    而不是当作“信任旧数据”盲目继承。改动正文拼接方式会立刻让全部章节失效。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def speaker_settings(bible: dict, speaker: str) -> dict:
    """把嗓音圣经的一项翻成 yovoice 的 SynthesisSettings。

    默认 clone-only、guidance 1.0（指令是音色漂移的主因，见 ADR-0003）；
    design 文本只作为音色描述保留，不会自动变成逐句指令。
    """
    entry = bible.get(speaker) or {}
    performance = entry.get("performance") or ""
    description = entry.get("design") or ""
    settings = {
        "modelId": DEFAULT_MODEL,
        "mode": "clone",
        "voiceMode": "clone",
        "voiceDescription": description,
        "guidanceScale": 1.0,
        "referenceText": entry.get("anchor_line") or entry.get("anchor_passage") or "",
    }
    # 有明确表演指导时才带 instruction，且只作为角色级默认，逐句覆盖留给界面。
    if performance:
        settings["instruction"] = performance
    return settings


def split_parts(chapter: str, cues: list[dict]) -> list[list[dict]]:
    """按前端上限切段：先按 cue 数，再按累计字符数。"""
    parts: list[list[dict]] = []
    current: list[dict] = []
    length = 0
    for cue in cues:
        over_lines = len(current) >= PART_CUE_LIMIT
        over_chars = length + len(cue["text"]) > PART_TEXT_LIMIT and current
        if over_lines or over_chars:
            parts.append(current)
            current, length = [], 0
        current.append(cue)
        length += len(cue["text"])
    if current:
        parts.append(current)
    return parts


def build_chapter(chapter: str, rows: list[dict], bible: dict, gate: dict) -> list[dict]:
    """一章 → 若干作品；每个作品都带自己的闸门与证据。"""
    rows = sorted(rows, key=lambda row: row["idx"])
    speakers: dict[str, dict] = {}
    cues: list[dict] = []
    for row in rows:
        speaker = row["speaker"]
        if speaker not in speakers:
            speakers[speaker] = {
                "id": speaker,
                "sourceName": "旁白" if speaker == "narrator" else speaker,
                "characterId": speaker,
                "settings": speaker_settings(bible, speaker),
            }
        cues.append({
            "id": f"{chapter}-{row['idx']}",
            "start": 0,
            "end": 0,
            "text": row["text"],
            "speakerId": speaker,
            "confidence": row.get("conf"),
            "evidence": row.get("evidence", ""),
            "kind": row.get("kind", "narration"),
        })

    state = gate.get(chapter, {})
    chapter_text = "\n".join(cue["text"] for cue in cues)
    parts = split_parts(chapter, cues)
    works = []
    offset = 0
    for index, part in enumerate(parts, 1):
        work_id = f"chapter-{chapter}" if len(parts) == 1 else f"chapter-{chapter}-p{index}"
        # 合成时间轴：从 0 起逐条推进，保证界面里能排序、能选中，但不假装是真实时长。
        cursor = 0
        for cue in part:
            cue["start"] = cursor
            cue["end"] = cursor + CUE_MS
            cursor += CUE_MS + CUE_GAP_MS
        used = {cue["speakerId"] for cue in part}
        works.append({
            "id": work_id,
            "draft": {
                "id": work_id,
                "title": f"第 {chapter} 章" if len(parts) == 1 else f"第 {chapter} 章（{index}/{len(parts)}）",
                "kind": "story",
                "text": "\n".join(cue["text"] for cue in part),
                "subtitles": {
                    "speakers": [entry for key, entry in speakers.items() if key in used],
                    "cues": part,
                },
                "modelId": DEFAULT_MODEL,
            },
            "gate": {
                "chapter": chapter,
                "part": index,
                "parts": len(parts),
                "status": state.get("status", "pending"),
                "textSha": text_sha(chapter_text),
                "recordedSha": state.get("text_sha", ""),
                "note": state.get("note", ""),
                "flags": state.get("flags", 0),
                "lines": len(part),
                "offset": offset,
                "evidence": {
                    cue["id"]: {
                        "confidence": cue.get("confidence"),
                        "evidence": cue.get("evidence", ""),
                        "kind": cue.get("kind", "narration"),
                    }
                    for cue in part
                },
            },
        })
        offset += len(part)
    return works


def main() -> int:
    parser = argparse.ArgumentParser(description="把脚本时代的归因导入 yovoice 故事作品")
    parser.add_argument("--records", required=True, help="vol6.jsonl 等记录文件")
    parser.add_argument("--bible", required=True, help="voice_bible_v5.json")
    parser.add_argument("--gate", default="", help="attribution_state.json，缺省时全部记 pending")
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    records = Path(args.records)
    if not records.exists():
        raise SystemExit(f"找不到记录文件 {records}")
    rows = load_jsonl(records)
    bible = json.loads(Path(args.bible).read_text(encoding="utf-8")) if Path(args.bible).exists() else {}
    gate = json.loads(Path(args.gate).read_text(encoding="utf-8")) if args.gate and Path(args.gate).exists() else {}

    by_chapter: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_chapter[row["ch"]].append(row)

    out = Path(args.out)
    (out / "works").mkdir(parents=True, exist_ok=True)
    (out / "gate").mkdir(parents=True, exist_ok=True)

    works_count = cue_count = review = split_parts_count = 0
    reviewed: list[dict] = []
    for chapter in sorted(by_chapter, key=lambda value: int(value)):
        works = build_chapter(chapter, by_chapter[chapter], bible, gate)
        if len(works) > 1:
            split_parts_count += 1
        for work in works:
            stem = safe_name(work["id"])
            target = out / "works" / f"work-{stem}.json"
            target.write_text(json.dumps(work["draft"], ensure_ascii=False, indent=2), encoding="utf-8")
            side = out / "gate" / f"{stem}.json"
            side.write_text(json.dumps(work["gate"], ensure_ascii=False, indent=2), encoding="utf-8")
            works_count += 1
            cue_count += len(work["draft"]["subtitles"]["cues"])
            for cue in work["draft"]["subtitles"]["cues"]:
                confidence = cue.get("confidence")
                if isinstance(confidence, (int, float)) and confidence < REVIEW_CONFIDENCE:
                    review += 1
                    reviewed.append({
                        "work": work["id"], "cue": cue["id"], "speaker": cue["speakerId"],
                        "confidence": confidence, "evidence": cue.get("evidence", ""),
                        "text": cue["text"][:120],
                    })

    manifest = {
        "source": str(records),
        "bible": str(args.bible),
        "gate": str(args.gate) if args.gate else "",
        "works": works_count,
        "chapters": len(by_chapter),
        "chapters_split": split_parts_count,
        "cues": cue_count,
        "records": len(rows),
        "review_queue": review,
        "gate_status": sorted({work_status for work_status in (gate.get(ch, {}).get("status", "pending") for ch in by_chapter)}),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 复核队列单独落一份，界面导入前的分诊和测试都读它。
    (out / "review-queue.json").write_text(json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if cue_count != len(rows):
        print(f"错误：cue 数 {cue_count} 与记录数 {len(rows)} 不一致", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
