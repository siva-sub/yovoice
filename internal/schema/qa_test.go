package schema

import (
	"encoding/json"
	"strings"
	"testing"
)

// 验收判定只允许三选一；fail 必须说明原因，时间有值时必须是 RFC3339。
func TestValidateQA(t *testing.T) {
	valid := []QARecord{
		{Verdict: QAVerdictPass, At: "2026-11-11T00:00:00Z"},
		{Verdict: QAVerdictFail, Note: "第 3 句人名读错", At: "2026-11-11T00:00:00Z"},
		{Verdict: QAVerdictUnchecked},
	}
	for _, rec := range valid {
		if err := ValidateQA(rec); err != nil {
			t.Fatalf("应接受 %+v：%v", rec, err)
		}
	}
	invalid := []QARecord{
		{Verdict: ""},
		{Verdict: "ok"},
		{Verdict: QAVerdictFail},
		{Verdict: QAVerdictFail, Note: "   "},
		{Verdict: QAVerdictPass, At: "2026-11-11 00:00"},
	}
	for _, rec := range invalid {
		if err := ValidateQA(rec); err == nil {
			t.Fatalf("应拒绝 %+v", rec)
		}
	}
}

// 没有验收记录与章号的旧作品必须能原样往返；带字段的新数据也能读回。
func TestDraftQAOptionalRoundTrip(t *testing.T) {
	old := Draft{ID: "id", Title: "旧作品", Text: "正文"}
	data, err := json.Marshal(old)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), `"qa"`) || strings.Contains(string(data), `"chapter"`) {
		t.Fatalf("未设置的字段不应出现在 JSON 里：%s", data)
	}
	var decoded Draft
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.QA != nil || decoded.Chapter != 0 {
		t.Fatalf("旧数据反序列化后应为空：%+v", decoded)
	}

	with := old
	with.QA = &QARecord{WER: .02, LUFS: -19.1, Peak: -3.2, Silences: 1, Verdict: QAVerdictPass, At: "2026-11-11T00:00:00Z"}
	with.Chapter = 1229
	data, err = json.Marshal(with)
	if err != nil {
		t.Fatal(err)
	}
	decoded = Draft{}
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.QA == nil || decoded.QA.WER != .02 || decoded.QA.Verdict != QAVerdictPass || decoded.Chapter != 1229 {
		t.Fatalf("新字段未往返：%+v", decoded)
	}
}

// 没有验收记录的旧作品按 unchecked 处理，不能因为缺字段就报错或当成通过。
func TestQAStatusDefaultsToUnchecked(t *testing.T) {
	if got := (Draft{}).QAStatus(); got != QAVerdictUnchecked {
		t.Fatalf("缺少验收记录应为 unchecked：%q", got)
	}
	if got := (Draft{QA: &QARecord{Verdict: QAVerdictPass}}).QAStatus(); got != QAVerdictPass {
		t.Fatalf("应返回记录中的判定：%q", got)
	}
}
