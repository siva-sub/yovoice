package schema

import (
	"strings"
	"time"

	"yovoice/internal/msg"
)

// 验收判定。qa 是章节流水线上渲染之后的验收结果（ADR-0013）；没有验收记录或判定为空时视为 unchecked。
const (
	QAVerdictPass      = "pass"
	QAVerdictFail      = "fail"
	QAVerdictUnchecked = "unchecked"
)

// QARecord 是章节的音频验收结果：WER、整章响度、真峰值、静音段数与判定。
// At 是判定时间（RFC3339）；Verdict=fail 时必须用 Note 说明原因，否则失败无法复核。
type QARecord struct {
	WER      float64 `json:"wer"`
	LUFS     float64 `json:"lufs"`
	Peak     float64 `json:"peak"`
	Silences int     `json:"silences"`
	Verdict  string  `json:"verdict"`
	At       string  `json:"at,omitempty"`
	Note     string  `json:"note,omitempty"`
}

// QAStatus 返回章节的验收判定；旧作品没有验收记录时按 unchecked。
func (d Draft) QAStatus() string {
	if d.QA == nil || d.QA.Verdict == "" {
		return QAVerdictUnchecked
	}
	return d.QA.Verdict
}

// ValidateQA 校验验收结果：判定三选一，fail 必须带说明，At 有值时必须是 RFC3339。
func ValidateQA(rec QARecord) error {
	switch rec.Verdict {
	case QAVerdictPass, QAVerdictFail, QAVerdictUnchecked:
	case "":
		return msg.Err(msg.ErrRequestInvalid, nil)
	default:
		return msg.Err(msg.ErrRequestInvalid, nil)
	}
	if rec.Verdict == QAVerdictFail && strings.TrimSpace(rec.Note) == "" {
		return msg.Err(msg.ErrRequestInvalid, nil)
	}
	if rec.At != "" {
		if _, err := time.Parse(time.RFC3339, rec.At); err != nil {
			return msg.Err(msg.ErrRequestInvalid, nil)
		}
	}
	return nil
}
