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
