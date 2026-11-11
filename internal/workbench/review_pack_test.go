package workbench

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"yovoice/internal/schema"
)

// reviewTestBook 在状态里登记一本卷，复核包按 BookID 找章节。
func reviewTestBook(t *testing.T, w *Workbench, id, title string) {
	t.Helper()
	must(t, w.Store.Update(func(s *schema.State) {
		s.Books = append(s.Books, schema.Book{ID: id, Title: title})
	}, true))
}

// reviewTestStory 保存一个故事作品：三个说话人固定存在，便于测试改派与两边都改。
func reviewTestStory(t *testing.T, w *Workbench, bookID, title string, texts ...string) schema.Draft {
	t.Helper()
	d := schema.DefaultDraft()
	d.Kind = "story"
	d.BookID = bookID
	d.Title = title
	d.Text = strings.Join(texts, "\n")
	confidence := .4
	document := &schema.SubtitleDocument{Speakers: []schema.SubtitleSpeaker{
		{ID: "narrator", SourceName: "旁白"},
		{ID: "narrator2", SourceName: "旁白二"},
		{ID: "jet", SourceName: "Jet", CharacterID: "jet"},
	}}
	for index, text := range texts {
		document.Cues = append(document.Cues, schema.SubtitleCue{
			ID: fmt.Sprintf("%s-%d", d.ID[:8], index), Start: int64(index) * 1000, End: int64(index+1) * 1000,
			Text: text, SpeakerID: "narrator", Kind: "narration", Confidence: &confidence,
		})
	}
	d.Subtitles = document
	must(t, w.SaveDraft(d))
	return d
}

func reviewDecode(t *testing.T, data []byte) ReviewPack {
	t.Helper()
	var pack ReviewPack
	must(t, json.Unmarshal(data, &pack))
	return pack
}

func reviewEncode(t *testing.T, pack ReviewPack) []byte {
	t.Helper()
	data, err := json.Marshal(pack)
	must(t, err)
	return data
}

// reviewDraftsJSON 用作“数据未被修改”的快照；排序与 map 序列化都是稳定的。
func reviewDraftsJSON(t *testing.T, w *Workbench) string {
	t.Helper()
	data, err := json.Marshal(w.Store.Read().Drafts)
	must(t, err)
	return string(data)
}

func reviewLocalCue(t *testing.T, w *Workbench, draftID string, index int) schema.SubtitleCue {
	t.Helper()
	for _, d := range w.Store.Read().Drafts {
		if d.ID == draftID && d.Subtitles != nil {
			return d.Subtitles.Cues[index]
		}
	}
	t.Fatalf("找不到作品 %s", draftID)
	return schema.SubtitleCue{}
}

func reviewSetSpeaker(t *testing.T, w *Workbench, draftID string, index int, speakerID string) {
	t.Helper()
	must(t, w.Store.Update(func(s *schema.State) {
		for i := range s.Drafts {
			if s.Drafts[i].ID == draftID {
				s.Drafts[i].Subtitles.Cues[index].SpeakerID = speakerID
			}
		}
	}, true))
}

// reviewChapterHash 从复核章节的 cue 正文重算指纹，用来模拟 reviewer 真的改了正文并同步了 textSha。
func reviewChapterHash(chapter ReviewChapter) string {
	cues := reviewSortedCues(chapter.Cues)
	texts := make([]string, 0, len(cues))
	for _, cue := range cues {
		texts = append(texts, cue.Text)
	}
	return schema.TextSHA(strings.Join(texts, "\n"))
}

// 被拆成两段的第 1229 章合并后 index 必须连续、总数等于两段之和，顺序与原文一致。
// 这是唯一能暴露“段偏移累加写错”的用例。
func TestExportReviewPackMergesChapterParts(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	// 先保存第一段再保存第二段：列表里新作品在前，导出必须按段号恢复原始顺序。
	reviewTestStory(t, w, "vol-6", "第 1229 章（1/2）", "一。", "二。")
	second := reviewTestStory(t, w, "vol-6", "第 1229 章（2/2）", "三。", "四。", "五。")

	data, err := w.ExportReviewPack("vol-6", []int{1229}, "owner")
	must(t, err)
	pack := reviewDecode(t, data)
	if pack.Version != reviewPackVersion || pack.BookID != "vol-6" || pack.Reviewer != "owner" {
		t.Fatalf("复核包头部不对：%+v", pack)
	}
	if len(pack.Chapters) != 1 {
		t.Fatalf("应合并成一章：%+v", pack.Chapters)
	}
	chapter := pack.Chapters[0]
	if chapter.Chapter != 1229 || len(chapter.Cues) != 5 {
		t.Fatalf("第 1229 章应有 5 条 cue：%+v", chapter)
	}
	want := []string{"一。", "二。", "三。", "四。", "五。"}
	for index, cue := range chapter.Cues {
		if cue.Index != index {
			t.Fatalf("cue 下标应从 0 连续：第 %d 条是 %d", index, cue.Index)
		}
		if cue.Text != want[index] {
			t.Fatalf("cue 顺序不对：第 %d 条是 %q，应为 %q", index, cue.Text, want[index])
		}
	}
	if chapter.TextSHA != schema.TextSHA(strings.Join(want, "\n")) {
		t.Fatalf("合并后的正文指纹不对：%q", chapter.TextSHA)
	}
	if len(chapter.Speakers) != 1 || chapter.Speakers[0].ID != "narrator" || chapter.Speakers[0].SourceName != "旁白" {
		t.Fatalf("应带实际用到的说话人表：%+v", chapter.Speakers)
	}
	if second.ID == "" {
		t.Fatal("第二段应已保存")
	}
}

// Chapter 字段优先于标题；没有 Chapter 时回落到标题末尾的数字；标题取不到数字要报出书与标题。
func TestReviewChapterResolution(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 9 章", "正文。")
	must(t, w.Store.Update(func(s *schema.State) {
		for i := range s.Drafts {
			if s.Drafts[i].ID == d.ID {
				s.Drafts[i].Chapter = 7
			}
		}
	}, true))
	if _, err = w.ExportReviewPack("vol-6", []int{7}, "owner"); err != nil {
		t.Fatalf("应优先使用 Chapter=7：%v", err)
	}
	if _, err = w.ExportReviewPack("vol-6", []int{9}, "owner"); err == nil {
		t.Fatal("标题里的 9 不应覆盖已设置的 Chapter")
	}

	// 新导入的标题是“书名 · 061”，没有 Chapter 时也要能取到 61。
	reviewTestStory(t, w, "vol-6", "第六卷 · 061", "新正文。")
	data, err := w.ExportReviewPack("vol-6", []int{61}, "owner")
	must(t, err)
	if pack := reviewDecode(t, data); len(pack.Chapters) != 1 || pack.Chapters[0].Chapter != 61 {
		t.Fatalf("应回落到标题数字：%+v", pack)
	}
}

func TestReviewChapterWithoutNumberReportsBookAndTitle(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	reviewTestStory(t, w, "vol-6", "序章", "正文。")
	_, err = w.ExportReviewPack("vol-6", []int{1}, "owner")
	if err == nil || !strings.Contains(err.Error(), "第六卷") || !strings.Contains(err.Error(), "序章") {
		t.Fatalf("错误里应带卷名与标题：%v", err)
	}
}

// 导出后原样导入没有任何变更，也不写状态。
func TestReviewPackRoundTripHasNoChanges(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	before := reviewDraftsJSON(t, w)

	data, err := w.ExportReviewPack("vol-6", nil, "owner")
	must(t, err)
	changed, conflicts, err := w.ImportReviewPack(data, "owner")
	must(t, err)
	if changed != 0 || len(conflicts) != 0 {
		t.Fatalf("原样往返不应有变更：changed=%d conflicts=%+v", changed, conflicts)
	}
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("原样往返改动了数据")
	}
}

// 章正文改过一个字：整章进 conflicts，且本地数据完全没被修改。
func TestReviewPackRejectsChangedChapterWithoutTouchingData(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	before := reviewDraftsJSON(t, w)

	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	pack := reviewDecode(t, data)
	// 模拟 reviewer 改了正文并同步重算指纹：导入时与本地 textSha 不一致。
	pack.Chapters[0].Cues[0].Text = "深夜里。"
	pack.Chapters[0].TextSHA = reviewChapterHash(pack.Chapters[0])

	changed, conflicts, err := w.ImportReviewPack(reviewEncode(t, pack), "reviewer-a")
	must(t, err)
	if changed != 0 || len(conflicts) != 1 {
		t.Fatalf("改过正文的章应整章拒绝：changed=%d conflicts=%+v", changed, conflicts)
	}
	conflict := conflicts[0]
	if conflict.Kind != ReviewConflictTextSHA || conflict.Chapter != 1 || conflict.TextSHA != pack.Chapters[0].TextSHA || conflict.LocalTextSHA == "" || conflict.TextSHA == conflict.LocalTextSHA {
		t.Fatalf("冲突应记录两边的正文指纹：%+v", conflict)
	}
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("整章拒绝时数据被修改了")
	}
	if got := reviewLocalCue(t, w, d.ID, 0); got.Text != "夜里。" {
		t.Fatalf("正文不应被复核包改写：%q", got.Text)
	}
}

// owner 单独改过、reviewer 没改这一句：导入后必须保留 owner 的值，不能被基线覆盖。
func TestReviewPackKeepsOwnerChange(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	reviewSetSpeaker(t, w, d.ID, 0, "jet")

	changed, conflicts, err := w.ImportReviewPack(data, "reviewer-a")
	must(t, err)
	if changed != 0 || len(conflicts) != 0 {
		t.Fatalf("reviewer 没改时不应写入或提议：changed=%d conflicts=%+v", changed, conflicts)
	}
	if got := reviewLocalCue(t, w, d.ID, 0).SpeakerID; got != "jet" {
		t.Fatalf("owner 的改派被覆盖了：%q", got)
	}
}

// reviewer 单独改一句：应用并清空机器置信度。
func TestReviewPackAppliesReviewerChange(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	pack := reviewDecode(t, data)
	pack.Chapters[0].Cues[0].SpeakerID = "jet"
	pack.Chapters[0].Cues[0].Kind = "dialogue"

	changed, conflicts, err := w.ImportReviewPack(reviewEncode(t, pack), "reviewer-a")
	must(t, err)
	if changed != 1 || len(conflicts) != 0 {
		t.Fatalf("应应用一条改派：changed=%d conflicts=%+v", changed, conflicts)
	}
	cue := reviewLocalCue(t, w, d.ID, 0)
	if cue.SpeakerID != "jet" || cue.Kind != "dialogue" {
		t.Fatalf("改派未写入：%+v", cue)
	}
	if cue.Confidence != nil {
		t.Fatal("人工改派后应清空机器置信度")
	}
}

// 两边都改过同一句：只返回 proposal（带 reviewer/at/from/to），绝不自动采用。
func TestReviewPackProposesWhenBothChanged(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	reviewSetSpeaker(t, w, d.ID, 0, "narrator2")

	pack := reviewDecode(t, data)
	pack.Chapters[0].Cues[0].SpeakerID = "jet"
	before := reviewDraftsJSON(t, w)

	changed, conflicts, err := w.ImportReviewPack(reviewEncode(t, pack), "reviewer-a")
	must(t, err)
	if changed != 0 || len(conflicts) != 1 {
		t.Fatalf("两边都改应只产生 proposal：changed=%d conflicts=%+v", changed, conflicts)
	}
	proposal := conflicts[0]
	if proposal.Kind != ReviewConflictProposal || proposal.Chapter != 1 || proposal.Key != "1:0" {
		t.Fatalf("proposal 标识不对：%+v", proposal)
	}
	if proposal.Reviewer != "reviewer-a" || proposal.From == nil || proposal.To == nil || proposal.From.SpeakerID != "narrator2" || proposal.To.SpeakerID != "jet" {
		t.Fatalf("proposal 审计不对：%+v", proposal)
	}
	if _, err = time.Parse(time.RFC3339, proposal.At); err != nil {
		t.Fatalf("proposal 应带 RFC3339 时间：%q", proposal.At)
	}
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("proposal 不应自动修改数据")
	}
}
