# 有声书构建契约

并行开发时，三条车道改不同的文件、共同遵守这一份字段定义。字段名、JSON 名、错误码三者
必须完全一致，否则合并后会在运行时才暴露问题。

来源：[ADR-0001](adr/0001-attribution-gate.md)（闸门）、[ADR-0002](adr/0002-cue-evidence-model.md)
（证据）、[ADR-0012](adr/0012-book-layer.md)（书层）。

## 一、Go 类型（`internal/schema`）

### 闸门

```go
type Gate struct {
    Note       string `json:"note,omitempty"`
    Status     string `json:"status"`               // pending | read | attributed | verified
    TextSHA    string `json:"textSha"`
    VerifiedAt string `json:"verifiedAt,omitempty"` // RFC3339，仅 verified 时有值
}
```

`Draft` 增加：

```go
Gate *Gate `json:"gate,omitempty"`
```

### cue 的证据

`SubtitleCue` 增加三个可选字段：

```go
Confidence *float64 `json:"confidence,omitempty"` // 0..1；人工改写后置空
Evidence   string   `json:"evidence,omitempty"`   // prose | inner_monologue | lead_next | lead_prev |
                                                  // tag_next | tag_prev | alternation | addressed_inverse | continuity
Kind       string   `json:"kind,omitempty"`       // narration | dialogue | thought
```

### 书

```go
type Book struct {
    ID             string `json:"id"`
    Title          string `json:"title"`
    SeriesID       string `json:"seriesId,omitempty"`
    Order          int    `json:"order,omitempty"`
    LoudnessTarget string `json:"loudnessTarget,omitempty"` // 默认 "-19 LUFS / -3 dBTP"
    CreatedAt      string `json:"createdAt,omitempty"`
    UpdatedAt      string `json:"updatedAt,omitempty"`
}
```

`Draft` 增加归属：

```go
BookID string `json:"bookId,omitempty"`
```

`State` 增加：`Books []Book \`json:"books"\``。默认状态为 `Books: []Book{{ID: "unfiled", Title: "未归档"}}`。

## 二、规则（`internal/schema/validate.go`）

1. `Gate.Status` 只允许四个值；`read`/`attributed`/`verified` 必须有 16 位十六进制的 `TextSHA`；
   `verified` 必须有 `VerifiedAt`。
2. `Confidence` 若存在必须在 `[0,1]`；`Kind` 只允许三个值；`Evidence` 只允许上表九个值或空。
3. `Kind == "story"` 且 `Gate == nil` 时视为 `pending`（缺省不报错，旧数据要能读）。
4. `Book.ID` 必须非空且 `[a-z0-9-]{1,64}`；`Draft.BookID` 若存在必须能在 `State.Books` 里找到
   （由 workbench 校验，`schema` 层只校验格式）。

## 三、错误码（`internal/msg`）

新增一个，命名与现有风格一致：

```go
ErrChapterNotVerified // 章节未确认读过，拒绝渲染
```

渲染入口（`workbench.GenerateCue` 及其调用方）在 `Gate.Status != "verified"` 时返回它，
且**必须在请求引擎之前返回**。

## 四、workbench 动作

```go
func (w *Workbench) ConfirmRead(id string) error                       // pending → read，记录 textSha
func (w *Workbench) VerifyChapter(id, note string) error               // read/attributed → verified
func (w *Workbench) ReopenChapter(id string) error                     // 任意 → pending
func (w *Workbench) MoveToBook(id, bookID string) error                // 归属与迁移用
```

- `ConfirmRead` 对已 `verified` 的章节是 no-op（不降级）。
- `VerifyChapter` 在 `Gate.Status == "pending"` 时拒绝（必须先 `ConfirmRead`）。
- 三个动作都只改 `Gate`，不碰 `textSha` 之外的正文字段——**不重新计算 textSha**，
  由调用方（导入器/编辑）在正文变化时清空 `Gate`。

## 五、前端类型（`web/src/shared/workbench/types.ts`）

与上面 Go 结构一一对应，字段名相同（camelCase 一致）：`Gate`、`Book`、以及
`SubtitleCue` 上的 `confidence` / `evidence` / `kind`。

## 六、车道边界

| 车道 | 拥有（可写） | 只读 |
| --- | --- | --- |
| A 分析 | `scripts/audiobook/**` | 其他全部 |
| B 内核 | `internal/**`、`cmd/**` | `web/**`、`scripts/**` |
| C 界面 | `web/**` | `internal/**`、`scripts/**` |

合并顺序：A → B → C。每合一条跑一次全量校验；A 与内容无关，先合最省事。
