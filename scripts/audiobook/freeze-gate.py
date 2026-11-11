#!/usr/bin/env python3
"""冻结闸门：读锚点审计结果，按 ADR-0006 决定这批锚点能不能冻结。

只有**可绝对判定**的三类失败是阻断性的：

  * 锚点短于 1.2 s：指纹不稳，克隆出来会漂
  * 测不到可用的中位 F0：没有克隆所需的音高
  * 浊音比低于 0.55：实测的“气声”失败类（坏样本集中在 0.3 以下）

F0 与目标值的偏差、语速、2–4 kHz 能量比、音色余弦都**只提示**：嗓音圣经里的 f0_hz
可能是设计目标而非实测值，锚点逐字稿也未必与音频逐字对应。把提示当阻断会让本来可用的
锚点被误拒，所以这里只复述审计给出的 advisory，不据此拒绝冻结。

输入是 `anchor-audit.py --json` 写出的数组。脚本不改任何文件，只输出结论与逐条明细：

    python3 scripts/audiobook/freeze-gate.py anchors-audit.json
    python3 scripts/audiobook/freeze-gate.py anchors-audit.json --json freeze.json

退出码 0 表示允许冻结，1 表示拒绝（存在阻断项或审计为空）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

# 阈值只从审计工具取一份：两个脚本各写一个 1.2 / 0.55 迟早会漂开。
ANCHOR_AUDIT_PATH = Path(__file__).resolve().with_name("anchor-audit.py")


def load_anchor_audit():
    """按文件路径加载 anchor-audit.py；文件名带连字符，不能直接 import。"""
    spec = importlib.util.spec_from_file_location("anchor_audit", ANCHOR_AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载 {ANCHOR_AUDIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anchor_audit = load_anchor_audit()
MIN_DURATION_S = anchor_audit.MIN_DURATION_S
MIN_VOICED_FRAC = anchor_audit.VOICED_FRAC_MIN
F0_TOLERANCE = anchor_audit.F0_TOLERANCE
RATE_SLOW = anchor_audit.RATE_SLOW
RATE_FAST = anchor_audit.RATE_FAST


def load_audit(path: Path) -> list[dict]:
    """读审计 JSON；anchor-audit 的格式是数组，也接受 {"anchors": [...]} 包装。"""
    if not path.exists():
        raise SystemExit(f"找不到审计结果 {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} 不是合法 JSON：{error}") from error
    if isinstance(payload, dict):
        payload = payload.get("anchors", payload.get("files"))
    if not isinstance(payload, list):
        raise SystemExit(f"{path} 应该是锚点数组，实际是 {type(payload).__name__}")
    return [row for row in payload if isinstance(row, dict)]


def _number(value: object) -> float | None:
    """把 JSON 里的数字转成 float；缺失、布尔与非数字一律返回 None。"""
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


def evaluate_anchor(
    row: dict,
    min_duration: float = MIN_DURATION_S,
    min_voiced_frac: float = MIN_VOICED_FRAC,
) -> dict:
    """对一条锚点只检查三个阻断项；advisory 原样带出，不参与判定。"""
    duration = _number(row.get("duration_s"))
    f0 = _number(row.get("median_f0_hz"))
    voiced = _number(row.get("voiced_frac"))
    blocking: list[str] = []
    if duration is None or duration <= 0:
        blocking.append("缺少有效时长")
    elif duration < min_duration:
        blocking.append(f"锚点太短（{duration:.2f} s < {min_duration:g} s）")
    if f0 is None or f0 <= 0:
        blocking.append("无可用 F0")
    if voiced is None:
        blocking.append("缺少浊音比，无法判定是否气声")
    elif voiced < min_voiced_frac:
        blocking.append(f"浊音比 {voiced:.2f} < {min_voiced_frac:g}，听感是气声")
    # F0 与语速只进 advisory。审计结果通常已带这两条，这里补一层兜底，
    # 避免手工裁剪过的 JSON 把“提示”悄悄丢掉。
    advice = list(row.get("advisory") or [])
    expect = _number(row.get("expect_f0_hz"))
    if f0 and f0 > 0 and expect and expect > 0 and abs(f0 - expect) > expect * F0_TOLERANCE:
        if not any("F0" in item for item in advice):
            advice.append(f"中位 F0 {f0:.0f} Hz 与目标 {expect:.0f} Hz 相差超过 {F0_TOLERANCE:.0%}")
    rate = _number(row.get("words_per_second"))
    if rate is not None and rate < RATE_SLOW and not any("语速" in item for item in advice):
        advice.append(f"语速 {rate:.2f} w/s 低于 {RATE_SLOW}，可能拖沓")
    elif rate is not None and rate > RATE_FAST and not any("语速" in item for item in advice):
        advice.append(f"语速 {rate:.2f} w/s 高于 {RATE_FAST}，可能赶")
    return {
        "file": str(row.get("file", "")),
        "duration_s": duration,
        "median_f0_hz": f0 if f0 and f0 > 0 else None,
        "voiced_frac": voiced,
        "blocked": bool(blocking),
        "blocking": blocking,
        "advisory": advice,
    }


def gate(
    rows: list[dict],
    min_duration: float = MIN_DURATION_S,
    min_voiced_frac: float = MIN_VOICED_FRAC,
) -> dict:
    """汇总整批锚点；任何一条有阻断项，整批都不允许冻结。"""
    anchors = [evaluate_anchor(row, min_duration, min_voiced_frac) for row in rows]
    blocked = [item for item in anchors if item["blocked"]]
    advised = [item for item in anchors if item["advisory"] and not item["blocked"]]
    allowed = bool(anchors) and not blocked
    if not anchors:
        status = "red"
        reason = "审计结果为空，没有可冻结的锚点"
    elif blocked:
        status = "red"
        reason = f"{len(blocked)} 条锚点存在阻断项，拒绝冻结"
    else:
        status = "yellow" if advised else "green"
        reason = f"{len(anchors)} 条锚点通过阻断项"
        if advised:
            reason += f"；{len(advised)} 条仅有提示，不阻断"
    return {
        "allowed": allowed,
        "status": status,
        "reason": reason,
        "anchor_count": len(anchors),
        "blocked_count": len(blocked),
        "advisory_count": len(advised),
        "anchors": anchors,
    }


def print_gate(result: dict, source: Path) -> None:
    print(f"冻结闸门：{source}")
    for item in result["anchors"]:
        if item["blocked"]:
            mark = "✗ " + "；".join(item["blocking"])
        elif item["advisory"]:
            mark = "✓ (提示: " + "；".join(item["advisory"]) + ")"
        else:
            mark = "✓"
        print(f"  {item['file'] or '(未命名)':24} {mark}")
    print(
        f"\n{result['anchor_count']} 条锚点，阻断 {result['blocked_count']}，"
        f"仅提示 {result['advisory_count']}；灯色 {result['status']}"
    )
    print(f"结论：{'允许冻结' if result['allowed'] else '拒绝冻结'}——{result['reason']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结音色前按审计结果做最终判定")
    parser.add_argument("audit", help="anchor-audit.py --json 的输出文件")
    parser.add_argument("--json", default="", help="结论写入的 JSON 路径")
    parser.add_argument(
        "--min-duration", type=float, default=MIN_DURATION_S,
        help=f"最短锚点秒数（默认 {MIN_DURATION_S:g}，ADR-0006）",
    )
    parser.add_argument(
        "--min-voiced-frac", type=float, default=MIN_VOICED_FRAC,
        help=f"最低浊音比（默认 {MIN_VOICED_FRAC:g}，ADR-0006）",
    )
    args = parser.parse_args(argv)

    source = Path(args.audit)
    result = gate(load_audit(source), args.min_duration, args.min_voiced_frac)
    print_gate(result, source)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"结论写入 {args.json}")
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
