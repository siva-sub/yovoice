package workbench

import (
	"testing"
	"time"

	"yovoice/internal/schema"
)

func takeGeneration(id, cueID string, createdAt time.Time) schema.Generation {
	seed := 7
	return schema.Generation{
		ID:        id,
		CreatedAt: createdAt,
		Seed:      &seed,
		Settings:  schema.Draft{Text: "同一句台词", SynthesisSettings: schema.SynthesisSettings{ModelID: "breeze-tts-2"}},
		Segment:   &schema.GenerationSegment{CueID: cueID, SpeakerID: "speaker"},
	}
}

func selectedTakes(takes []TakeRecord, cueID string) []TakeRecord {
	var selected []TakeRecord
	for _, take := range takes {
		if take.CueID == cueID && take.Selected {
			selected = append(selected, take)
		}
	}
	return selected
}

// 同一 cue 的三次生成全部保留，默认选中最新一版，且只有一条 Selected。
func TestTakesFromHistoryKeepsEveryVersion(t *testing.T) {
	base := time.Date(2026, 11, 11, 10, 0, 0, 0, time.UTC)
	takes := TakesFromHistory([]schema.Generation{
		takeGeneration("take-3", "cue-1", base.Add(2*time.Minute)),
		takeGeneration("take-2", "cue-1", base.Add(time.Minute)),
		takeGeneration("take-1", "cue-1", base),
	})
	if len(takes) != 3 {
		t.Fatalf("三条 take 都应保留：%+v", takes)
	}
	selected := selectedTakes(takes, "cue-1")
	if len(selected) != 1 || selected[0].TakeID != "take-3" {
		t.Fatalf("默认应只选中最新一版：%+v", selected)
	}
	if takes[0].TextHash != TextHash("同一句台词") || takes[0].ModelID != "breeze-tts-2" || takes[0].Seed == nil || *takes[0].Seed != 7 {
		t.Fatalf("take 记录缺少生成参数：%+v", takes[0])
	}
}

// 切选后该 cue 仍然只有一条 Selected；不存在的 take 返回错误且不改动原选择。
func TestSelectTakeKeepsSingleSelection(t *testing.T) {
	base := time.Date(2026, 11, 11, 10, 0, 0, 0, time.UTC)
	takes := TakesFromHistory([]schema.Generation{
		takeGeneration("take-3", "cue-1", base.Add(2*time.Minute)),
		takeGeneration("take-2", "cue-1", base.Add(time.Minute)),
		takeGeneration("take-1", "cue-1", base),
	})
	if err := SelectTake(takes, "cue-1", "take-1"); err != nil {
		t.Fatal(err)
	}
	selected := selectedTakes(takes, "cue-1")
	if len(selected) != 1 || selected[0].TakeID != "take-1" {
		t.Fatalf("切选后应只选中 take-1：%+v", selected)
	}

	if err := SelectTake(takes, "cue-1", "missing"); err == nil {
		t.Fatal("不存在的 take 应被拒绝")
	}
	if selected = selectedTakes(takes, "cue-1"); len(selected) != 1 || selected[0].TakeID != "take-1" {
		t.Fatalf("拒绝切选时不应改动原有选择：%+v", selected)
	}
}

// 旧数据只有整段生成、没有逐句 Segment 时推导出空列表，不报错也不伪造 take。
func TestTakesFromHistoryAcceptsLegacyGenerations(t *testing.T) {
	if takes := TakesFromHistory(nil); len(takes) != 0 {
		t.Fatalf("没有历史时应返回空列表：%+v", takes)
	}
	legacy := []schema.Generation{{ID: "whole", Settings: schema.Draft{Text: "整段"}}}
	if takes := TakesFromHistory(legacy); len(takes) != 0 {
		t.Fatalf("整段生成不应被当成逐句 take：%+v", takes)
	}
}
