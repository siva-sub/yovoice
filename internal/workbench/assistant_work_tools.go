package workbench

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"yovoice/internal/diag"
	"yovoice/internal/schema"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func (w *Workbench) listWorksTool(ctx context.Context, req *mcp.CallToolRequest, in struct {
	Kind string `json:"kind,omitempty" jsonschema:"只列某类作品：text、story、music、sfx 或 score；留空列全部"`
}) (*mcp.CallToolResult, any, error) {
	drafts := w.Store.Read().Drafts
	works := make([]map[string]any, 0, len(drafts))
	for _, d := range drafts {
		kind := projectKind(d)
		if in.Kind != "" && in.Kind != kind {
			continue
		}
		work := map[string]any{"id": d.ID, "kind": kind, "title": d.Title, "updatedAt": d.UpdatedAt}
		// 音乐作品带上风格，写新歌时可以对照已有作品，避免每首都是同一种流派和速度。
		if kind == "music" {
			work["style"] = schema.TruncateRunes(d.Text, 200)
		}
		works = append(works, work)
	}
	w.chatCard("list_works", fmt.Sprintf("查看了作品列表（%d 个）", len(works)), fmt.Sprintf("Viewed the works list (%d)", len(works)), "done")
	return nil, map[string]any{"works": works}, nil
}

// workContent 把作品转成给模型看的内容；时间线只保留选轨所需的摘要，不返回剪辑和素材明细。
func workContent(d schema.Draft) map[string]any {
	out := map[string]any{"id": d.ID, "kind": projectKind(d), "title": d.Title, "text": d.Text, "updatedAt": d.UpdatedAt}
	settings := map[string]any{"modelId": d.ModelID, "mode": d.Mode, "language": d.Language, "speed": d.Speed}
	for name, value := range map[string]any{"voiceId": d.VoiceID, "emotionVoiceId": d.EmotionVoiceID, "speaker": d.Speaker, "voiceMode": d.VoiceMode, "voiceDescription": d.VoiceDescription, "referenceText": d.ReferenceText, "lyrics": d.Lyrics, "instrumental": d.Instrumental, "synthesisLanguage": d.SynthesisLanguage, "abc": d.Abc, "seed": d.Seed, "temperature": d.Temperature} {
		if value != nil && value != "" && value != false {
			settings[name] = value
		}
	}
	out["settings"] = settings
	if d.Subtitles != nil {
		speakers := make([]map[string]any, 0, len(d.Subtitles.Speakers))
		for _, s := range d.Subtitles.Speakers {
			speaker := map[string]any{"id": s.ID, "name": s.SourceName}
			if s.CharacterID != "" {
				speaker["characterId"] = s.CharacterID
			}
			if s.Settings != nil {
				if s.Settings.VoiceID != nil && *s.Settings.VoiceID != "" {
					speaker["voiceId"] = *s.Settings.VoiceID
				}
				if s.Settings.ModelID != "" && s.Settings.ModelID != d.ModelID {
					speaker["modelId"] = s.Settings.ModelID
				}
			}
			speakers = append(speakers, speaker)
		}
		cues := make([]map[string]any, 0, len(d.Subtitles.Cues))
		for _, c := range d.Subtitles.Cues {
			cues = append(cues, map[string]any{"id": c.ID, "speakerId": c.SpeakerID, "text": c.Text})
		}
		out["speakers"] = speakers
		out["cues"] = cues
	}
	if d.Timeline != nil {
		tracks := make([]map[string]any, 0, len(d.Timeline.Tracks))
		for i, track := range d.Timeline.Tracks {
			end := 0.0
			for _, clip := range track.Clips {
				end = max(end, clip.Start+clip.Duration)
			}
			tracks = append(tracks, map[string]any{"index": i + 1, "id": track.ID, "name": track.Name, "locked": track.Locked, "end": end})
		}
		out["tracks"] = tracks
	}
	if d.Score != nil {
		out["score"] = d.Score
	}
	return out
}

// workIDInput 是以作品 ID 为参数的工具输入。
type workIDInput struct {
	ID string `json:"id" jsonschema:"作品 ID"`
}

func (w *Workbench) getWorkTool(ctx context.Context, req *mcp.CallToolRequest, in workIDInput) (*mcp.CallToolResult, any, error) {
	d, err := w.assistantWork(in.ID)
	if err != nil {
		return nil, nil, err
	}
	w.chatCard("get_work", fmt.Sprintf("读取了《%s》", d.Title), fmt.Sprintf("Read %q", d.Title), "done")
	return nil, workContent(d), nil
}

// assistantWork 按 ID 找到作品；找不到时给出与界面一致的错误。
func (w *Workbench) assistantWork(id string) (schema.Draft, error) {
	for _, d := range w.Store.Read().Drafts {
		if d.ID == id {
			return d, nil
		}
	}
	return schema.Draft{}, fmt.Errorf("没有找到作品 %s，请先用 list_works 查看现有的作品 ID", id)
}

// createWorkInput 的类型与 createDraft 的界面默认一致：语音用默认语音模型，音乐用 ACE-Step，音效用 Stable Audio SFX，编曲用音色库。
type createWorkInput struct {
	Kind  string `json:"kind" jsonschema:"作品类型：text、story、music、sfx 或 score"`
	Title string `json:"title,omitempty" jsonschema:"作品标题，留空由用户稍后命名"`
	Open  *bool  `json:"open,omitempty" jsonschema:"是否让界面打开新作品，默认 true；只是准备素材时传 false"`
}

func (w *Workbench) createWorkTool(ctx context.Context, req *mcp.CallToolRequest, in createWorkInput) (*mcp.CallToolResult, any, error) {
	if !slices.Contains([]string{"text", "story", "music", "sfx", "score"}, in.Kind) {
		return nil, nil, fmt.Errorf("kind 必须是 text、story、music、sfx 或 score")
	}
	d := schema.DefaultDraft()
	d.Text, d.Kind, d.Title = "", in.Kind, strings.TrimSpace(in.Title)
	if d.Title == "" {
		d.Title = "未命名作品"
	}
	s := w.Store.Read()
	d.ModelID = preferredModel(s, in.Kind, d.ModelID)
	switch in.Kind {
	case "score":
		d.Score = &schema.Score{Tempo: 100, TimeSignature: []int{4, 4}}
	}
	w.edit.Lock()
	err := w.saveDraft(d)
	w.edit.Unlock()
	if err != nil {
		return nil, nil, err
	}
	w.chatCard("create_work", fmt.Sprintf("新建了《%s》", d.Title), fmt.Sprintf("Created %q", d.Title), "done")
	w.chatUpdate(d.ID)
	if in.Open == nil || *in.Open {
		w.chatNav(d.ID, "")
	}
	diag.Log(w.Store.Root, "assistant.work_created", "project_id", d.ID, "kind", in.Kind)
	return nil, map[string]any{"id": d.ID, "kind": in.Kind, "title": d.Title}, nil
}

// 助手的有声书流程工具：看章级待办、推进闸门、改派说话人、写入验收与检查混音。
// 它们与界面按钮走同一批 workbench 方法，助手不自己改状态，也不绕过闸门。

// registerChapterTools 把有声书流程工具注册到内置助手的 MCP server。
func (w *Workbench) registerChapterTools(server *mcp.Server) {
	mcp.AddTool(server, &mcp.Tool{Name: "list_pending_chapters", Description: "列出书级待办：该读、该校验归因、该渲染、该验收的章节，按章号升序。用户问“接下来做什么”“还有哪些章没过”，或你准备调用 verify_chapter 前，先调用它。只读：bookId 留空返回所有书的待办，传入书（卷）ID 只看那一本；每项给出 kind、draftId、chapter 与可直接展示的 reason。"}, w.listPendingChaptersTool)
	mcp.AddTool(server, &mcp.Tool{Name: "verify_chapter", Description: "把一章的闸门推进到已确认：先记为已读并记录当前正文指纹，再写入校验说明与确认时间。用户通读正文、核对说话人后让你确认时调用；调用它等于替用户声明“这一章已经读过”，不要替用户猜。副作用：闸门状态写入状态库并允许这一章渲染；确认后助手无法撤销，要重新核对只能重开章节（没有对应工具）。"}, w.verifyChapterTool)
	mcp.AddTool(server, &mcp.Tool{Name: "reassign_cue", Description: "把台词改派给另一个说话人或改台词类型。用户指出“这句不是旁白说的”“改成 Jet”时调用单句（cueId）；整段判错时用 cue_ids 一次改完，不要逐句调用——两人对话里说话人常常整体反向，错误是成片的。先 get_work 取到 cueId 与说话人 ID。kind 可同时改类型：narration（叙述）、dialogue（对白）或 thought（心声），留空表示只改说话人。副作用：会清空这些句子的机器归因置信度（人做过决定后置信度不再有意义），不重新生成音频，已经渲染过的句子要重新渲染才生效。speakerId 非空时必须在该章的说话人列表里，否则整批拒绝、不会部分写入。"}, w.reassignCueTool)
	mcp.AddTool(server, &mcp.Tool{Name: "record_qa", Description: "写入一章的音频验收结果：WER、整章响度、真峰值、静音段数与判定。用户通听后给出通过或不通过的结论时调用；verdict 取 pass、fail 或 unchecked。副作用：覆盖该章上一份验收记录；判 fail 必须用 note 写清问题，否则会被拒绝。不改变闸门与渲染状态。"}, w.recordQATool)
	mcp.AddTool(server, &mcp.Tool{Name: "check_mix", Description: "检查故事配音时间线的混音配置：四层音轨（ambience、score、bed、hit）的电平区间、侧链深度与 hit 相对 cue 的提前量，返回问题列表与逐条说明。用户问“混音有没有问题”“电平合不合适”，或准备导出前调用。只读，不修改时间线；没有时间线时返回空列表。"}, w.checkMixTool)
}

type pendingChaptersInput struct {
	BookID string `json:"bookId,omitempty" jsonschema:"书（卷）ID，留空返回所有书的待办"`
}

func (w *Workbench) listPendingChaptersTool(ctx context.Context, req *mcp.CallToolRequest, in pendingChaptersInput) (*mcp.CallToolResult, any, error) {
	items := w.PendingQueue(in.BookID)
	if items == nil {
		items = []QueueItem{}
	}
	w.chatCard("list_pending_chapters", fmt.Sprintf("查看了待办章节（%d 件）", len(items)), fmt.Sprintf("Listed pending chapters (%d)", len(items)), "done")
	return nil, map[string]any{"items": items, "total": len(items)}, nil
}

type verifyChapterInput struct {
	ID   string `json:"id" jsonschema:"章节作品 ID"`
	Note string `json:"note,omitempty" jsonschema:"确认说明，例如通读时发现的问题；留空保留已有说明"`
}

func (w *Workbench) verifyChapterTool(ctx context.Context, req *mcp.CallToolRequest, in verifyChapterInput) (*mcp.CallToolResult, any, error) {
	d, err := w.assistantWork(in.ID)
	if err != nil {
		return nil, nil, err
	}
	if err = w.ConfirmRead(in.ID); err != nil {
		return nil, nil, err
	}
	if err = w.VerifyChapter(in.ID, strings.TrimSpace(in.Note)); err != nil {
		return nil, nil, err
	}
	w.chatCard("verify_chapter", fmt.Sprintf("确认了《%s》的归因", d.Title), fmt.Sprintf("Verified %q", d.Title), "done")
	return nil, map[string]any{"id": in.ID, "verified": true}, nil
}

type reassignCueInput struct {
	DraftID   string   `json:"draftId" jsonschema:"章节作品 ID"`
	CueID     string   `json:"cueId,omitempty" jsonschema:"单句的台词（cue）ID；只改一句时用，与 cue_ids 二选一"`
	CueIDs    []string `json:"cue_ids,omitempty" jsonschema:"成片改派的多句台词 ID；整段判错时一次传入，与 cueId 二选一"`
	SpeakerID string   `json:"speakerId,omitempty" jsonschema:"改派到的说话人 ID，必须在该章的说话人列表里；只改类型时可留空"`
	Kind      string   `json:"kind,omitempty" jsonschema:"同时改成的台词类型：narration（叙述）、dialogue（对白）或 thought（心声）；留空表示不改"`
}

func (w *Workbench) reassignCueTool(ctx context.Context, req *mcp.CallToolRequest, in reassignCueInput) (*mcp.CallToolResult, any, error) {
	speakerID, kind := strings.TrimSpace(in.SpeakerID), strings.TrimSpace(in.Kind)
	if speakerID == "" && kind == "" {
		return nil, nil, fmt.Errorf("speakerId 与 kind 不能都为空：至少要改说话人或台词类型")
	}
	cueID := strings.TrimSpace(in.CueID)
	if len(in.CueIDs) > 0 && cueID != "" {
		return nil, nil, fmt.Errorf("cueId 与 cue_ids 只能给一个：单句用 cueId，成片改派用 cue_ids")
	}
	if len(in.CueIDs) == 0 && cueID == "" {
		return nil, nil, fmt.Errorf("cueId 或 cue_ids 不能为空：先 get_work 取到要改的台词 ID")
	}
	d, err := w.assistantWork(in.DraftID)
	if err != nil {
		return nil, nil, err
	}
	if len(in.CueIDs) > 0 {
		changed, err := w.ReassignCues(in.DraftID, in.CueIDs, speakerID, kind)
		if err != nil {
			return nil, nil, err
		}
		w.chatCard("reassign_cue", fmt.Sprintf("改派了《%s》的 %d 句台词", d.Title, changed), fmt.Sprintf("Reassigned %d cues in %q", changed, d.Title), "done")
		return nil, map[string]any{"draftId": in.DraftID, "cueIds": in.CueIDs, "speakerId": speakerID, "kind": kind, "changed": changed, "confidenceCleared": true}, nil
	}
	if speakerID == "" {
		// 只改类型：单句也走成片改派，空 speakerId 保持原说话人。
		changed, err := w.ReassignCues(in.DraftID, []string{cueID}, "", kind)
		if err != nil {
			return nil, nil, err
		}
		w.chatCard("reassign_cue", fmt.Sprintf("改派了《%s》的一句台词", d.Title), fmt.Sprintf("Reassigned a cue in %q", d.Title), "done")
		return nil, map[string]any{"draftId": in.DraftID, "cueId": cueID, "kind": kind, "changed": changed, "confidenceCleared": true}, nil
	}
	if err = w.ReassignCue(in.DraftID, cueID, speakerID, kind); err != nil {
		return nil, nil, err
	}
	w.chatCard("reassign_cue", fmt.Sprintf("改派了《%s》的一句台词", d.Title), fmt.Sprintf("Reassigned a cue in %q", d.Title), "done")
	return nil, map[string]any{"draftId": in.DraftID, "cueId": cueID, "speakerId": speakerID, "kind": kind, "confidenceCleared": true}, nil
}

type recordQAInput struct {
	ID       string  `json:"id" jsonschema:"章节作品 ID"`
	Verdict  string  `json:"verdict" jsonschema:"验收判定：pass（通过）、fail（不通过）或 unchecked（未判定）"`
	Note     string  `json:"note,omitempty" jsonschema:"判定说明；判定为 fail 时必填，写清问题所在"`
	WER      float64 `json:"wer,omitempty" jsonschema:"回读词错率 0–1"`
	LUFS     float64 `json:"lufs,omitempty" jsonschema:"整章响度（LUFS）"`
	Peak     float64 `json:"peak,omitempty" jsonschema:"真峰值（dBTP）"`
	Silences int     `json:"silences,omitempty" jsonschema:"2 秒以上的静音段数"`
}

func (w *Workbench) recordQATool(ctx context.Context, req *mcp.CallToolRequest, in recordQAInput) (*mcp.CallToolResult, any, error) {
	d, err := w.assistantWork(in.ID)
	if err != nil {
		return nil, nil, err
	}
	record := schema.QARecord{WER: in.WER, LUFS: in.LUFS, Peak: in.Peak, Silences: in.Silences, Verdict: in.Verdict, Note: strings.TrimSpace(in.Note)}
	if err = w.RecordQA(in.ID, record); err != nil {
		return nil, nil, err
	}
	w.chatCard("record_qa", fmt.Sprintf("写入了《%s》的验收结果", d.Title), fmt.Sprintf("Recorded QA for %q", d.Title), "done")
	return nil, map[string]any{"id": in.ID, "verdict": record.Verdict}, nil
}

func (w *Workbench) checkMixTool(ctx context.Context, req *mcp.CallToolRequest, in workIDInput) (*mcp.CallToolResult, any, error) {
	d, err := w.assistantWork(in.ID)
	if err != nil {
		return nil, nil, err
	}
	issues := CheckMix(d, DefaultMixPolicy())
	if issues == nil {
		issues = []MixIssue{}
	}
	w.chatCard("check_mix", fmt.Sprintf("检查了《%s》的混音（%d 个问题）", d.Title, len(issues)), fmt.Sprintf("Checked the mix of %q (%d issues)", d.Title, len(issues)), "done")
	return nil, map[string]any{"issues": issues, "total": len(issues), "clean": len(issues) == 0}, nil
}
