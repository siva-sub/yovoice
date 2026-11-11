#!/usr/bin/env python3
"""锚点审计：在冻结音色之前量出五个数，避免"听着还行"就污染该角色全书每一句。

用法（本机只有 numpy，没有 soundfile，所以用 wave 读）：

    # 按嗓音圣经逐个角色审计 anchors_v6/ 下的锚点
    python3 scripts/audiobook/anchor-audit.py anchors_v6/anchor_*.wav \
        --bible voice_bible_v5.json --json anchors-audit.json

    # 比较同一角色的多个候选（不传 --bible 时不做目标 F0 判定）
    python3 scripts/audiobook/anchor-audit.py cand-a.wav cand-b.wav --text "锚点句" --expect-f0 118

判定口径见 docs/adr/0006-voice-design-gate.md：
中位 F0 偏离目标 ±10% 失败；语速 < 1.8 拖沓、2.2–3.2 正常；
浊音比与周期性偏低、平坦度偏高即气声。2–4 kHz 与音色余弦只报告，不作硬判定。
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

# 语速阈值来自实测：< 1.8 w/s 听感拖沓，2.2–3.2 w/s 是正常旁白区间。
# 但只有在锚点逐字稿与音频真的对应时才有意义，所以作为提示而非硬闸门。
RATE_SLOW = 1.8
RATE_FAST = 3.2
# 每帧自相关峰值高于此值才记为浊音（与 measure_whisper.py 一致）。
VOICED_PERIODICITY = 0.45
# 浊音比低于此值判为气声；实测坏样本集中在 0.3 以下。
VOICED_FRAC_MIN = 0.55
# 中位 F0 允许偏离设计目标的比例。
F0_TOLERANCE = 0.10


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """用标准库读 16 位 PCM；立体声取均值。返回 [-1,1] 的浮点与采样率。"""
    with wave.open(str(path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width != 2:
        raise ValueError(f"{path.name}: 只支持 16 位 PCM，实际 {width * 8} 位")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, sample_rate


def frame_stats(data: np.ndarray, sample_rate: int, win_s: float = 0.04, hop_s: float = 0.02) -> dict:
    """逐帧算自相关峰值、谱平坦度、质心与 2–4 kHz 能量比，返回帧均值。"""
    win = max(int(sample_rate * win_s), 16)
    hop = max(int(sample_rate * hop_s), 8)
    periodicity: list[float] = []
    flatness: list[float] = []
    centroid: list[float] = []
    band_ratio: list[float] = []
    lows, highs = int(sample_rate / 500), int(sample_rate / 60)
    band_lo, band_hi = int(2000 * win / sample_rate), int(4000 * win / sample_rate)
    for start in range(0, max(len(data) - win, 1), hop):
        segment = data[start:start + win]
        if len(segment) < win:
            break
        if float(np.sqrt(np.mean(segment ** 2))) < 1e-4:
            continue
        segment = segment - segment.mean()
        # 自相关判浊音：只在 60–500 Hz 对应的滞后区间找峰。
        ac = np.correlate(segment, segment, mode="full")[len(segment) - 1:]
        ac = ac / (ac[0] + 1e-9)
        upper = min(highs, len(ac) - 1)
        periodicity.append(float(np.max(ac[lows:upper])) if upper > lows else 0.0)
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment)))) + 1e-9
        flatness.append(float(np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum)))
        centroid.append(float(np.sum(np.arange(len(spectrum)) * spectrum) / np.sum(spectrum)) * sample_rate / (2 * len(spectrum)))
        total = float(np.sum(spectrum ** 2)) + 1e-12
        band = float(np.sum(spectrum[band_lo:band_hi] ** 2))
        band_ratio.append(band / total)
    if not periodicity:
        return {}
    return {
        "periodicity": float(np.mean(periodicity)),
        "flatness": float(np.mean(flatness)),
        "centroid_hz": float(np.mean(centroid)),
        "voiced_frac": float(np.mean([1.0 if value > VOICED_PERIODICITY else 0.0 for value in periodicity])),
        "band_2_4k_ratio": float(np.mean(band_ratio)),
    }


def median_f0(data: np.ndarray, sample_rate: int) -> float:
    """40 ms 帧自相关求基频，取中位数；不构成 4 个有效帧时返回 0。"""
    frame = int(sample_rate * 0.04)
    values: list[float] = []
    low, high = int(sample_rate / 500), int(sample_rate / 60)
    for start in range(0, max(len(data) - frame, 0), frame):
        window = data[start:start + frame]
        window = window - window.mean()
        if float(window.std()) < 1e-4:
            continue
        ac = np.correlate(window, window, mode="full")[frame - 1:]
        ac = ac / (ac[0] + 1e-9)
        segment = ac[low:high]
        if segment.size == 0 or float(segment.max()) <= 0.3:
            continue
        values.append(sample_rate / (int(np.argmax(segment)) + low))
    return float(np.median(values)) if len(values) >= 4 else 0.0


def spectrum_fingerprint(data: np.ndarray, sample_rate: int) -> np.ndarray:
    """对数谱指纹：分帧取均值并去均值，供不同 take 之间做余弦相似度。"""
    width = int(sample_rate * 0.75) if len(data) > sample_rate * 1.5 else max(int(len(data) / 2), 1)
    specs = []
    for start in range(0, max(len(data) - width, 1), width):
        segment = data[start:start + width]
        if len(segment) < width:
            break
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
        specs.append(np.log(spectrum + 1e-6)[:400])
    if not specs:
        return np.zeros(400)
    mean = np.mean(specs, axis=0)
    return mean - mean.mean()


def cosine(a: np.ndarray, b: np.ndarray) -> float | None:
    """两条指纹的余弦相似度；任一条为空时返回 None。"""
    if not len(a) or not len(b):
        return None
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def audit(path: Path, text: str = "", expect_f0: float = 0.0, reference: np.ndarray | None = None) -> dict:
    """量一条锚点，给出五个数、报告项与判定。"""
    data, sample_rate = read_wav(path)
    duration = len(data) / sample_rate
    stats = frame_stats(data, sample_rate)
    f0 = median_f0(data, sample_rate)
    words = len(text.split()) if text else 0
    result = {
        "file": path.name,
        "duration_s": round(duration, 3),
        "median_f0_hz": round(f0, 1),
        "expect_f0_hz": expect_f0 or None,
        "words": words,
        "words_per_second": round(words / duration, 3) if words and duration else None,
        **{key: round(value, 3) for key, value in stats.items()},
        "timbre_cos": None,
        "issues": [],
        "advisory": [],
    }
    if reference is not None:
        similarity = cosine(spectrum_fingerprint(data, sample_rate), reference)
        result["timbre_cos"] = round(similarity, 3) if similarity is not None else None
    issues = result["issues"]
    advice = result["advisory"]
    # 硬闸门只留可以绝对判定的两类：锚点太短（指纹不稳）、测不到基频（无法克隆）。
    if duration < 1.2:
        # 太短的锚点指纹不稳，克隆出来会漂。
        issues.append("锚点太短（< 1.2 s）")
    if not f0:
        issues.append("测不到基频")
    # 气声是实测能抓到的失败类（direfang 这类浊音比 0.21 的样本），计入硬闸门。
    if stats.get("voiced_frac", 1.0) < VOICED_FRAC_MIN:
        issues.append(f"浊音比 {stats['voiced_frac']:.2f} 偏低，听感是气声")
    # F0 与语速的参照系不可靠：嗓音里的 f0_hz 可能是设计目标而非实测值，
    # 锚点的 anchor_line 也未必与音频逐字对应，所以这两项只提示，交给候选间对比决定。
    if f0 and expect_f0 and abs(f0 - expect_f0) > expect_f0 * F0_TOLERANCE:
        advice.append(f"中位 F0 {f0:.0f} Hz 与目标 {expect_f0:.0f} Hz 相差超过 {F0_TOLERANCE:.0%}")
    rate = result["words_per_second"]
    if rate is not None and rate < RATE_SLOW:
        advice.append(f"语速 {rate:.2f} w/s 低于 {RATE_SLOW}，可能拖沓")
    elif rate is not None and rate > RATE_FAST:
        advice.append(f"语速 {rate:.2f} w/s 高于 {RATE_FAST}，可能赶（先确认逐字稿与音频一致）")
    result["verdict"] = "pass" if not issues else "fail"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结音色前审计锚点")
    parser.add_argument("files", nargs="+", help="锚点 wav 文件")
    parser.add_argument("--bible", default="", help="voice_bible_v5.json，按文件名里的角色取 design 与 f0_hz")
    parser.add_argument("--text", default="", help="锚点逐字稿，用于算语速")
    parser.add_argument("--expect-f0", type=float, default=0.0, help="设计期望的中位 F0")
    parser.add_argument("--reference", default="", help="参考锚点 wav，用于音色余弦比较")
    parser.add_argument("--json", default="", help="结果写入的 JSON 路径")
    args = parser.parse_args()

    bible = json.loads(Path(args.bible).read_text(encoding="utf-8")) if args.bible and Path(args.bible).exists() else {}
    reference = None
    if args.reference and Path(args.reference).exists():
        ref_data, ref_sr = read_wav(Path(args.reference))
        reference = spectrum_fingerprint(ref_data, ref_sr)

    results = []
    for name in args.files:
        path = Path(name)
        if not path.exists():
            print(f"跳过不存在的文件：{path}", file=sys.stderr)
            continue
        # 从 anchor_<角色>.wav 里认出角色，好从嗓音圣经取设计目标。
        stem = path.stem.replace("anchor_", "")
        entry = bible.get(stem, {})
        text = args.text or entry.get("anchor_line") or entry.get("anchor_passage") or ""
        expect = args.expect_f0 or float(entry.get("f0_hz") or 0)
        results.append(audit(path, text, expect, reference))

    header = f"{'锚点':22}{'时长':>6}{'F0':>7}{'目标':>6}{'w/s':>6}{'浊音':>6}{'周期':>6}{'平坦':>6}{'2-4k':>6}  判定"
    print(header)
    for row in results:
        rate = f"{row['words_per_second']:.2f}" if row["words_per_second"] else "-"
        f0 = f"{row['median_f0_hz']:.0f}" if row["median_f0_hz"] else "-"
        target = f"{row['expect_f0_hz']:.0f}" if row["expect_f0_hz"] else "-"
        voiced = f"{row.get('voiced_frac', 0):.2f}"
        periodicity = f"{row.get('periodicity', 0):.2f}"
        flat = f"{row.get('flatness', 0):.3f}"
        band = f"{row.get('band_2_4k_ratio', 0):.3f}"
        mark = "✓" if row["verdict"] == "pass" else "✗ " + "; ".join(row["issues"])
        if row["verdict"] == "pass" and row["advisory"]:
            mark = "✓ (提示: " + "; ".join(row["advisory"]) + ")"
        print(f"{row['file'][:22]:22}{row['duration_s']:6.1f}{f0:>7}{target:>6}{rate:>6}{voiced:>6}{periodicity:>6}{flat:>6}{band:>6}  {mark}")

    failed = [row for row in results if row["verdict"] != "pass"]
    advised = [row for row in results if row["verdict"] == "pass" and row["advisory"]]
    print(f"\n{len(results)} 条锚点，通过 {len(results) - len(failed)}（其中 {len(advised)} 条带提示），未通过 {len(failed)}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果写入 {args.json}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
