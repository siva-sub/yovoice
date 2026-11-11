#!/usr/bin/env python3
"""按显卡算力判断 Breeze TTS 2 能开哪一档加速，并给出成本区间。

背景（见 docs/breeze-colab-efficiency.md）：加速栈按算力代次给票。
FlashAttention-2 需要 sm80+，CUDA Graph 的 fast path 也按代次开；T4 是 sm75，
所以 T4 上加速栈全部失效——这正是第三方基准量到 0.48× 实时、而官方在 H100 上
声称 RTF 0.32 的原因。两者差 6.5 倍，选错卡等于多付六倍的钱。

用法：
    python3 scripts/colab/card-fit.py --compute-cap 8.9 --vram-gb 24
    python3 scripts/colab/card-fit.py --card L4 --minutes 1317 --balance 265.33
"""

from __future__ import annotations

import argparse
import json

# 已知卡的算力代次与显存。G4 未确认代次，所以不在表里：必须实测后传入。
KNOWN_CARDS = {
    "T4": {"compute_cap": 7.5, "vram_gb": 16},
    "L4": {"compute_cap": 8.9, "vram_gb": 24},
    "A100": {"compute_cap": 8.0, "vram_gb": 40},
    "H100": {"compute_cap": 9.0, "vram_gb": 80},
}

# 官方 api/infer 的显存需求。
EAGER_GIB = 7.7
FAST_ALL_GIB = 14.4

# RTF（墙钟/音频时长）假设，全部来自外部来源，本机未实测：
#   官方 README：H100 + 预热 fast path 达到 0.32（约 3.1× 实时）
#   第三方基准：0.48× 实时，即 RTF≈2.08（未标注卡型，疑似未开加速）
RTF_FAST = 0.32
RTF_SLOW = 1 / 0.48


def flash_attention_supported(compute_cap: float) -> bool:
    """FlashAttention-2 需要 sm80（Ampere）及以上；Turing(sm75) 不在支持范围。"""
    return compute_cap >= 8.0


def plan_for(compute_cap: float, vram_gb: float) -> dict:
    """返回该卡可用的加速档位与说明。"""
    flash = flash_attention_supported(compute_cap)
    fast_all = vram_gb >= FAST_ALL_GIB * 1.15  # 留约 15% 余量给上下文与碎片
    eager = vram_gb >= EAGER_GIB * 1.15
    if not eager:
        mode, note = "unsupported", f"显存 {vram_gb} GB 连 eager 的 {EAGER_GIB} GiB 都紧张"
    elif fast_all and flash:
        mode, note = "fast-all", "可开 --fast-all 与 FlashAttention-2，这是最快档"
    elif fast_all:
        mode, note = "fast-all-no-flash", "显存够 --fast-all，但没有 FlashAttention-2，收益打折"
    elif flash:
        mode, note = "eager-flash", "只能 eager，但 FlashAttention-2 仍可用"
    else:
        mode, note = "eager-only", "既无 FlashAttention-2 也开不了 --fast-all：接近基准的慢档"
    return {
        "compute_cap": compute_cap,
        "vram_gb": vram_gb,
        "flash_attention": flash,
        "fast_all": fast_all,
        "mode": mode,
        "note": note,
        # 该卡现实可期望的 RTF 区间：没有加速栈就按慢档算，有 fast-attn+fast-all 才敢用 0.32
        "rtf_best_guess": RTF_FAST if (fast_all and flash) else RTF_SLOW,
    }


def cost_range(minutes: float, balance: float | None, unit_rate: float = 1.0) -> dict:
    """给出 RTF 两端的 GPU 小时与计算单元，明确区分“乐观”和“悲观”。

    unit_rate 为每小时消耗的计算单元（A100 约 1.0，仅作口径示例；实际以 colab usage 为准）。
    """
    if minutes <= 0:
        raise ValueError("音频时长必须为正")
    audio_hours = minutes / 60
    hours = {"optimistic": audio_hours * RTF_FAST, "pessimistic": audio_hours * RTF_SLOW}
    units = {k: v * unit_rate for k, v in hours.items()}
    out = {
        "audio_hours": round(audio_hours, 2),
        "hours": {k: round(v, 1) for k, v in hours.items()},
        "units": {k: round(v, 1) for k, v in units.items()},
        "unit_rate_assumed": unit_rate,
    }
    if balance is not None:
        out["balance"] = balance
        out["fits"] = {
            "optimistic": units["optimistic"] <= balance,
            "pessimistic": units["pessimistic"] <= balance,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Breeze TTS 2 选卡与成本区间")
    parser.add_argument("--card", choices=sorted(KNOWN_CARDS), help="已知卡型")
    parser.add_argument("--compute-cap", type=float, help="算力代次，例如 8.9")
    parser.add_argument("--vram-gb", type=float, help="显存 GB")
    parser.add_argument("--minutes", type=float, default=1317.0, help="音频时长（默认本卷 1317 分钟）")
    parser.add_argument("--balance", type=float, help="计算单元结余（colab usage 读出）")
    parser.add_argument("--unit-rate", type=float, default=1.0, help="每小时计算单元（口径假设）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.card:
        cap, vram = KNOWN_CARDS[args.card]["compute_cap"], KNOWN_CARDS[args.card]["vram_gb"]
    elif args.compute_cap is not None and args.vram_gb is not None:
        cap, vram = args.compute_cap, args.vram_gb
    else:
        raise SystemExit("给出 --card，或 --compute-cap 与 --vram-gb（G4 请用后者：代次未确认）")

    plan = plan_for(cap, vram)
    cost = cost_range(args.minutes, args.balance, args.unit_rate)
    if args.json:
        print(json.dumps({"plan": plan, "cost": cost}, ensure_ascii=False, indent=2))
        return 0

    cap_label = f"sm{cap:g}"  # 7.5 显示 sm7.5；8.0 显示 sm8；避免 sm75 被四舍五入成 sm8
    print(f"算力 {cap_label} · 显存 {vram:g} GB → {plan['mode']}")
    print(f"  FlashAttention-2: {'可用' if plan['flash_attention'] else '不可用'}")
    print(f"  --fast-all:       {'可用' if plan['fast_all'] else '不可用'}")
    print(f"  说明: {plan['note']}")
    print()
    print(f"音频 {cost['audio_hours']} 小时（{args.minutes:.0f} 分钟）")
    print(f"  GPU 小时  乐观 {cost['hours']['optimistic']}（RTF {RTF_FAST}）"
          f" / 悲观 {cost['hours']['pessimistic']}（RTF {RTF_SLOW:.2f}）")
    print(f"  计算单元  乐观 {cost['units']['optimistic']} / 悲观 {cost['units']['pessimistic']}"
          f"（假设 {args.unit_rate}/小时，实际以 colab usage 为准）")
    if "fits" in cost:
        print(f"  结余 {cost['balance']}: 乐观{'够' if cost['fits']['optimistic'] else '不够'}，"
              f"悲观{'够' if cost['fits']['pessimistic'] else '不够'}")
    print()
    print("注意：两个 RTF 都不是本机实测值。真实数字要用 breeze-batch.py 在目标卡上量一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
