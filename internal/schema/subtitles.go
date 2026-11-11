package schema

import (
	"fmt"
	"slices"
	"strings"
	"yovoice/internal/msg"
)

// performanceSettings 优先使用该句演绎的完整模型和参数。
func (s SynthesisSettings) PerformanceSettings(p *CharacterPerformance) SynthesisSettings {
	if p != nil {
		return p.Settings
	}
	return s
}

// subtitleDrafts 校验字幕关系，并展开每句的说话人和演绎参数。
func (d Draft) SubtitleDrafts() ([]Draft, error) {
	if d.Subtitles == nil {
		return []Draft{d}, nil
	}
	document := d.Subtitles
	bad := msg.Err(msg.ErrSubtitleInvalid, nil)
	if len(document.Cues) > 2000 || len(document.Speakers) == 0 || len(document.Speakers) > 100 {
		return nil, bad
	}
	speakers := map[string]SubtitleSpeaker{}
	for _, speaker := range document.Speakers {
		if speaker.ID == "" || len(speaker.ID) > 64 || TextLen(speaker.SourceName) > 120 {
			return nil, bad
		}
		if _, exists := speakers[speaker.ID]; exists {
			return nil, bad
		}
		speakers[speaker.ID] = speaker
	}
	drafts := make([]Draft, 0, len(document.Cues))
	texts := make([]string, 0, len(document.Cues))
	ids := map[string]bool{}
	for _, cue := range document.Cues {
		if err := ValidateCueMarks(cue); err != nil {
			return nil, err
		}
		if cue.ID != "" {
			if len(cue.ID) > 64 || ids[cue.ID] {
				return nil, bad
			}
			ids[cue.ID] = true
		}
		speaker, exists := speakers[cue.SpeakerID]
		if !exists || cue.Start < 0 || cue.End <= cue.Start || cue.End > 86400000 {
			return nil, bad
		}
		next := d
		next.Subtitles = nil
		next.Text = cue.Text
		next.CharacterID = speaker.CharacterID
		if speaker.Settings != nil {
			next.SynthesisSettings = *speaker.Settings
		}
		if cue.Performance != nil && (cue.Performance.ID == "" || len(cue.Performance.ID) > 64 || TextLen(cue.Performance.Name) > 120) {
			return nil, bad
		}
		next.SynthesisSettings = next.SynthesisSettings.PerformanceSettings(cue.Performance)
		next.Performance = cue.Performance
		texts = append(texts, cue.Text)
		drafts = append(drafts, next)
	}
	if strings.Join(texts, "\n") != d.Text || TextLen(d.Text) > 12000 {
		return nil, bad
	}
	return drafts, nil
}

// Normalize 补齐旧数据与外部输入缺少的部分：台词补上可重复的 ID，后续插入或删除台词不会错配生成结果；
// 音效作品补纯音乐标记与默认五秒时长，编曲作品缺少乐谱时补一份空乐谱。
// 保存作品、导入作品包与状态库升级都经过这里，界面只按补齐后的格式读取。
func (d *Draft) Normalize() {
	if d.Kind == "sfx" {
		d.Instrumental = true
		if d.ModelOptions == nil {
			d.ModelOptions = map[string]map[string]any{}
		}
		options := d.ModelOptions["stable_audio"]
		if options == nil {
			options = map[string]any{}
		}
		if _, ok := options["duration_seconds"]; !ok {
			options["duration_seconds"] = float64(5)
		}
		d.ModelOptions["stable_audio"] = options
	}
	if d.Kind == "score" && d.Score == nil {
		d.Score = &Score{Tempo: 100, TimeSignature: []int{4, 4}}
	}
	if d.Subtitles == nil || !slices.ContainsFunc(d.Subtitles.Cues, func(c SubtitleCue) bool { return c.ID == "" }) {
		return
	}
	document := *d.Subtitles
	document.Cues = append([]SubtitleCue{}, document.Cues...)
	for index := range document.Cues {
		if document.Cues[index].ID == "" {
			document.Cues[index].ID = fmt.Sprintf("legacy-%s-%d", d.ID, index)
		}
	}
	d.Subtitles = &document
}
