# 有声书工作流：实施计划

目标是把 2026-10 跑通的 Shadow Slave 流程（`ss_audiobook/`，170 章 / 8,970 条记录 /
22 小时音频）搬进 yovoice 的界面，并把当时的教训固化成代码约束，而不是留在脚本里。

决策背景见 [ADR 索引](adr/README.md)。

## 现状对照

| 环节 | 已有（脚本时代） | yovoice 现状 | 差距 |
| --- | --- | --- | --- |
| 书稿导入 | `extract.py` 解析 epub | `features/create/manuscript.ts` 支持 epub/txt/md，按章拆分 | 无 |
| 规则归因 | 引号内外 + 叙述动词 | `castRoles()` 同一套规则 | 无 |
| AI 补全角色表 | 人工读 | `proposeCast` 补性别/年龄/声音描述 | 无 |
| 逐句改写说话人 | 覆盖表 `章节:段落` | 每行一个说话人下拉，可整场改音色 | 无 |
| **闸门** | `attribution_gate.py` 四态 | **无** | **缺** |
| **置信度/证据** | `conf` / `evidence` | **无字段** | **缺** |
| **批量改写** | 覆盖表、别名合并 | **无** | **缺** |
| 音色锚点审计 | 五个指标 | 只有试听 | **缺** |
| 渲染 | Colab 跑 Python | 引擎子进程 | 可卸载（ADR-0005） |
| 音效四层 | `sfx.json` + `places.json` | 有 `duckDb` 与自由命名轨道 | **缺 role 语义** |
| 混音 | `mix_chapter.py` 侧链 | 时间线 + 增益/闪避 | 缺四层默认电平 |

## 里程碑

### M1 旧数据导入（本次执行）

把已有成果直接变成界面里的作品，而不是重新导入 epub 丢掉 170 章的归因。

- `scripts/audiobook/legacy-import.py`：`vol6.jsonl` + `voice_bible_v5.json` +
  `attribution_state.json` → 每章一个作品 JSON + 闸门边文件 + 清单
- 验收：作品数 = 章节数（超长章节拆分为多段）；cue 总数 = 记录数；
  每个 `speakerId` 都能在 `speakers` 里找到；`conf < 0.5` 的条目数与原数据一致

实测结果（2026-11-11）：171 个作品 / 170 章（第 1229 章 11,301 字符拆成两段），
8,970 条 cue 对 8,970 条记录，复核队列 367 条，闸门全为 `verified`。
**170 章的 `text_sha` 与原 `attribution_state.json` 逐字全部相等**，
所以 `verified` 是随导入转移过来的，而不是被当成信任旧数据无条件继承。

### M2 闸门落到 workbench（Go）

- `internal/schema`：`Draft` 增加 `gate`（`status`、`textSha`、`note`、`verifiedAt`）
- `internal/schema/validate.go`：渲染前检查；低于 `verified` 返回明确错误码
- `internal/store/migrate.go`：末尾追加升级步骤，旧作品按 `pending` 补齐
- `internal/workbench`：`ConfirmRead`、`VerifyChapter`、`ReopenChapter` 三个动作
- 验收：`go test -race ./internal/workbench ./internal/store ./internal/schema` 覆盖
  "未验证章节发起生成被拒绝"

### M3 复核队列与批量改写（前端）

- `features/create/subtitles.tsx`：行尾显示置信度与证据标签；队列默认只列 `< 0.5`
- 批量操作：选区改说话人、引号内全部改给某人、两人交替、别名合并（Sunny/Sunless/
  Lost from Light）、重名消歧（`Saint` 阶级 vs 角色）
- 人工改写后清空 `confidence`（ADR-0002）
- 验收：`pnpm lint && pnpm build`，浏览器测试覆盖"改说话人后置信度消失"与"队列项数下降"

### M4 渲染缓存与车道路由

- 缓存键：`hash(speaker, anchorRev, text, instruction, cfg)`；音效沿用
  `hash(prompt, seconds, seed)`。命中即不请求引擎
- 车道：旁白/角色/音效/配乐各自选模型，锚点审计的五个指标显示在角色页
- 验收：重跑一章不再产生引擎请求；八个锚点的审计数字可见

### M5 Colab 车道与实测

- 已交付脚手架：`scripts/colab/{provision.sh,bench.py,engine-shim.py}`（ADR-0005）
- 待办：一次 T4 与一次 A100 实机跑，把 RTF 与显存写回 ADR-0003 的表
- 验收：章节 1111 在本机界面上用远端 A100 渲染完成，并与 `pilot/ch_1111_v6.m4b` 对照

## 关键约束（不要重新发现）

1. **归因靠读，靠正则只有约 40% 正确率，且两人对话会系统性反向。**
2. **指令是音色漂移主因**：默认 clone-only + `guidance_scale=1.0`，只有加粗/喊叫行加到 2.0。
3. **行内标签是英文、括号、复数**，且必须是本句说话人自己的动作：`(sighs)`、`(laughs)`。
4. **音色由锚点音频决定，不由提示词决定**：坏锚点会污染该角色全书。
5. **音效点击要提前于台词约自身长度的 40%**；**侧链要压约 16 dB**，65% 不够。
6. **时长不要写进提示词**（MOSS 会自己补 `duration:`）；Stable Audio 3 的时长是真实参数。
7. **冷启动代价 14 倍**：一条车道常驻一个引擎，不要逐章重开。
8. **单章正文上限 11,000 字符**（前端 `partTextLimit`），第 1229 章 11,301 字符必须拆段。

## 不在本期范围

- Drama 3 的整场生成（`<|speaker:N|>`）只做一章 A/B，不做迁移
- 音色克隆的 LoRA 训练
- 多语言（本卷是英文，双语项不参与）
