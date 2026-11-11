package schema

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

// 闸门四个状态与正文指纹：read/attributed/verified 必须带 16 位十六进制摘要，verified 还要带确认时间。
func TestValidateGate(t *testing.T) {
	const sha = "0123456789abcdef"
	for _, tc := range []struct {
		name  string
		gate  *Gate
		valid bool
	}{
		{"没有闸门的旧作品", nil, true},
		{"pending", &Gate{Status: GatePending}, true},
		{"read", &Gate{Status: GateRead, TextSHA: sha}, true},
		{"attributed", &Gate{Status: GateAttributed, TextSHA: sha}, true},
		{"verified", &Gate{Status: GateVerified, TextSHA: sha, VerifiedAt: "2026-11-11T00:00:00Z"}, true},
		{"未知状态", &Gate{Status: "done"}, false},
		{"状态为空", &Gate{}, false},
		{"read 缺少摘要", &Gate{Status: GateRead}, false},
		{"摘要大写", &Gate{Status: GateRead, TextSHA: "0123456789ABCDEF"}, false},
		{"摘要过短", &Gate{Status: GateRead, TextSHA: "0123456789abcde"}, false},
		{"摘要过长", &Gate{Status: GateRead, TextSHA: sha + "0"}, false},
		{"摘要含非十六进制字符", &Gate{Status: GateAttributed, TextSHA: "0123456789abcdeg"}, false},
		{"verified 缺少确认时间", &Gate{Status: GateVerified, TextSHA: sha}, false},
		{"确认时间不是 RFC3339", &Gate{Status: GateVerified, TextSHA: sha, VerifiedAt: "2026-11-11 00:00"}, false},
	} {
		err := ValidateGate(tc.gate)
		if (err == nil) != tc.valid {
			t.Errorf("%s: err = %v", tc.name, err)
		}
	}
	if err := (&Draft{Kind: "story"}).ValidateChapter(); err != nil {
		t.Fatal("没有闸门的旧作品应能通过章节校验", err)
	}
	if err := (&Draft{Kind: "story", Gate: &Gate{Status: GateRead}}).ValidateChapter(); err == nil {
		t.Fatal("非法闸门应被章节校验拒绝")
	}
}

// 台词的置信度必须在 [0,1]，类型与判据标记只允许契约列出的值；三者都可缺省。
func TestValidateCueMarks(t *testing.T) {
	zero, one, middle := 0.0, 1.0, .5
	tooLow, tooHigh, nan := -0.1, 1.1, math.NaN()
	valid := []SubtitleCue{
		{},
		{Confidence: &zero},
		{Confidence: &one},
		{Confidence: &middle},
		{Kind: "narration"},
		{Kind: "dialogue"},
		{Kind: "thought"},
	}
	for _, evidence := range []string{"prose", "inner_monologue", "lead_next", "lead_prev", "tag_next", "tag_prev", "alternation", "addressed_inverse", "continuity"} {
		valid = append(valid, SubtitleCue{Evidence: evidence})
	}
	for _, cue := range valid {
		if err := ValidateCueMarks(cue); err != nil {
			t.Errorf("%+v 应通过校验：%v", cue, err)
		}
	}
	for _, cue := range []SubtitleCue{
		{Confidence: &tooLow},
		{Confidence: &tooHigh},
		{Confidence: &nan},
		{Kind: "story"},
		{Evidence: "lead"},
	} {
		if err := ValidateCueMarks(cue); err == nil {
			t.Errorf("%+v 应被拒绝", cue)
		}
	}
	// 保存与生成都经过 SubtitleDrafts，台词的非法标记在那里被拦住。
	bad := DefaultDraft()
	bad.Subtitles = &SubtitleDocument{Speakers: []SubtitleSpeaker{{ID: "1"}}, Cues: []SubtitleCue{{Start: 0, End: 1, Text: bad.Text, SpeakerID: "1", Evidence: "lead"}}}
	if _, err := bad.SubtitleDrafts(); err == nil {
		t.Fatal("非法判据标记应被字幕校验拒绝")
	}
	good := DefaultDraft()
	good.Subtitles = &SubtitleDocument{Speakers: []SubtitleSpeaker{{ID: "1"}}, Cues: []SubtitleCue{{Start: 0, End: 1, Text: good.Text, SpeakerID: "1", Evidence: "prose", Kind: "narration", Confidence: &middle}}}
	if _, err := good.SubtitleDrafts(); err != nil {
		t.Fatal("带置信度与判据的台词应通过字幕校验", err)
	}
}

// kind=story 且没有闸门的旧数据视为 pending，读取不报错；带闸门时按其状态返回。
func TestStoryWithoutGateIsPending(t *testing.T) {
	var legacy Draft
	if err := json.Unmarshal([]byte(`{"id":"old","kind":"story","title":"旧章节","text":"正文"}`), &legacy); err != nil {
		t.Fatal("旧作品应能读取", err)
	}
	if legacy.Gate != nil || legacy.ChapterStatus() != GatePending {
		t.Fatalf("旧作品应视为 pending：%+v", legacy.Gate)
	}
	if err := legacy.ValidateChapter(); err != nil {
		t.Fatal("旧作品缺省不应报错", err)
	}
	legacy.Gate = &Gate{Status: GateRead, TextSHA: TextSHA(legacy.Text)}
	if legacy.ChapterStatus() != GateRead {
		t.Fatal("ChapterStatus 应返回闸门状态")
	}
	// 正文指纹固定为 sha256 十六进制前 16 位，正文一变就不同。
	if sha := TextSHA("正文"); len(sha) != 16 || strings.Trim(sha, "0123456789abcdef") != "" || sha == TextSHA("正文。") {
		t.Fatalf("正文指纹格式不对：%q", sha)
	}
}

// 卷标识：非空、小写字母数字与连字符、最长 64 位；作品的归属只在非空时校验格式。
func TestValidateBookIdentity(t *testing.T) {
	for _, id := range []string{UnfiledBookID, "book-01", "b", strings.Repeat("a", 64)} {
		if err := ValidateBookID(id); err != nil {
			t.Errorf("%q 应通过校验：%v", id, err)
		}
	}
	for _, id := range []string{"", "Unfiled", "book_01", "book/01", "书", strings.Repeat("a", 65)} {
		if err := ValidateBookID(id); err == nil {
			t.Errorf("%q 应被拒绝", id)
		}
	}
	if err := (&Draft{BookID: UnfiledBookID}).ValidateChapter(); err != nil {
		t.Fatal("合法归属应通过章节校验", err)
	}
	if err := (&Draft{BookID: "未归档"}).ValidateChapter(); err == nil {
		t.Fatal("非法归属应被章节校验拒绝")
	}
	if len(DefaultState().Books) != 1 || DefaultState().Books[0].ID != UnfiledBookID || DefaultState().Books[0].Title != "未归档" {
		t.Fatalf("默认状态应含“未归档”卷：%+v", DefaultState().Books)
	}
}
