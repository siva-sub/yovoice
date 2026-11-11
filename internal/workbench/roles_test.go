package workbench

import (
	"strings"
	"testing"

	"yovoice/internal/schema"
)

// reviewer 可以改 speakerId 与 kind：通过权限检查后交给复核包导入，本地 cue 按三方合并更新。
func TestApplyReviewPackAllowsReviewerAssignments(t *testing.T) {
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

	changed, conflicts, err := w.ApplyReviewPack(reviewEncode(t, pack), schema.Reviewer{ID: "r1", Name: "审校甲", Role: schema.RoleReviewer})
	must(t, err)
	if changed != 1 || len(conflicts) != 0 {
		t.Fatalf("应写入 1 条改派且无冲突：changed=%d conflicts=%+v", changed, conflicts)
	}
	got := reviewLocalCue(t, w, d.ID, 0)
	if got.SpeakerID != "jet" || got.Kind != "dialogue" {
		t.Fatalf("reviewer 的改派未写入：%+v", got)
	}
}

// reviewer 改 text 被拒：错误里指明 text 字段，整包不写入，连同时改的 speakerId 也不生效。
func TestApplyReviewPackRejectsReviewerTextChange(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")

	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	pack := reviewDecode(t, data)
	pack.Chapters[0].Cues[0].Text = "改过的正文。"
	pack.Chapters[0].TextSHA = reviewChapterHash(pack.Chapters[0])
	pack.Chapters[0].Cues[0].SpeakerID = "jet"

	changed, conflicts, err := w.ApplyReviewPack(reviewEncode(t, pack), schema.Reviewer{ID: "r1", Name: "审校甲", Role: schema.RoleReviewer})
	if err == nil || !strings.Contains(err.Error(), "text") {
		t.Fatalf("越权改 text 应报出字段名：changed=%d conflicts=%+v err=%v", changed, conflicts, err)
	}
	if changed != 0 || len(conflicts) != 0 {
		t.Fatalf("拒绝后不应写入或产生冲突：changed=%d conflicts=%+v", changed, conflicts)
	}
	got := reviewLocalCue(t, w, d.ID, 0)
	if got.Text != "夜里。" || got.SpeakerID != "narrator" {
		t.Fatalf("越权包不应写入任何字段：%+v", got)
	}
}

// listener 只读：任何改派都被拒绝，错误里带具体字段；原样包可以安全通过（0 条变更）。
func TestApplyReviewPackRejectsListenerWrites(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")

	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	unchanged, conflicts, err := w.ApplyReviewPack(data, schema.Reviewer{ID: "l1", Name: "听众乙", Role: schema.RoleListener})
	must(t, err)
	if unchanged != 0 || len(conflicts) != 0 {
		t.Fatalf("listener 原样导入不应有改动：changed=%d conflicts=%+v", unchanged, conflicts)
	}

	pack := reviewDecode(t, data)
	pack.Chapters[0].Cues[0].SpeakerID = "jet"
	changed, conflicts, err := w.ApplyReviewPack(reviewEncode(t, pack), schema.Reviewer{ID: "l1", Name: "听众乙", Role: schema.RoleListener})
	if err == nil || !strings.Contains(err.Error(), "speakerId") {
		t.Fatalf("listener 改 speakerId 应被拒并报出字段名：changed=%d conflicts=%+v err=%v", changed, conflicts, err)
	}
	if got := reviewLocalCue(t, w, d.ID, 0).SpeakerID; got != "narrator" {
		t.Fatalf("listener 不应写入任何字段：%q", got)
	}
}

// 调用方没给角色时查角色表：登记为 listener 的 reviewer 仍按只读处理；未登记则默认只读。
func TestApplyReviewPackUsesStoredRoleWhenUnset(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	must(t, w.Store.Update(func(s *schema.State) {
		s.Reviewers = []schema.Reviewer{{ID: "listener-b", Name: "听众乙", Role: schema.RoleListener}}
	}, true))

	data, err := w.ExportReviewPack("vol-6", []int{1}, "owner")
	must(t, err)
	pack := reviewDecode(t, data)
	pack.Chapters[0].Cues[0].SpeakerID = "jet"

	_, _, err = w.ApplyReviewPack(reviewEncode(t, pack), schema.Reviewer{ID: "listener-b", Name: "听众乙"})
	if err == nil || !strings.Contains(err.Error(), "speakerId") {
		t.Fatalf("角色表里的 listener 应按只读拒绝：%v", err)
	}
}
