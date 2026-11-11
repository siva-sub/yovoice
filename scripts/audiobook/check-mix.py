#!/usr/bin/env python3
"""混音参数检查（ADR-0004）：三层电平、侧链深度与 hit 前移，违规则拒绝。

检查的是**混音配置/时间线**，不是渲染后的音频：

  * ambience  −30…−25 dB
  * bed       −23…−20 dB
  * hit       −16…−8 dB
  * 侧链      ≥ 16 dB
  * hit 前移  约自身长度的 40%（容差 ±10 个百分点，即 30%…50%）

输入可以是一份**混音配置**，也可以是若干份旧数据文件拼起来的配置。兼容的形状：

  * `places`（places.json）：每条按 ambience 检查
  * `cues`（sfx.json）：按 `role` 检查 gain
  * `placements`：混音后的放置结果，含 `start_s` / `anchor_s`，用于核对 hit 前移
  * `ambience` / `bed` / `hit` 数组：直接给出角色与 gain
  * `tracks`：yovoice 的轨道，读 `role`、`gainDb`、`duckDb`
  * `sidechain_db` / `duck_db`（或 camelCase）：侧链深度

没有放置结果时，hit 前移只提示、不判违规——单看 `sfx.json` 无法知道 cue 被放在第几秒。
要核对前移，给 `placements`，或在 hit 上同时给 `seconds` 与 `start_s` / `anchor_s`。

    python3 scripts/audiobook/check-mix.py mix.json
    python3 scripts/audiobook/check-mix.py places.json sfx.json --json check.json

退出码 0 表示全部通过，1 表示存在违规项（逐条列出）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# 电平区间来自 ADR-0004 的实测表；区间包含端点。
RANGES = {
    "ambience": (-30.0, -25.0),
    "bed": (-23.0, -20.0),
    "hit": (-16.0, -8.0),
}
SIDECHAIN_MIN_DB = 16.0
HIT_LEAD_RATIO = 0.40
HIT_LEAD_TOLERANCE = 0.10
EPS = 1e-9


def load_config(path: Path) -> dict:
    """读混音配置；坏文件直接报路径。"""
    if not path.exists():
        raise SystemExit(f"找不到混音配置 {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} 不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} 顶层应该是对象，实际是 {type(payload).__name__}")
    return payload


def _number(value: object) -> float | None:
    """把数字或数字字符串转成 float；布尔、缺失与其他类型返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _first_number(values: list[object]) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def normalize_role(value: object) -> str:
    """角色名收敛：beds→bed、hits→hit、ambiences→ambience。"""
    role = str(value or "").strip().lower()
    return {
        "ambiences": "ambience",
        "beds": "bed",
        "hits": "hit",
    }.get(role, role)


def _entry_from_raw(raw: dict, default_role: str, source: str, index: int) -> dict:
    """把一条 cue / place / placement 收敛成统一的检查项。"""
    cue_value = raw.get("cue")
    cue: dict = cue_value if isinstance(cue_value, dict) else {}
    role = normalize_role(raw.get("role") or raw.get("kind") or default_role or cue.get("role"))
    identity = str(raw.get("id") or raw.get("cue_id") or cue.get("id") or f"item-{index}")
    return {
        "id": identity,
        "source": source,
        "role": role,
        "gain_db": _first_number([raw.get("gain_db"), raw.get("gainDb"), cue.get("gain_db")]),
        "seconds": _first_number([
            raw.get("seconds"), raw.get("length_s"), raw.get("duration_s"),
            raw.get("duration"), cue.get("seconds"),
        ]),
        "lead_seconds": _number(raw.get("lead_seconds")),
        "start_s": _first_number([raw.get("start_s"), raw.get("start")]),
        "anchor_s": _first_number([
            raw.get("anchor_s"), raw.get("anchor_start_s"),
            raw.get("line_start_s"), raw.get("anchor"),
        ]),
    }


def _list_of(value: object) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def collect_items(config: dict, source: str) -> list[dict]:
    """从一份配置里收集要检查的层；有 placements 时以放置结果为准。"""
    items: list[dict] = []
    placements = config.get("placements")
    if isinstance(placements, list):
        for index, raw in enumerate(placements):
            if isinstance(raw, dict):
                items.append(_entry_from_raw(raw, "", source, index))
    else:
        for index, raw in enumerate(_list_of(config.get("places"))):
            items.append(_entry_from_raw(raw, "ambience", source, index))
        for index, raw in enumerate(_list_of(config.get("cues"))):
            items.append(_entry_from_raw(raw, "", source, index))
        for key, role in (
            ("ambience", "ambience"), ("ambiences", "ambience"),
            ("bed", "bed"), ("beds", "bed"),
            ("hit", "hit"), ("hits", "hit"),
            ("score", "score"),
        ):
            for index, raw in enumerate(_list_of(config.get(key))):
                items.append(_entry_from_raw(raw, role, source, index))
    for index, track in enumerate(_list_of(config.get("tracks"))):
        role = normalize_role(track.get("role"))
        if not role:
            continue
        items.append(_entry_from_raw(track, role, source, index))
    return items


def check_gains(items: list[dict]) -> list[dict]:
    """逐项核对电平区间；未知角色登记为跳过，不误报。"""
    results: list[dict] = []
    for item in items:
        role = item["role"]
        if role not in RANGES:
            results.append({**item, "checked": False, "ok": None, "reason": "角色不在本次检查范围"})
            continue
        low, high = RANGES[role]
        gain = item["gain_db"]
        if gain is None:
            results.append({
                **item, "checked": True, "ok": False, "range": [low, high],
                "reason": "缺少 gain_db，无法核对电平",
            })
            continue
        results.append({
            **item,
            "checked": True,
            "ok": low - EPS <= gain <= high + EPS,
            "range": [low, high],
            "reason": "" if low - EPS <= gain <= high + EPS else f"{role} 应在 {low:g}…{high:g} dB",
        })
    return results


def check_hit_leads(items: list[dict]) -> list[dict]:
    """核对 hit 前移比例；缺时间信息只提示，章首钳制视为可接受但给出提示。"""
    results: list[dict] = []
    for item in items:
        if item["role"] != "hit":
            continue
        seconds = item["seconds"]
        lead = item["lead_seconds"]
        if lead is None and seconds is not None and item["start_s"] is not None and item["anchor_s"] is not None:
            lead = item["anchor_s"] - item["start_s"]
        if lead is None or seconds is None or seconds <= 0:
            results.append({
                "id": item["id"], "source": item["source"],
                "ratio": None, "ok": None,
                "reason": "缺少放置时间或长度，无法核对前移",
            })
            continue
        ratio = lead / seconds
        low = HIT_LEAD_RATIO - HIT_LEAD_TOLERANCE
        high = HIT_LEAD_RATIO + HIT_LEAD_TOLERANCE
        clamped = (
            item["start_s"] is not None
            and item["start_s"] <= EPS
            and 0.0 <= lead < HIT_LEAD_RATIO * seconds - EPS
        )
        ok = clamped or (low - EPS <= ratio <= high + EPS)
        results.append({
            "id": item["id"],
            "source": item["source"],
            "seconds": round(seconds, 3),
            "lead_seconds": round(lead, 3),
            "ratio": round(ratio, 3),
            "range": [round(low, 3), round(high, 3)],
            "clamped": clamped,
            "ok": ok,
            "reason": (
                "章首钳制，前移不足 40% 但已贴到 0 s" if clamped
                else ("" if ok else f"hit 前移 {ratio:.0%}，应在 {low:.0%}…{high:.0%}")
            ),
        })
    return results


def check_sidechain(configs: list[dict]) -> list[dict]:
    """收集所有声明过的侧链深度；没有声明是一项违规。"""
    values: list[dict] = []
    for config in configs:
        for key in ("sidechain_db", "sidechainDb", "duck_db", "duckDb"):
            db = _number(config.get(key))
            if db is not None:
                values.append({"source": key, "db": db, "ok": db >= SIDECHAIN_MIN_DB - EPS})
        for index, track in enumerate(_list_of(config.get("tracks"))):
            for key in ("duckDb", "duck_db", "sidechain_db", "sidechainDb"):
                db = _number(track.get(key))
                if db is not None:
                    track_id = str(track.get("id") or index)
                    values.append({"source": f"track:{track_id}", "db": db, "ok": db >= SIDECHAIN_MIN_DB - EPS})
    return values


def evaluate(configs: list[dict]) -> dict:
    """汇总电平、hit 前移与侧链；violations 为空才算通过。"""
    items: list[dict] = []
    for config in configs:
        source = str(config.get("_source", "config"))
        items.extend(collect_items(config, source))
    gains = check_gains(items)
    leads = check_hit_leads(items)
    sidechain = check_sidechain(configs)

    violations: list[dict] = []
    for row in gains:
        if row["checked"] and not row["ok"]:
            violations.append({
                "item": row["id"], "source": row["source"], "role": row["role"],
                "field": "gain_db", "value": row["gain_db"], "rule": row["reason"],
            })
    for row in leads:
        if row["ok"] is False:
            violations.append({
                "item": row["id"], "source": row["source"], "role": "hit",
                "field": "lead_ratio", "value": row["ratio"], "rule": row["reason"],
            })
    if not sidechain:
        violations.append({
            "item": "sidechain", "source": "", "role": "effect-bus",
            "field": "sidechain_db", "value": None,
            "rule": f"缺少侧链深度；应在配置里写 sidechain_db 或轨道 duckDb，且 ≥ {SIDECHAIN_MIN_DB:g} dB",
        })
    for row in sidechain:
        if not row["ok"]:
            violations.append({
                "item": "sidechain", "source": row["source"], "role": "effect-bus",
                "field": "sidechain_db", "value": row["db"],
                "rule": f"侧链 {row['db']:g} dB < {SIDECHAIN_MIN_DB:g} dB",
            })

    warnings: list[str] = []
    for row in leads:
        if row["ok"] is None:
            warnings.append(f"{row['id']}：{row['reason']}")
        elif row["clamped"]:
            warnings.append(f"{row['id']}：{row['reason']}，请人工确认")
    warnings.extend(
        f"{row['id']} 的 {row['role']} 不在检查范围（只检查 ambience/bed/hit）"
        for row in gains if not row["checked"] and row["role"]
    )
    return {
        "ok": not violations,
        "checked_items": sum(1 for row in gains if row["checked"]),
        "violations": violations,
        "warnings": warnings,
        "items": gains,
        "hit_leads": leads,
        "sidechain": sidechain,
    }


def print_result(result: dict) -> None:
    for row in result["items"]:
        if not row["checked"]:
            print(f"  {row['id']:24} {row['role'] or '?':10} 跳过（角色不在检查范围）")
            continue
        gain = f"{row['gain_db']:+.0f} dB" if row["gain_db"] is not None else "缺 gain"
        mark = "✓" if row["ok"] else "✗"
        low, high = row["range"]
        print(f"  {row['id']:24} {row['role']:10} {gain:>10}  [{low:g}…{high:g}] {mark}")
    for row in result["sidechain"]:
        mark = "✓" if row["ok"] else "✗"
        print(f"  侧链 {row['source']:16} {row['db']:g} dB  ≥ {SIDECHAIN_MIN_DB:g} {mark}")
    for row in result["hit_leads"]:
        if row["ok"] is None:
            print(f"  hit {row['id']:22} 前移未知：{row['reason']}")
        else:
            mark = "✓" if row["ok"] else "✗"
            print(
                f"  hit {row['id']:22} 前移 {row['lead_seconds']:g}/{row['seconds']:g} s "
                f"= {row['ratio']:.0%}  [30%…50%] {mark}"
            )
    for warning in result["warnings"]:
        print(f"  提示：{warning}")
    if result["violations"]:
        print("\n违规项：")
        for violation in result["violations"]:
            value = "—" if violation["value"] is None else f"{violation['value']}"
            print(
                f"  ✗ {violation['item']} [{violation['source'] or '?'}] "
                f"{violation['field']}={value}：{violation['rule']}"
            )
        print(f"\n结论：拒绝（{len(result['violations'])} 项违规）")
    else:
        print("\n结论：通过")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 ADR-0004 检查混音电平、侧链与 hit 前移")
    parser.add_argument("configs", nargs="+", help="混音配置 / places.json / sfx.json")
    parser.add_argument("--json", default="", help="结果写入的 JSON 路径")
    args = parser.parse_args(argv)

    configs: list[dict] = []
    for name in args.configs:
        config = load_config(Path(name))
        config["_source"] = name
        configs.append(config)
    result = evaluate(configs)
    result["configs"] = args.configs
    print_result(result)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"结果写入 {args.json}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
