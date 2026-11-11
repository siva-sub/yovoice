# ADR-0002：台词带置信度与证据

- 状态：已采纳，待实现
- 日期：2026-11-11
- 相关：ADR-0001

## 背景

闸门解决了"读过没有"，但没解决"该读哪几句"。170 章、8,970 条记录，逐条重读一遍的成本
是整套流程里最贵的人工。实际数据里，**置信度低于 0.5 的只有 367 条**（4%），它们才是真正
需要人看的部分；其余 96% 是机器判得对的旁白与明写对白。

要人能只处理这 4%，界面必须知道每条台词是怎么被判出来的。现有 `SubtitleCue` 只有
`{start, end, text, speakerId, performance}`，没有置信度，也没有判据。

## 决策

给 `SubtitleCue` 增加两个可选字段，并保留 `kind`：

```go
type SubtitleCue struct {
    Confidence *float64 `json:"confidence,omitempty"` // 归因置信度 0..1，人工改写后置空
    Evidence   string   `json:"evidence,omitempty"`   // 判据标记，见下表
    Kind       string   `json:"kind,omitempty"`       // narration | dialogue | thought
    // …既有字段不变
}
```

判据标记沿用抽取阶段的词汇，界面上翻成人话显示：

| evidence | 含义 | 是否常见出错 |
| --- | --- | --- |
| `prose` | 叙述行，非对白 | 否 |
| `inner_monologue` | 心声，用系统/内心音色 | 偶尔 |
| `lead_next` / `lead_prev` | 下一句/上一句叙述里点名 | **是（会反向）** |
| `tag_prev` / `tag_next` | 引号后紧跟"某某说" | 极少 |
| `alternation` | 两人对话交替推断 | **是（长对话整段翻转）** |
| `addressed_inverse` | 被呼唤者的对话 | 偶尔 |
| `continuity` | 沿用上一位说话人 | 偶尔 |

**人工改写任何一条台词时把 `confidence` 清空**：它表示"机器猜的"，人做过决定之后这个数字
就失去意义，留着会让人误以为系统又复核过一次。

界面提供按 `confidence` 升序的复核队列，默认只列 `< 0.5` 的条目，并显示 evidence 的人话标签。

## 依据

- 本卷数据：367 / 8,970 条 `conf < 0.5`；`evidence` 分布以 prose 7,085、inner_monologue 495、
  lead_next 434、continuity 303、lead_prev 249 为主
- 抽取脚本 `ss_audiobook/extract.py` 已经产出 `conf` 与 `evidence`，只是没有落进 GUI

## 后果

- store 需要一次迁移：在 `internal/store/migrate.go` 末尾追加步骤，旧数据两项留空即可
- 导入器必须把 `conf`/`evidence` 一起带进来，否则队列为空、功能等于没有
- 复核队列只对一个工作生效，不跨章节合并：章节是闸门的单位（ADR-0001）

## 备选方案

- **只存 conf 不存 evidence**：界面上能看到"可疑"但说不出为什么，人还得回去读原文
- **把 conf 存在独立边文件**：能更快上线，但一旦界面能改 speaker 就必须同步两份，容易漂移
