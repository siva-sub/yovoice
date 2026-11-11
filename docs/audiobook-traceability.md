# 需求追溯：脚本时代 → 当前系统

这张表回答一个问题：**旧流程里每一个真实存在过的能力，现在由谁承担、有没有测试证明它还在。**
左边的每一项都来自 2026-10 的 `ss_audiobook/` 实跑与后续会话里的纠正，不是设想。

## 一、脚本时代的能力

| 旧实现 | 做什么 | 新家 | 状态 | 证明方式 |
| --- | --- | --- | --- | --- |
| `extract.py` | epub → 按章分段 + 说话人归因 | `web/src/features/create/manuscript.ts`（`readManuscript`、`castRoles`） | 已有（仓库自带） | 浏览器测试覆盖导入 |
| `attribution_gate.py` | 四态闸门，未 verified 不许 build | ADR-0001；`internal/schema.Gate` + `workbench.ConfirmRead/VerifyChapter` | **M2 施工中** | Go 测试：未 verified 渲染被拒绝 |
| `data/attribution_state.json` | 每章 `status` / `text_sha` / `flags` / `note` | `Draft.Gate`（`status`/`textSha`/`verifiedAt`/`note`） | **M2 施工中** | 迁移测试 + `legacy-import.py` 的 sha 对齐（170/170 相等） |
| `data/overrides.json` | 覆盖表，键 `章节:段落`，可改 speaker 与 kind | ADR-0001 覆盖语义；ADR-0011 复核包沿用同一键 | 契约已定，待实现 | 待 M7 |
| `extract.py` 的 `conf` / `evidence` | 每条记录的置信度与判据 | ADR-0002；`SubtitleCue.confidence/evidence/kind` | **M2 施工中** | 校验正反例 + 复核队列计数（367 条） |
| `voice_bible_v5.json` | 18 个角色的 design / anchor_line / f0 / seed | ADR-0006；`Character.Settings` + `Voice` | 导入器已产出，界面待做 | `legacy-import.py` 单测（默认 clone-only、guidance 1.0） |
| `render_breeze.py` | 逐行渲染、可续跑、每章编码 m4b | `workbench.GenerateCue` + ADR-0010 批次/续跑 | 逐 cue 已有，批次待做 | 待 M6 |
| `sfx.json` / `places.json` | 音效 cue + 环境层，锚到脚本记录 | ADR-0004；`AudioTrack.role` 待加 | 数据模型部分具备（`duckDb`） | 待 M9 |
| `mix_chapter.py` | 四层放置 + 侧链 + m4b | ADR-0004；时间线 + `duckDb` | 待做 | 待 M9 |
| `sfx.py lint` | 按模型提示契约打分 | ADR-0004 的提示规则 | 待做 | 待 M9 |
| 缓存键 `hash(prompt, seconds, seed)` | 音效不重渲 | ADR-0010 | 待做 | 待 M6 |
| `measure_f0/whisper/timbre.py` | 锚点五个数 | ADR-0006；`scripts/audiobook/anchor-audit.py` | **已交付** | 7 项合成信号测试通过 |
| `colab_all_in_one.py` | 一次跑完 TTS→SFX→混音 | ADR-0005；`scripts/colab/*` | **脚手架已交付** | 未做 Colab 实机 |

## 二、会话中纠正过的判断（避免重复犯错）

这些是本次会话里**推翻过先前结论**的点，写下来是因为它们最容易在实现时被"重新发现"成错的：

| 曾以为 | 实际 | 影响 |
| --- | --- | --- |
| yovoice 只有基础的 TTS 编辑 | 仓库**自带** epub 导入、`castRoles` 规则归因、AI 选角、逐行说话人下拉 | M2/M3 只补闸门与阅读体验，不重做归因 |
| Yovoice 的 Linux 有 CUDA | 官方 Ubuntu 预编译包与 yovoice 均只有 CPU/Vulkan；CUDA 只能自编译 | ADR-0005 的方案必须是"Colab 上自编译 + shim"，不是"直接用" |
| Breeze 约 8B、需 A100 | 基准与目录记 ~3.47B；但**稳态只有 0.48× 实时** | 真正的问题是速度（45.7 GPU 小时），不是显存 |
| Breeze 无法本地跑 | `Breeze-TTS-2.cpp` 的 q4_k 只有 2.4 GB、纯 Vulkan | 本地渲染是可行的备选 |
| Stable Audio 3 权重有门禁 | `stable-audio-3-small-sfx` 在 HF 上 `gated=None` | SFX 车道的"HF token 阻塞"已不存在 |
| Drama 3 在 audio.cpp 里有 | 本地 checkout 只有 `fish_audio` = S2 Pro；Drama 3 需引擎 ≥0.9.3 | 换 Drama 3 要先更新引擎 |
| 归因正则提议可用 | 实测三次只对一次，且会把"对某人说"改成"由某人说" | 任何提议都只能人工确认 |
| 侧链压 65% 够 | 实测要 ~16 dB（84%），且 hit 要提前约自身长度的 40% | 混音参数不可调低 |

## 三、用户在这次会话里明确提出的要求

| 要求 | 落点 | 状态 |
| --- | --- | --- |
| 「要能读归因并改派」 | ADR-0007 阅读式界面 | **M3 施工中** |
| 「别人也要能做」 | ADR-0011 owner/reviewer/listener + 复核包 | 契约已定，待实现 |
| 「cast 管理界面」 | ADR-0008 角色卡 + 别名图 + 未知专名 | 待做 |
| 「音频库」 | ADR-0009 take 三层组织 | 待做 |
| 「多本书怎么管」 | ADR-0014 作用域表 + 同名规则 | 契约已定，待实现 |
| 「把模型卸载到 Colab」 | ADR-0005 + `scripts/colab/engine-shim.py` | 脚手架已交付，未实机 |
| 「测多档 GPU」 | `scripts/colab/bench.py` + 多架构编译 | 脚手架已交付，未实机 |
| 「全部 nice-to-have 都写 ADR」 | ADR-0007..0011 共 17 项映射 | 已完成 |
| 「先算成本再渲」 | ADR-0010 + `analyze.py cost` | **M0 施工中** |
| 「按流优化构建顺序」 | `docs/audiobook-flows.md` 的 P0–P8 | 已完成 |

## 四、当前缺口（按影响排序）

1. **闸门强制未生效**：现在渲染不检查闸门状态，等于旧脚本的纪律没搬过来（M2 施工中）
2. **阅读界面不能按批次工作**：M3 施工中
3. **没有成本闸门**：可能在没有预算的情况下发起 45 小时的渲染（M0/M6）
4. **验收只看文本**：吞词、读错人名、空洞都没有自动检查（M4）
5. **章缝未量**：本卷是分块渲染的，边界跳变从未检查过（M0 的 `seam` 子命令）

## 五、怎么验证"没有回归"

每合一条车道跑一次同一组命令，任何一项失败即回退该车道：

```
gofmt -l cmd internal desktop/linux    # 必须为空
go build ./... && go vet ./...
go test -race ./...                    # 20 个包
cd web && pnpm run lint && pnpm run build
cd web && pnpm exec playwright test browser-tests/
python3 scripts/audiobook/test_legacy_import.py
python3 scripts/audiobook/test_anchor_audit.py
python3 scripts/audiobook/test_analyze.py
```
