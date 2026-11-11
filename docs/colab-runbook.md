# Colab 实机跑手册：一次 T4、一次 A100

> **状态：尚未实机运行。** 本页命令来自仓库脚手架、`colab` CLI 0.7.4 的帮助文本与
> `analyze.py cost` 的本地计算，**没有一条在 Colab 上跑过**。在 T4/A100 的 JSON 留档之前：
>
> - ADR-0005 保持“脚手架已交付，尚未实机验证”
> - ADR-0003 的 RTF 表不得写入新数字
> - CHANGELOG 不得写“已在 T4/A100 实测”

目标是用**两趟租卡**拿到第一手数据：T4 与 A100 各跑同一组基准（SFX + TTS），分别重点看
音效车道与人声道的 RTF 与峰值显存；两趟都只做基准测量，不跑整卷。整卷渲染要等这里的
RTF/显存数据写回 ADR-0003 之后再排。

## 一、先决条件

```bash
# CLI 与账号余额（余额单位是计算单元）
colab version
colab usage
```

`provision.sh` 只在 Colab 的 GPU 运行时里工作：它要编译 CUDA 版 audio.cpp、从 Hugging Face
下载两个 GGUF，并假设 `/content` 可写。它找不到 `nvcc` 会直接失败，不会降级到 CPU。
T4/A100 的资格与形状以 `colab new` 的实际返回为准；A100 是否可用取决于账号档位。

## 二、预算估算（本地计算，不是实测）

整卷的音频时长与模型成本用 `analyze.py cost` 在本机算，不占 GPU：

```bash
python3 scripts/audiobook/analyze.py cost \
    --records ~/ss_audiobook/data/vol6.jsonl --balance 265
```

2026-11-11 在本机跑出的真实输出（默认 A100 单价）：

```text
成本预估：/home/siva/ss_audiobook/data/vol6.jsonl
170 章 / 8,970 条 cue / 1,122,289 字符
音频估算：14.2 字符/秒 → 21.95 小时（默认系数按 ADR-0003 的 1,122,289 字符 / 1,317 分钟反推）
算法：GPU 小时 = 音频小时 ÷ 稳态 RTFx；计算单元 = GPU 小时 × 卡型单价

模型                      RTF     GPU 小时      单元(A100)  相对结余
breeze-tts-2           0.48       45.7         538.3  不足 ← 当前
ming-3.0-0.5b          1.01       21.7         255.8  够
qwen3-tts-1.7b         3.58        6.1          72.2  够
longcat-audiodit-1b    6.69        3.3          38.6  够
drama-3                7.60        2.9          34.0  够

卡型单价（计算单元/GPU 小时）：T4=1.68 · L4=4.82 · G4=4.82 · A100=11.77 · H100=19.24
按 breeze-tts-2 换算到各卡：T4 76.8 / L4 220.5 / G4 220.5 / A100 538.3 / H100 880.0

按 breeze-tts-2 在 A100 上约需 538.3 单元，结余 265，不足，差 273.3；结余约够按顺序渲 83/170 章。 成本上可换 drama-3，压到 2.9 GPU 小时 / 34.0 单元。
注意：卡型只换算单价，不换算速度；RTF 表来自基准卡，换卡后的吞吐需要实测（ADR-0003 待办）。模型选择还要看许可证与车道策略（ADR-0003），这里只比较成本，不替用户选车道。
```

同一份数据按 T4 单价换算：

```bash
python3 scripts/audiobook/analyze.py cost \
    --records ~/ss_audiobook/data/vol6.jsonl --balance 265 --card T4
```

```text
按 breeze-tts-2 在 T4 上约需 76.8 单元，结余 265，够用（剩 188.2）。
```

T4 的 76.8 单元是**单价换算**，不是“T4 更快”：`analyze.py cost` 明确不把基准卡 RTF
换算成 T4 的吞吐。两趟基准测量的目的就是补上这个缺口。

### 这两趟租卡本身花多少

`analyze.py` 算的是整卷成本；单次基准没有实测墙钟，只能按租用时长设上限：

| 卡 | 单价（单元 / GPU 小时） | 按 1 GPU 小时预留 | 说明 |
| --- | --- | --- | --- |
| T4 | 1.68 | 1.68 | 含编译与模型下载；实际墙钟未知 |
| A100 | 11.77 | 11.77 | 同上；不要在没有 `colab stop` 的情况下过夜 |

这里的“1 小时”是预算上限，不是预计耗时。跑完用 `colab usage` 对比余额，得到真实花费。

## 三、T4：音效车道的基准

`bench.py` 需要同目录的 `provision.sh`，而 `colab run` 只把单个 `.py` 送进内核、默认
超时 30 s。所以这里用 `new` + `upload` + `exec`，把两个文件放进 `/content` 再跑：

```bash
colab new --gpu T4 --session acpp-t4

# 先确认远端有 nvcc；缺失时 provision.sh 会直接退出
colab exec -s acpp-t4 --timeout 60 <<'PY'
import shutil, subprocess
print("nvcc:", shutil.which("nvcc") or "缺失")
subprocess.run(["nvcc", "--version"])
PY

colab upload -s acpp-t4 scripts/colab/provision.sh /content/provision.sh
colab upload -s acpp-t4 scripts/colab/bench.py     /content/bench.py

colab exec -s acpp-t4 --timeout 7200 <<'PY'
import subprocess, sys
subprocess.run(["bash", "/content/provision.sh"], check=True)
subprocess.run([
    sys.executable, "/content/bench.py",
    "--gpu-label", "T4",
    "--repeats", "2",
    "--skip-provision",
], check=True)
PY

colab download -s acpp-t4 /content/bench-T4.json ./bench-T4.json
colab stop -s acpp-t4
```

`provision.sh` 负责编译与下载；`--skip-provision` 让 `bench.py` 不再重复准备。
`--repeats 2` 让每条 SFX 提示跑两遍，便于看启动开销的抖动。

## 四、A100：人声车道的基准

同一个脚本，换卡与标签；两趟的提示、重复次数保持一致，结果才能横向比较。
A100 只租这一趟，跑完立即 `stop`：

```bash
colab new --gpu A100 --session acpp-a100

colab upload -s acpp-a100 scripts/colab/provision.sh /content/provision.sh
colab upload -s acpp-a100 scripts/colab/bench.py     /content/bench.py

colab exec -s acpp-a100 --timeout 10800 <<'PY'
import subprocess, sys
subprocess.run(["bash", "/content/provision.sh"], check=True)
subprocess.run([
    sys.executable, "/content/bench.py",
    "--gpu-label", "A100",
    "--repeats", "2",
    "--skip-provision",
], check=True)
PY

colab download -s acpp-a100 /content/bench-A100.json ./bench-A100.json
colab stop -s acpp-a100
```

A100 的 `--timeout` 给到 3 小时，是为了覆盖编译；这不是预计耗时。若 `provision.sh`
在 `nvcc` 处失败，先确认 `colab new --gpu A100` 真的拿到了 GPU 运行时，再重试；
不要用 CPU 运行时跑这份脚本。

## 五、跑完怎么记录（没跑之前不要做）

两份 JSON 下载到本机后，先做一次只读汇总：

```bash
python3 - <<'PY'
import json
for name in ("bench-T4.json", "bench-A100.json"):
    report = json.load(open(name, encoding="utf-8"))
    print(report["gpu_label"], report["gpu"].get("name"), report["engine_commit"],
          "峰值显存", report["peak_vram_mib"], "MiB")
    for key, value in report["summary"].items():
        print(" ", key, value)
PY
```

只有在两份报告都留档、且数字能相互对照之后：

1. 把实测 RTF 与峰值显存写进 ADR-0003 的“依据”，并注明卡型、引擎 commit、重复次数；
2. 把 ADR-0005 的状态从“尚未实机验证”改成实测结论（方案 A 是否接通、隧道是否稳定）；
3. 在 CHANGELOG 的 `Unreleased` 记录用户可见的变化，而不是把“跑了一次基准”写成功能。

## 六、失败与清理

```bash
colab sessions                 # 看还有哪些会话在计费
colab stop -s acpp-t4
colab stop -s acpp-a100
```

- **`nvcc` 缺失**：运行时不是 GPU 形状，或 CUDA Toolkit 未安装；`provision.sh` 会直接退出。
- **编译失败**：先看 `cmake`/`nvcc` 的报错；`provision.sh` 是幂等的，修好后可重跑。
- **下载失败**：Hugging Face 的 GGUF 仓库未设门禁，但网络仍可能超时；重跑即可，已下载的会跳过。
- **`ffprobe` 缺失**：`bench.py` 量不到生成音频的时长，TTS 的 RTF 会是 `None`；装 ffmpeg 后重跑。
- **会话被回收**：`colab exec` 会报 404/401；重新 `colab new` 并重跑，不要把 Colab 当持久服务。

方案 A（`engine-shim.py` 接本机界面）的完整步骤见 [docs/colab-offload.md](colab-offload.md)；
本页只覆盖先量 RTF 与显存的 T4/A100 两趟，不声称方案 A 已经接通。
