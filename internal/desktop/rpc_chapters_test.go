package desktop

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"

	"yovoice/internal/msg"
	"yovoice/internal/schema"
	"yovoice/internal/workbench"
)

// chapterRPCBook 建一本书，供章级 RPC 把作品归入。
func chapterRPCBook(t *testing.T, w *workbench.Workbench, id, title string) {
	t.Helper()
	must(t, w.Store.Update(func(s *schema.State) {
		s.Books = append(s.Books, schema.Book{ID: id, Title: title})
	}, true))
}

// chapterRPCStory 保存一个两句话的章节作品，说话人表固定为旁白与 Jet。
func chapterRPCStory(t *testing.T, w *workbench.Workbench, bookID, title string, texts ...string) schema.Draft {
	t.Helper()
	d := schema.DefaultDraft()
	d.Kind, d.BookID, d.Title = "story", bookID, title
	d.Text = strings.Join(texts, "\n")
	document := &schema.SubtitleDocument{Speakers: []schema.SubtitleSpeaker{{ID: "narrator", SourceName: "旁白"}, {ID: "jet", SourceName: "Jet"}}}
	for index, text := range texts {
		document.Cues = append(document.Cues, schema.SubtitleCue{ID: fmt.Sprintf("cue-%d", index), Start: int64(index) * 1000, End: int64(index+1) * 1000, Text: text, SpeakerID: "narrator", Kind: "narration"})
	}
	d.Subtitles = document
	must(t, w.SaveDraft(d))
	return d
}

// chapterCall 把输入编码成 JSON 后走桌面 RPC 分发器。
func chapterCall(t *testing.T, w *workbench.Workbench, method string, input any) (any, error) {
	t.Helper()
	data, err := json.Marshal(input)
	must(t, err)
	return Call(context.Background(), w, method, data)
}

// chapterGate 返回作品当前的闸门；作品不存在直接失败。
func chapterGate(t *testing.T, w *workbench.Workbench, id string) *schema.Gate {
	t.Helper()
	for _, d := range w.Store.Read().Drafts {
		if d.ID == id {
			return d.Gate
		}
	}
	t.Fatalf("找不到作品 %s", id)
	return nil
}

// 章级闸门 RPC：未读直接校验返回未包装的闸门错误码；确认读过、校验、重开各走通一次往返。
func TestCallChapterGate(t *testing.T) {
	w, err := workbench.New(t.TempDir())
	must(t, err)
	defer w.Close()
	chapterRPCBook(t, w, "vol-6", "第六卷")
	d := chapterRPCStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")

	if _, err = chapterCall(t, w, "chapter.verify", map[string]any{"id": d.ID}); err == nil {
		t.Fatal("未读的章节不能直接校验")
	}
	callErr, ok := err.(*msg.CallError)
	if !ok || callErr.Code != msg.ErrChapterNotVerified {
		t.Fatalf("闸门错误必须原样返回 chapterNotVerified，实际 %T %v", err, err)
	}

	if result, err := chapterCall(t, w, "chapter.confirmRead", map[string]any{"id": d.ID}); err != nil || result != true {
		t.Fatal(result, err)
	}
	if gate := chapterGate(t, w, d.ID); gate == nil || gate.Status != schema.GateRead || gate.TextSHA != schema.TextSHA(d.Text) {
		t.Fatalf("确认读过应记为 read 并记下正文指纹：%+v", gate)
	}
	if result, err := chapterCall(t, w, "chapter.verify", map[string]any{"id": d.ID, "note": "通读确认"}); err != nil || result != true {
		t.Fatal(result, err)
	}
	if gate := chapterGate(t, w, d.ID); gate == nil || gate.Status != schema.GateVerified || gate.Note != "通读确认" || gate.VerifiedAt == "" {
		t.Fatalf("校验应记为 verified 并保留说明：%+v", gate)
	}
	if result, err := chapterCall(t, w, "chapter.reopen", map[string]any{"id": d.ID}); err != nil || result != true {
		t.Fatal(result, err)
	}
	if gate := chapterGate(t, w, d.ID); gate == nil || gate.Status != schema.GatePending {
		t.Fatalf("重开应退回 pending：%+v", gate)
	}
}

// 验收 RPC：fail 缺少说明被拒且不写状态；合法结果按原值写入并补上判定时间。
func TestCallRecordQA(t *testing.T) {
	w, err := workbench.New(t.TempDir())
	must(t, err)
	defer w.Close()
	chapterRPCBook(t, w, "vol-6", "第六卷")
	d := chapterRPCStory(t, w, "vol-6", "第 1 章", "夜里。")

	if _, err = chapterCall(t, w, "chapter.recordQA", map[string]any{"id": d.ID, "qa": map[string]any{"verdict": "fail"}}); err == nil {
		t.Fatal("fail 缺少 note 应被拒绝")
	}
	callErr, ok := err.(*msg.CallError)
	if !ok || callErr.Code != msg.ErrRequestInvalid {
		t.Fatalf("缺少 note 应返回 requestInvalid，实际 %T %v", err, err)
	}
	if qa := w.Store.Read().Drafts[0].QA; qa != nil {
		t.Fatalf("被拒绝的验收不应写入：%+v", qa)
	}

	record := map[string]any{"wer": .031, "lufs": -19.2, "peak": -3.4, "silences": 2, "verdict": "pass"}
	if result, err := chapterCall(t, w, "chapter.recordQA", map[string]any{"id": d.ID, "qa": record}); err != nil || result != true {
		t.Fatal(result, err)
	}
	qa := w.Store.Read().Drafts[0].QA
	if qa == nil || qa.Verdict != schema.QAVerdictPass || qa.WER != .031 || qa.LUFS != -19.2 || qa.Peak != -3.4 || qa.Silences != 2 || qa.At == "" {
		t.Fatalf("验收结果未按原值写入：%+v", qa)
	}
}

// 待办 RPC：四种状态的章给出四类 kind；bookId 为空时返回所有书的待办。
func TestCallQueuePending(t *testing.T) {
	w, err := workbench.New(t.TempDir())
	must(t, err)
	defer w.Close()
	chapterRPCBook(t, w, "vol-6", "第六卷")
	chapterRPCBook(t, w, "vol-7", "第七卷")
	read := chapterRPCStory(t, w, "vol-6", "第 1 章", "未读。")
	verify := chapterRPCStory(t, w, "vol-6", "第 2 章", "已读未校验。")
	render := chapterRPCStory(t, w, "vol-6", "第 3 章", "已验证未渲染。")
	intake := chapterRPCStory(t, w, "vol-6", "第 4 章", "已渲染未验收。")
	done := chapterRPCStory(t, w, "vol-6", "第 5 章", "已验收。")
	other := chapterRPCStory(t, w, "vol-7", "第 1 章", "另一本书。")

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

	result, err := chapterCall(t, w, "queue.pending", map[string]any{"bookId": "vol-6"})
	must(t, err)
	queue, ok := result.([]workbench.QueueItem)
	if !ok {
		t.Fatalf("queue.pending 应返回 []workbench.QueueItem，实际 %T", result)
	}
	wantDrafts := []string{read.ID, verify.ID, render.ID, intake.ID}
	wantKinds := []string{workbench.QueueRead, workbench.QueueVerify, workbench.QueueRender, workbench.QueueQA}
	if len(queue) != len(wantKinds) {
		t.Fatalf("应有 %d 件待办：%+v", len(wantKinds), queue)
	}
	for i, item := range queue {
		if item.Chapter != i+1 || item.DraftID != wantDrafts[i] || item.Kind != wantKinds[i] || item.Reason == "" {
			t.Fatalf("第 %d 件待办不对：%+v", i+1, item)
		}
	}

	// 空 bookId 表示所有书：先第六卷的四件，再第七卷的一件。
	result, err = chapterCall(t, w, "queue.pending", map[string]any{})
	must(t, err)
	all, ok := result.([]workbench.QueueItem)
	if !ok {
		t.Fatalf("queue.pending 应返回 []workbench.QueueItem，实际 %T", result)
	}
	if len(all) != 5 || !slices.EqualFunc(all[:4], queue, func(a, b workbench.QueueItem) bool { return a == b }) || all[4].DraftID != other.ID || all[4].Kind != workbench.QueueRead {
		t.Fatalf("空 bookId 应返回所有书的待办：%+v", all)
	}
}
