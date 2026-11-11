package workbench

import (
	"testing"
	"time"

	"yovoice/internal/schema"
)

// 验收写入：fail 缺少说明被拒且不改动原记录，合法判定写入后带判定时间。
func TestRecordQARequiresNoteOnFail(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")

	if err = w.RecordQA(d.ID, schema.QARecord{Verdict: schema.QAVerdictFail}); err == nil {
		t.Fatal("fail 缺少说明应被拒绝")
	}
	if got := w.Store.Read().Drafts[0].QA; got != nil {
		t.Fatalf("被拒绝的验收不应写入：%+v", got)
	}
	if err = w.RecordQA(d.ID, schema.QARecord{Verdict: "ok"}); err == nil {
		t.Fatal("非法判定应被拒绝")
	}
	if err = w.RecordQA("missing", schema.QARecord{Verdict: schema.QAVerdictPass}); err == nil {
		t.Fatal("不存在的作品应被拒绝")
	}

	rec := schema.QARecord{WER: .031, LUFS: -19.2, Peak: -3.4, Silences: 2, Verdict: schema.QAVerdictFail, Note: "第 2 段有 2 秒空洞"}
	must(t, w.RecordQA(d.ID, rec))
	got := w.Store.Read().Drafts[0].QA
	if got == nil || got.Verdict != schema.QAVerdictFail || got.Note != rec.Note || got.WER != rec.WER || got.LUFS != rec.LUFS || got.Peak != rec.Peak || got.Silences != rec.Silences {
		t.Fatalf("验收结果未按原值写入：%+v", got)
	}
	if _, err = time.Parse(time.RFC3339, got.At); err != nil {
		t.Fatalf("缺少判定时间时应补 RFC3339：%q", got.At)
	}
	// 失败的章仍然算验收过：进度停在 qa，由 Note 说明为什么失败。
	if stage, err := w.ChapterProgress(d.ID); err != nil || stage != StageQA {
		t.Fatalf("验收后应停在 qa：%q %v", stage, err)
	}
}

// 进度是纯派生：pending 视为 imported，闸门推进依次映射 read/attributed/verified，
// 有生成版本即 rendered，盖过验收章即 qa。
func TestChapterProgressAcrossGateStages(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	stage := func() ChapterStage {
		t.Helper()
		got, e := w.ChapterProgress(d.ID)
		must(t, e)
		return got
	}

	if got := stage(); got != StageImported {
		t.Fatalf("未读的章应停在 imported：%q", got)
	}
	must(t, w.ConfirmRead(d.ID))
	if got := stage(); got != StageRead {
		t.Fatalf("读过应到 read：%q", got)
	}
	must(t, w.Store.Update(func(s *schema.State) {
		for i := range s.Drafts {
			if s.Drafts[i].ID == d.ID {
				s.Drafts[i].Gate = &schema.Gate{Status: schema.GateAttributed, TextSHA: schema.TextSHA(d.Text)}
			}
		}
	}, true))
	if got := stage(); got != StageAttributed {
		t.Fatalf("归因后应到 attributed：%q", got)
	}
	must(t, w.VerifyChapter(d.ID, "通读确认"))
	if got := stage(); got != StageVerified {
		t.Fatalf("确认后应到 verified：%q", got)
	}

	// 章节的分句生成快照以作品 ID 关联；只要存在版本，就说明这一章渲染过。
	must(t, w.Store.Update(func(s *schema.State) {
		s.History = append(s.History, schema.Generation{ID: schema.NewID(), Settings: schema.Draft{ID: d.ID}})
	}, true))
	if got := stage(); got != StageRendered {
		t.Fatalf("有生成版本应到 rendered：%q", got)
	}
	must(t, w.RecordQA(d.ID, schema.QARecord{Verdict: schema.QAVerdictPass}))
	if got := stage(); got != StageQA {
		t.Fatalf("验收后应到 qa：%q", got)
	}
	if _, err = w.ChapterProgress("missing"); err == nil {
		t.Fatal("不存在的作品应报错")
	}
}

// unchecked 不算验收过，进度仍停在渲染阶段。
func TestChapterProgressIgnoresUncheckedQA(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	must(t, w.RecordQA(d.ID, schema.QARecord{Verdict: schema.QAVerdictUnchecked}))
	got, err := w.ChapterProgress(d.ID)
	must(t, err)
	if got != StageImported {
		t.Fatalf("unchecked 不应推进到 qa：%q", got)
	}
}
