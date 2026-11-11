package workbench

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"

	"yovoice/internal/schema"
)

// TakeRecord 是音频库里一条 cue 的一次生成（ADR-0009）。同一 cue 的多次生成全部保留，
// Selected 只标记真正进入混音的那一版；旧数据没有 take 表时由 Generation 直接推导，不强制迁移。
type TakeRecord struct {
	TakeID    string    `json:"takeId"`
	CueID     string    `json:"cueId"`
	ModelID   string    `json:"modelId"`
	AnchorRev string    `json:"anchorRev,omitempty"`
	TextHash  string    `json:"textHash"`
	Seed      *int      `json:"seed,omitempty"`
	CreatedAt time.Time `json:"createdAt"`
	Selected  bool      `json:"selected"`
}

// TextHash 是文本的稳定指纹，供 take 记录与种子账本使用；空文本也有确定的指纹。
func TextHash(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// TakesFromHistory 从生成历史推导逐句 take 列表：只认带 GenerationSegment 的版本，
// 同一 cue 的全部版本都保留，默认选中时间最新的一版。生成历史通常新的在前，
// 这里按 CreatedAt 再确认一次，导入归档后顺序变化也不会选错。
// 旧版本没有记录 anchorRev，推导结果留空，不阻断读取。
func TakesFromHistory(history []schema.Generation) []TakeRecord {
	latest := map[string]time.Time{}
	for _, generation := range history {
		if generation.Segment == nil || generation.Segment.CueID == "" {
			continue
		}
		cueID := generation.Segment.CueID
		if current, ok := latest[cueID]; !ok || generation.CreatedAt.After(current) {
			latest[cueID] = generation.CreatedAt
		}
	}
	takes := make([]TakeRecord, 0, len(history))
	selected := map[string]bool{}
	for _, generation := range history {
		if generation.Segment == nil || generation.Segment.CueID == "" {
			continue
		}
		cueID := generation.Segment.CueID
		seed := generation.Seed
		if seed == nil {
			seed = generation.Settings.Seed
		}
		record := TakeRecord{
			TakeID:    generation.ID,
			CueID:     cueID,
			ModelID:   generation.Settings.ModelID,
			TextHash:  TextHash(generation.Settings.Text),
			Seed:      seed,
			CreatedAt: generation.CreatedAt,
		}
		// 同一时间戳的重复版本只选历史中靠前的一条，保证每个 cue 恰好一条 Selected。
		if !selected[cueID] && generation.CreatedAt.Equal(latest[cueID]) {
			record.Selected = true
			selected[cueID] = true
		}
		takes = append(takes, record)
	}
	return takes
}

// SelectTake 把 cueID 的选中版本切到 takeID，并取消该 cue 其余版本的选中状态。
// cue 或 take 不存在时返回错误且不改动任何记录，避免界面上出现没有当前版本的空 cue。
func SelectTake(takes []TakeRecord, cueID, takeID string) error {
	found := false
	for _, take := range takes {
		if take.CueID == cueID && take.TakeID == takeID {
			found = true
			break
		}
	}
	if !found {
		return fmt.Errorf("cue %s 没有 take %s", cueID, takeID)
	}
	for i := range takes {
		if takes[i].CueID == cueID {
			takes[i].Selected = takes[i].TakeID == takeID
		}
	}
	return nil
}
