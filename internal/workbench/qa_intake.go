package workbench

import (
	"fmt"
	"strings"

	"yovoice/internal/schema"
)

// 验收阈值默认值（ADR-0013）：WER 不超过 5%，响度不高于 −19 LUFS，真峰值不超过 −3 dBTP。
const (
	defaultWERThreshold  = .05
	defaultLUFSThreshold = -19
	defaultPeakThreshold = -3
)

// Thresholds 是单章验收的阈值；零值字段在 IntakeQARTool 里逐项套用默认值。
type Thresholds struct {
	WER  float64 `json:"wer"`
	LUFS float64 `json:"lufs"`
	Peak float64 `json:"peak"`
}

// QAReport 是验收脚本或转写工具的输出：WER、整章响度、真峰值、静音段数与可选阈值。
// 脚本没有给出的阈值保持零值，由 IntakeQARTool 按默认值补齐。
type QAReport struct {
	WER        float64    `json:"wer"`
	LUFS       float64    `json:"lufs"`
	Peak       float64    `json:"peak"`
	Silences   int        `json:"silences"`
	Thresholds Thresholds `json:"thresholds"`
}

// DefaultThresholds 返回 ADR-0013 的默认验收阈值。
func DefaultThresholds() Thresholds {
	return Thresholds{WER: defaultWERThreshold, LUFS: defaultLUFSThreshold, Peak: defaultPeakThreshold}
}

// IntakeQARTool 把脚本或转写工具的输出转成 QARecord 并写入章节（ADR-0013）。
// 任一项超过阈值即判 fail，note 逐项列出超标项；全部达标判 pass，不写 note。
func (w *Workbench) IntakeQARTool(draftID string, report QAReport) error {
	thresholds := reportThresholds(report.Thresholds)
	var exceeded []string
	if report.WER > thresholds.WER {
		exceeded = append(exceeded, fmt.Sprintf("WER %.2f%% 超过阈值 %.2f%%", report.WER*100, thresholds.WER*100))
	}
	if report.LUFS > thresholds.LUFS {
		exceeded = append(exceeded, fmt.Sprintf("响度 %.2f LUFS 高于目标 %.2f LUFS", report.LUFS, thresholds.LUFS))
	}
	if report.Peak > thresholds.Peak {
		exceeded = append(exceeded, fmt.Sprintf("真峰值 %.2f dBTP 超过上限 %.2f dBTP", report.Peak, thresholds.Peak))
	}
	verdict, note := schema.QAVerdictPass, ""
	if len(exceeded) > 0 {
		verdict, note = schema.QAVerdictFail, strings.Join(exceeded, "；")
	}
	return w.RecordQA(draftID, schema.QARecord{
		WER:      report.WER,
		LUFS:     report.LUFS,
		Peak:     report.Peak,
		Silences: report.Silences,
		Verdict:  verdict,
		Note:     note,
	})
}

// reportThresholds 逐项补齐零值阈值；显式给出的负数或正数保持不变。
func reportThresholds(given Thresholds) Thresholds {
	filled := DefaultThresholds()
	if given.WER != 0 {
		filled.WER = given.WER
	}
	if given.LUFS != 0 {
		filled.LUFS = given.LUFS
	}
	if given.Peak != 0 {
		filled.Peak = given.Peak
	}
	return filled
}
