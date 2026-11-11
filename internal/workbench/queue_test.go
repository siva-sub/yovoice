package workbench

import (
	"strings"
	"testing"

	"yovoice/internal/schema"
)

// 四种章状态分别派生四类待办，按章号升序，Reason 是可直接显示的中文。
func TestPendingQueueDerivesFourStages(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")

	read := reviewTestStory(t, w, "vol-6", "第 1 章", "未读。")
	verify := reviewTestStory(t, w, "vol-6", "第 2 章", "已读未校验。")
	render := reviewTestStory(t, w, "vol-6", "第 3 章", "已验证未渲染。")
	intake := reviewTestStory(t, w, "vol-6", "第 4 章", "已渲染未验收。")
	done := reviewTestStory(t, w, "vol-6", "第 5 章", "已验收。")

	must(t, w.ConfirmRead(verify.ID))
	for _, id := range []string{render.ID, intake.ID, done.ID} {
		must(t, w.ConfirmRead(id))
		must(t, w.VerifyChapter(id, ""))
	}
	must(t, w.Store.Update(func(s *schema.State) {
		for _, id := range []string{intake.ID, done.ID} {
			s.History = append(s.History, schema.Generation{ID: schema.NewID(), Settings: schema.Draft{ID: id}})
		}
	}, true))
	must(t, w.RecordQA(done.ID, schema.QARecord{Verdict: schema.QAVerdictPass}))

	before := reviewDraftsJSON(t, w)
	queue := w.PendingQueue("vol-6")
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("待办队列是纯派生，不应写状态")
	}
	wantDrafts := []string{read.ID, verify.ID, render.ID, intake.ID}
	wantKinds := []string{QueueRead, QueueVerify, QueueRender, QueueQA}
	wantReasons := []string{"读", "校验", "渲染", "验收"}
	if len(queue) != len(wantKinds) {
		t.Fatalf("应有 %d 件待办：%+v", len(wantKinds), queue)
	}
	for i, item := range queue {
		if item.Chapter != i+1 || item.DraftID != wantDrafts[i] || item.Kind != wantKinds[i] {
			t.Fatalf("第 %d 件待办不对：%+v", i+1, item)
		}
		if !strings.Contains(item.Reason, wantReasons[i]) {
			t.Fatalf("第 %d 件待办的说明缺少 %q：%q", i+1, wantReasons[i], item.Reason)
		}
	}
	if queue := w.PendingQueue("missing"); len(queue) != 0 {
		t.Fatalf("不存在的卷不应有待办：%+v", queue)
	}
}

// 已读与已归因都还需要 owner 确认；确认后不再重复出现在校验待办里。
func TestPendingQueueVerifyCoversReadAndAttributed(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	read := reviewTestStory(t, w, "vol-6", "第 1 章", "已读。")
	attributed := reviewTestStory(t, w, "vol-6", "第 2 章", "已归因。")
	must(t, w.ConfirmRead(read.ID))
	must(t, w.ConfirmRead(attributed.ID))
	must(t, w.Store.Update(func(s *schema.State) {
		for i := range s.Drafts {
			if s.Drafts[i].ID == attributed.ID {
				s.Drafts[i].Gate.Status = schema.GateAttributed
			}
		}
	}, true))

	queue := w.PendingQueue("vol-6")
	if len(queue) != 2 {
		t.Fatalf("两章都该等确认：%+v", queue)
	}
	for _, item := range queue {
		if item.Kind != QueueVerify || !strings.Contains(item.Reason, "确认") {
			t.Fatalf("已读/已归因都应派生校验待办：%+v", item)
		}
	}
}
