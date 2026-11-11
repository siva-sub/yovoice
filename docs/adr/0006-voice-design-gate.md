# ADR-0006：音色设计是独立阶段，且必须先过审计再冻结

- 状态：锚点审计与冻结闸门脚本已交付（`anchor-audit.py`、`freeze-gate.py`），角色页待实现
- 日期：2026-11-11
- 相关：ADR-0001、ADR-0003

## 背景

选角和音色设计是两件事，现有实现把它们合成了一步：

- **选角**回答"这句话谁说的"，输入是正文，输出是角色（`castRoles` / `proposeCast` 从文本推断
  性别、年龄、声音描述）。
- **音色设计**回答"这个角色听起来是什么样"，输入是一段**写出来的声音描述**，输出是一段
  **冻结的锚点音频**加它的逐字稿。

yovoice 目前有 `Character.Preview`（一段预览音频）和 `Voice`（锚点音频 + `referenceText`），
但**预览与冻结之间没有任何测量**：听完觉得可以就存成音色。原先的流程不是这样，中间有一道
审计，因为坏的锚点会污染该角色**全书每一句**，而这一点在试听单句时听不出来。

`voice_bible_v5.json` 的每一项正好是这两步的产物：

```json
{
  "gender": "male",
  "design": "A professional audiobook narrator: a man in his late thirties with a mid-low,
             resonant voice, clean and warm, crisp consonants. He reads at a steady, even pace…",
  "anchor_line": "The sea stretched on, gray and indifferent, and the raft drifted with it.",
  "performance": "…",
  "f0_hz": 118,
  "seed": 278749530,
  "pace": "even"
}
```

## 决策

把音色设计做成渲染之前的独立阶段，四步，第三步是一道闸门：

```
design（写声音描述 + 期望 F0 + 种子）
   ↓
candidates（同一 description、多个 seed 各渲一条锚点句）
   ↓
audit（五个数，见下表；不达标就回上一步，不许"听着还行"就冻结）
   ↓
freeze（存成 Voice：音频 + 逐字稿 + f0_hz + seed），此后全书 clone-only
```

审计的五个数，全部来自已有脚本的实测口径：

| 指标 | 怎么量 | 判定 |
| --- | --- | --- |
| **中位 F0** | 40 ms 帧自相关，60–500 Hz | 偏离设计目标 ±10% 即失败 |
| **浊音比** voiced fraction | 每帧自相关峰值 > 0.45 记为浊音，取帧均值 | 偏低即"气声" |
| **周期性** periodicity | 同上峰值，帧均值 | 与噪声感互为反面 |
| **谱平坦度** flatness | 帧谱几何均值 / 算术均值 | 高即噪声样（气声） |
| **语速** words/sec | 逐字稿词数 ÷ 时长 | **< 1.8 = 拖沓；2.2–3.2 = 正常旁白** |

另外报告 **2–4 kHz 能量比**（齿音/明亮度）与**音色相似度**（对数谱余弦，用于"同一角色不同
take"之间的相对比较）。这两个不作硬判定：前者没有公认阈值，后者只有相对意义——拿候选互相比，
而不是跟一个绝对分数线比。

冻结后写回 `f0_hz` 与 `seed`，两者是后续复现同一音色的唯一凭据；`seed` 丢了就无法重渲同一条
锚点，只能重挑。

## 依据

- `ss_audiobook/measure_f0.py`（中位 F0）、`measure_whisper.py`（周期性、平坦度、浊音比、
  语速及其 1.8 / 2.2–3.2 阈值）、`measure_timbre.py`（对数谱余弦）
- `anchors_v6/` 有 56 条实测锚点，`voice_bible_v5.json` 每个角色记了 `f0_hz` 与 `seed`
- 规则来自 `~/.pi/skills/breeze-audiobook/references/voice-design.md`：音色身份来自锚点音频，
  指令会造成漂移；所以冻结之后逐句一律 clone-only、`guidance_scale=1.0`

## 后果

- 界面需要一个"设计 → 候选 → 审计 → 冻结"的角色页，而不是一个"生成预览"按钮
- `Character` 需要存审计结果（五个数 + 判定 + 目标 F0 + seed），否则换模型重做锚点时无法对比
- 审计工具必须能用标准库跑（本机没有 `soundfile`），所以用 `wave` + `numpy` 实现
- 换模型（ADR-0003）时，同一 `design` 重新走一遍审计即可对比新旧音色，不需要重写声音描述

## 备选方案

- **保持现状（预览即冻结）**：成本最低，但坏锚点的问题要到听众投诉"这个人的声音一直在变"
  才暴露，那时已经渲染了全书
- **只做技术性检查（不爆音、不静音）**：能挡住明显故障，挡不住"气声"和"拖沓"这两类真正
  会被抱怨的问题，而后者正是五个数要量出来的
