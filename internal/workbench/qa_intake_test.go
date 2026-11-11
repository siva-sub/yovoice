package workbench

import (
	"strings"
	"testing"

	"yovoice/internal/schema"
)

func intakeQA(t *testing.T, w *Workbench, draftID string) *schema.QARecord {
	t.Helper()
	for _, d := range w.Store.Read().Drafts {
		if d.ID == draftID {
			return d.QA
		}
	}
	t.Fatalf("找不到作品 %s", draftID)
	return nil
}

// 默认阈值下 WER 6% 超过 5%：判 fail，note 必须点出 WER。
func TestIntakeQARToolFailsWEROverThreshold(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")

	must(t, w.IntakeQARTool(d.ID, QAReport{WER: .06, LUFS: -19.2, Peak: -4}))
	qa := intakeQA(t, w, d.ID)
	if qa == nil || qa.Verdict != schema.QAVerdictFail {
		t.Fatalf("WER 超标应判 fail：%+v", qa)
	}
	if !strings.Contains(qa.Note, "WER") {
		t.Fatalf("note 应说明是 WER 超标：%q", qa.Note)
	}
	if qa.WER != .06 || qa.LUFS != -19.2 || qa.Peak != -4 {
		t.Fatalf("原始指标应原样保存：%+v", qa)
	}
}

// 默认阈值下 −18 LUFS 高于 −19 LUFS：判 fail，note 必须提到响度。
func TestIntakeQARToolFailsLoudnessOverTarget(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")

	must(t, w.IntakeQARTool(d.ID, QAReport{WER: .03, LUFS: -18, Peak: -4}))
	qa := intakeQA(t, w, d.ID)
	if qa == nil || qa.Verdict != schema.QAVerdictFail {
		t.Fatalf("响度超标应判 fail：%+v", qa)
	}
	if !strings.Contains(qa.Note, "响度") {
		t.Fatalf("note 应说明是响度超标：%q", qa.Note)
	}
}

// 全部指标在默认阈值内：判 pass，不写 note，静音段数只记录不判失败。
func TestIntakeQARToolPassesWithinThresholds(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")

	must(t, w.IntakeQARTool(d.ID, QAReport{WER: .03, LUFS: -19.2, Peak: -4, Silences: 2}))
	qa := intakeQA(t, w, d.ID)
	if qa == nil || qa.Verdict != schema.QAVerdictPass {
		t.Fatalf("全部达标应判 pass：%+v", qa)
	}
	if qa.Note != "" {
		t.Fatalf("pass 不应带失败说明：%q", qa.Note)
	}
	if qa.Silences != 2 {
		t.Fatalf("静音段数应记录：%+v", qa)
	}
}

// 零值阈值逐项回落到默认值；显式给出的阈值优先。
func TestIntakeQARToolUsesCustomThresholds(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")

	must(t, w.IntakeQARTool(d.ID, QAReport{WER: .06, LUFS: -19.2, Peak: -4, Thresholds: Thresholds{WER: .1}}))
	if qa := intakeQA(t, w, d.ID); qa == nil || qa.Verdict != schema.QAVerdictPass {
		t.Fatalf("WER 阈值放宽到 10%% 后 6%% 应通过：%+v", qa)
	}

	must(t, w.IntakeQARTool(d.ID, QAReport{WER: .03, LUFS: -19.2, Peak: -2, Thresholds: Thresholds{Peak: -1}}))
	if qa := intakeQA(t, w, d.ID); qa == nil || qa.Verdict != schema.QAVerdictPass {
		t.Fatalf("真峰值上限放宽到 −1 dBTP 后 −2 dBTP 应通过：%+v", qa)
	}
}

// 多项超标时 note 逐项列出，不能只报第一项；目标作品不存在时拒绝写入。
func TestIntakeQARToolListsEveryExceededMetric(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")

	must(t, w.IntakeQARTool(d.ID, QAReport{WER: .12, LUFS: -15, Peak: -1}))
	qa := intakeQA(t, w, d.ID)
	if qa == nil || qa.Verdict != schema.QAVerdictFail {
		t.Fatalf("多项超标应判 fail：%+v", qa)
	}
	for _, want := range []string{"WER", "响度", "真峰值"} {
		if !strings.Contains(qa.Note, want) {
			t.Fatalf("note 缺少 %q：%q", want, qa.Note)
		}
	}
	if err = w.IntakeQARTool("missing", QAReport{WER: .03, LUFS: -19, Peak: -4}); err == nil {
		t.Fatal("不存在的作品应被拒绝")
	}
}
