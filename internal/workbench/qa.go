package workbench

import (
	"slices"
	"time"

	"yovoice/internal/msg"
	"yovoice/internal/schema"
)

// 章节在书级流水线上的阶段，顺序与 ADR-0012 的进度矩阵一致：
// imported → read → attributed → verified → rendered → qa → mixed。
type ChapterStage string

const (
	StageImported   ChapterStage = "imported"
	StageRead       ChapterStage = "read"
	StageAttributed ChapterStage = "attributed"
	StageVerified   ChapterStage = "verified"
	StageRendered   ChapterStage = "rendered"
	StageQA         ChapterStage = "qa"
	StageMixed      ChapterStage = "mixed"
)

// RecordQA 写入章节的验收结果（ADR-0013）。判定非法或 fail 缺少说明时拒绝，不改动已有记录；
// At 为空时记为当前时间，便于追溯这次判定发生在什么时候。
func (w *Workbench) RecordQA(id string, rec schema.QARecord) error {
	if err := schema.ValidateQA(rec); err != nil {
		return err
	}
	if rec.At == "" {
		rec.At = time.Now().UTC().Format(time.RFC3339)
	}
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
	if i < 0 {
		return msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	return w.Store.Update(func(s *schema.State) {
		if i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id }); i >= 0 {
			s.Drafts[i].QA = &rec
		}
	}, true)
}

// ChapterProgress 返回章节当前所处的阶段，纯派生、不写状态：
// 闸门给出 read/attributed/verified，有生成版本即 rendered，盖过验收章即 qa。
// mixed 需要混音产物的记录，当前状态里没有这个标记，所以派生最远停在 qa。
func (w *Workbench) ChapterProgress(id string) (ChapterStage, error) {
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
	if i < 0 {
		return "", msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	d := s.Drafts[i]
	// 验收记录晚于渲染；pass 与 fail 都表示已经验收过，失败由 Note 说明。
	if d.QA != nil && d.QA.Verdict != "" && d.QA.Verdict != schema.QAVerdictUnchecked {
		return StageQA, nil
	}
	// 章节的分句生成快照以作品 ID 关联，任一版本存在即说明这一章已经渲染过。
	for _, g := range s.History {
		if g.Settings.ID == d.ID {
			return StageRendered, nil
		}
	}
	switch d.ChapterStatus() {
	case schema.GateRead:
		return StageRead, nil
	case schema.GateAttributed:
		return StageAttributed, nil
	case schema.GateVerified:
		return StageVerified, nil
	default:
		return StageImported, nil
	}
}
