package workbench

import (
	"fmt"
	"maps"
	"slices"

	"yovoice/internal/schema"
)

// 待办类型：该读、该校验、该渲染、该验收。与章节阶段同名，界面可以直接显示。
const (
	QueueRead   = "read"
	QueueVerify = "verify"
	QueueRender = "render"
	QueueQA     = "qa"
)

// QueueItem 是书级队列里的一件事；DraftID 指向要处理的作品，Chapter 是章号，Reason 是给人看的说明。
type QueueItem struct {
	Kind    string `json:"kind"`
	DraftID string `json:"draftId"`
	Chapter int    `json:"chapter"`
	Reason  string `json:"reason"`
}

// PendingQueue 派生一本书当前还没做完的事（ADR-0010/0012）：先读、再校验归因、再渲染、最后验收。
// 按章号升序返回，同一章的多段作品按段序排列；全部是只读派生，不写状态。
// bookID 为空时返回所有书的待办，按书架顺序排列，每本书内仍按章号升序。
// 章号取不到的作品跳过，避免把顺序号当成章号；未验收的判定只看 QAStatus，pass 与 fail 都算已验收。
func (w *Workbench) PendingQueue(bookID string) []QueueItem {
	state := w.Store.Read()
	books := state.Books
	if bookID != "" {
		i := slices.IndexFunc(state.Books, func(b schema.Book) bool { return b.ID == bookID })
		if i < 0 {
			return nil
		}
		books = state.Books[i : i+1]
	}
	rendered := map[string]bool{}
	for _, generation := range state.History {
		rendered[generation.Settings.ID] = true
	}
	items := make([]QueueItem, 0, len(books))
	for _, book := range books {
		items = append(items, pendingBookItems(state.Drafts, book, rendered)...)
	}
	return items
}

// pendingBookItems 派生一本书里还没做完的事，按章号升序、同一章按段序排列。
func pendingBookItems(drafts []schema.Draft, book schema.Book, rendered map[string]bool) []QueueItem {
	groups := map[int][]schema.Draft{}
	for _, draft := range drafts {
		if draft.BookID != book.ID || draft.Kind != "story" {
			continue
		}
		chapter, err := reviewDraftChapter(draft, book.Title)
		if err != nil {
			continue
		}
		groups[chapter] = append(groups[chapter], draft)
	}
	items := make([]QueueItem, 0, len(groups))
	for _, chapter := range slices.Sorted(maps.Keys(groups)) {
		parts := groups[chapter]
		slices.SortStableFunc(parts, reviewPartCompare)
		for _, draft := range parts {
			kind, reason, ok := pendingQueueAction(draft, rendered[draft.ID], chapter)
			if !ok {
				continue
			}
			items = append(items, QueueItem{Kind: kind, DraftID: draft.ID, Chapter: chapter, Reason: reason})
		}
	}
	return items
}

// pendingQueueAction 返回作品的下一件事：闸门优先，确认后才看渲染与验收。
// 已验收的章不再进队列；QA 判 fail 也算验收过，具体问题在 QA.Note 里。
func pendingQueueAction(draft schema.Draft, rendered bool, chapter int) (kind, reason string, ok bool) {
	switch draft.ChapterStatus() {
	case schema.GatePending:
		return QueueRead, fmt.Sprintf("第 %d 章还没读，先通读正文并取指纹", chapter), true
	case schema.GateRead, schema.GateAttributed:
		return QueueVerify, fmt.Sprintf("第 %d 章已读，校验说话人后确认归因", chapter), true
	case schema.GateVerified:
		if !rendered {
			return QueueRender, fmt.Sprintf("第 %d 章已确认归因，等待渲染", chapter), true
		}
		if draft.QAStatus() == schema.QAVerdictUnchecked {
			return QueueQA, fmt.Sprintf("第 %d 章已渲染，等待音频验收", chapter), true
		}
	}
	return "", "", false
}
