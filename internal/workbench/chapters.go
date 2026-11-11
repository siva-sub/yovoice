package workbench

import (
	"slices"
	"time"

	"yovoice/internal/msg"
	"yovoice/internal/schema"
)

// ConfirmRead 记录这一章已经通读过正文：pending → read，并记下当前正文的指纹。
// 已读过、已归因或已确认的章节不降级，也不重算指纹；正文变化由导入器或编辑清空 Gate（契约第四节）。
// 未过闸门的章节在生成时返回 msg.ErrChapterNotVerified。
func (w *Workbench) ConfirmRead(id string) error {
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
	if i < 0 {
		return msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	d := s.Drafts[i]
	if d.ChapterStatus() != schema.GatePending {
		return nil
	}
	gate := schema.Gate{Status: schema.GateRead, TextSHA: schema.TextSHA(d.Text)}
	if d.Gate != nil {
		gate.Note = d.Gate.Note
	}
	return w.updateChapterGate(id, gate)
}

// VerifyChapter 把读过或已归因的章节标记为 verified，并记下确认时间；pending 时拒绝，必须先 ConfirmRead。
// 已 verified 的章节再次确认是 no-op。
func (w *Workbench) VerifyChapter(id, note string) error {
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
	if i < 0 {
		return msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	d := s.Drafts[i]
	if d.Gate != nil && d.Gate.Status == schema.GateVerified {
		return nil
	}
	if d.Gate == nil || d.Gate.TextSHA == "" || (d.Gate.Status != schema.GateRead && d.Gate.Status != schema.GateAttributed) {
		return msg.Err(msg.ErrChapterNotVerified, nil)
	}
	gate := schema.Gate{Status: schema.GateVerified, TextSHA: d.Gate.TextSHA, Note: d.Gate.Note, VerifiedAt: time.Now().UTC().Format(time.RFC3339)}
	if note != "" {
		gate.Note = note
	}
	return w.updateChapterGate(id, gate)
}

// ReopenChapter 把任意状态的章节退回 pending，清掉确认时间与正文指纹；已 pending 时不再写入。
func (w *Workbench) ReopenChapter(id string) error {
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
	if i < 0 {
		return msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	if s.Drafts[i].ChapterStatus() == schema.GatePending {
		return nil
	}
	return w.updateChapterGate(id, schema.Gate{Status: schema.GatePending})
}

// MoveToBook 把作品归入一本已有的卷；目标书不存在时拒绝，改成同一本书时不再写入。
func (w *Workbench) MoveToBook(id, bookID string) error {
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
	if i < 0 {
		return msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	if !slices.ContainsFunc(s.Books, func(b schema.Book) bool { return b.ID == bookID }) {
		return msg.Err(msg.ErrRequestInvalid, nil)
	}
	if s.Drafts[i].BookID == bookID {
		return nil
	}
	return w.Store.Update(func(s *schema.State) {
		if i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id }); i >= 0 {
			s.Drafts[i].BookID = bookID
		}
	}, true)
}

// updateChapterGate 只替换闸门，正文、归属与其他设置保持不变。
func (w *Workbench) updateChapterGate(id string, gate schema.Gate) error {
	return w.Store.Update(func(s *schema.State) {
		if i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id }); i >= 0 {
			s.Drafts[i].Gate = &gate
		}
	}, true)
}

// ReassignCue 把一句台词改派给指定说话人，并清空该句的机器归因置信度（ADR-0002）：
// 人做过决定后置信度不再有意义。kind 为空表示不改类型，非空时必须是 narration、dialogue 或 thought。
// 说话人不在这一章的说话人表里、cue 不存在或 kind 非法时拒绝，不写状态。
func (w *Workbench) ReassignCue(draftID, cueID, speakerID, kind string) error {
	if kind != "" && !slices.Contains(schema.CueKinds, kind) {
		return msg.Err(msg.ErrSubtitleInvalid, nil)
	}
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == draftID })
	if i < 0 {
		return msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	document := s.Drafts[i].Subtitles
	if document == nil || !slices.ContainsFunc(document.Speakers, func(speaker schema.SubtitleSpeaker) bool { return speaker.ID == speakerID }) {
		return msg.Err(msg.ErrSpeakerInvalid, nil)
	}
	j := slices.IndexFunc(document.Cues, func(cue schema.SubtitleCue) bool { return cue.ID == cueID })
	if j < 0 {
		return msg.Err(msg.ErrSubtitleInvalid, nil)
	}
	if !cueNeedsReassign(document.Cues[j], speakerID, kind) {
		return nil
	}
	return w.Store.Update(func(s *schema.State) {
		if i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == draftID }); i >= 0 && s.Drafts[i].Subtitles != nil {
			if j := slices.IndexFunc(s.Drafts[i].Subtitles.Cues, func(cue schema.SubtitleCue) bool { return cue.ID == cueID }); j >= 0 {
				reassignCue(&s.Drafts[i].Subtitles.Cues[j], speakerID, kind)
			}
		}
	}, true)
}

// ReassignCues 成片改派：一次加锁、批量应用，返回真正改变的句数。cueIDs 为空返回 0，不报错；
// speakerID 为空表示不改说话人，kind 为空表示不改类型，kind 非法时拒绝。
// 任一 cue 不存在则整批拒绝，不做部分写入：成片错误要么全改，要么保持原样。
func (w *Workbench) ReassignCues(draftID string, cueIDs []string, speakerID, kind string) (int, error) {
	if kind != "" && !slices.Contains(schema.CueKinds, kind) {
		return 0, msg.Err(msg.ErrSubtitleInvalid, nil)
	}
	if len(cueIDs) == 0 || (speakerID == "" && kind == "") {
		return 0, nil
	}
	w.edit.Lock()
	defer w.edit.Unlock()
	s := w.Store.Read()
	i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == draftID })
	if i < 0 {
		return 0, msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	document := s.Drafts[i].Subtitles
	if document == nil {
		return 0, msg.Err(msg.ErrSubtitleInvalid, nil)
	}
	if speakerID != "" && !slices.ContainsFunc(document.Speakers, func(speaker schema.SubtitleSpeaker) bool { return speaker.ID == speakerID }) {
		return 0, msg.Err(msg.ErrSpeakerInvalid, nil)
	}
	targets := make(map[string]bool, len(cueIDs))
	for _, cueID := range cueIDs {
		if !slices.ContainsFunc(document.Cues, func(cue schema.SubtitleCue) bool { return cue.ID == cueID }) {
			return 0, msg.Err(msg.ErrSubtitleInvalid, nil)
		}
		targets[cueID] = true
	}
	pending := 0
	for _, cue := range document.Cues {
		if targets[cue.ID] && cueNeedsReassign(cue, speakerID, kind) {
			pending++
		}
	}
	if pending == 0 {
		return 0, nil
	}
	changed := 0
	err := w.Store.Update(func(s *schema.State) {
		if i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == draftID }); i >= 0 && s.Drafts[i].Subtitles != nil {
			for j := range s.Drafts[i].Subtitles.Cues {
				cue := &s.Drafts[i].Subtitles.Cues[j]
				if targets[cue.ID] && cueNeedsReassign(*cue, speakerID, kind) {
					reassignCue(cue, speakerID, kind)
					changed++
				}
			}
		}
	}, true)
	if err != nil {
		return 0, err
	}
	return changed, nil
}

// cueNeedsReassign 判断改派是否会改动这一句：说话人或类型不同，或机器置信度还在（人工决定后要清空）。
func cueNeedsReassign(cue schema.SubtitleCue, speakerID, kind string) bool {
	return (speakerID != "" && cue.SpeakerID != speakerID) || (kind != "" && cue.Kind != kind) || cue.Confidence != nil
}

// reassignCue 把改派落到一句上：空说话人或空类型保持原值，置信度总是清空。
func reassignCue(cue *schema.SubtitleCue, speakerID, kind string) {
	if speakerID != "" {
		cue.SpeakerID = speakerID
	}
	if kind != "" {
		cue.Kind = kind
	}
	cue.Confidence = nil
}
