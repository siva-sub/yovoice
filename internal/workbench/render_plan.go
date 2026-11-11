package workbench

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
	"yovoice/internal/audio"
	"yovoice/internal/catalog"
	"yovoice/internal/diag"
	"yovoice/internal/msg"
	"yovoice/internal/platform"
	"yovoice/internal/schema"
)

// 成本预估的基准值来自 ADR-0003 的第三方基准与 2026-10 的 Colab 价目：
// GPU 小时 = 音频小时 ÷ RTF，计算单元 = GPU 小时 × 卡型单价。这些是估算值，
// 跑过一次后应由实测 RTF 替换，不能当成手册承诺。
const (
	// audiobookCharsPerSecond 是本卷实测的字数折算：1,122,289 字符 ≈ 1,317 分钟，约 14.2 字符/秒。
	audiobookCharsPerSecond = 14.2

	rtfBreezeTTS2      = 0.48
	rtfMing3           = 1.01
	rtfQwen3TTS        = 3.58
	rtfLongCatAudioDiT = 6.69
	rtfDrama3          = 7.6

	unitsPerGPUHourA100 = 11.77
	unitsPerGPUHourT4   = 1.68
	unitsPerGPUHourL4   = 4.82

	defaultRenderCard = "A100"
)

// renderRTFTable 把 ADR-0003 的模型名与目录里的实际模型 ID 对应起来：按前缀匹配，
// 量化后缀（q8/bf16）不影响换算，同一族的 0.6B 与 1.7B 暂按基准表里较慢的一档估算。
var renderRTFTable = []struct {
	prefix string
	rtf    float64
}{
	{"breeze-tts-2", rtfBreezeTTS2},
	{"ming-3.0-0.5b", rtfMing3},
	{"ming3-tts", rtfMing3},
	{"qwen3-tts-1.7b", rtfQwen3TTS},
	{"qwen3-tts", rtfQwen3TTS},
	{"longcat-audiodit-1b", rtfLongCatAudioDiT},
	{"drama-3", rtfDrama3},
	{"fish-drama3", rtfDrama3},
}

// CostEstimate 是一批章节的渲染成本估算。数字都是估算值，用于在按下开始前回答
// “能不能渲完”，不是账单。
type CostEstimate struct {
	ModelID      string  `json:"modelId"`
	Card         string  `json:"card"`
	RTF          float64 `json:"rtf"`
	UnitPrice    float64 `json:"unitPrice"`
	Chars        int     `json:"chars"`
	AudioSeconds float64 `json:"audioSeconds"`
	AudioHours   float64 `json:"audioHours"`
	GPUHours     float64 `json:"gpuHours"`
	Units        float64 `json:"units"`
}

// EstimateCost 按章节字数估算这批作品在指定模型与卡型上的音频时长、GPU 小时与计算单元。
// modelID 为空时按每个作品自己的模型分别估算；card 为空时按 A100 估算。
func (w *Workbench) EstimateCost(draftIDs []string, modelID, card string) (CostEstimate, error) {
	s := w.Store.Read()
	drafts, err := selectDrafts(s, draftIDs)
	if err != nil {
		return CostEstimate{}, err
	}
	return estimateCost(drafts, modelID, card)
}

// RenderOptions 是批量渲染的成本闸门设置。Balance 为 nil 表示尚未填写计算单元结余，
// 不启用预算闸门；0 是有效结余，任何正成本都会被拒绝，除非 Force 为真。
type RenderOptions struct {
	ModelID string
	Card    string
	Balance *float64
	Force   bool
}

// RenderBatch 是批量渲染入口：章节闸门 → 成本闸门 → 启动引擎，三者顺序不可调。
// 超额且未显式覆盖时在启动任何引擎之前返回 ErrRenderBudgetExceeded；覆盖时写日志留记录。
// 通过闸门后按引擎键分组渲染，清单里已完成的 cue 直接跳过。
func (w *Workbench) RenderBatch(draftIDs []string, opts RenderOptions) error {
	w.edit.Lock()
	defer w.edit.Unlock()

	s := w.Store.Read()
	drafts, err := selectDrafts(s, draftIDs)
	if err != nil {
		return err
	}
	if err = w.checkRenderGates(drafts, opts); err != nil {
		return err
	}
	ordered, engineVersion, err := w.pendingRenderJobs(s, drafts)
	if err != nil {
		return err
	}
	if len(ordered) == 0 {
		return nil
	}
	if err = renderReady(s, ordered); err != nil {
		return err
	}
	batchID := schema.NewID()
	return w.begin("generate", msg.ActivityGenerate, nil, nil, func(ctx context.Context) error {
		return w.renderBatchJobs(ctx, s, ordered, batchID, engineVersion)
	})
}

// checkRenderGates 按固定顺序检查章节闸门与成本闸门：未确认读过的章节连成本都不该算，
// 超额且未显式覆盖时也不能启动引擎。任何一项失败都发生在后台操作创建之前。
func (w *Workbench) checkRenderGates(drafts []schema.Draft, opts RenderOptions) error {
	for _, d := range drafts {
		if d.Kind != "" && d.Kind != "text" && d.Kind != "story" && d.Kind != "subtitle" {
			return msg.Err(msg.ErrRequestInvalid, nil)
		}
		if d.Kind == "story" && d.ChapterStatus() != schema.GateVerified {
			return msg.Err(msg.ErrChapterNotVerified, nil)
		}
	}
	estimate := CostEstimate{}
	if opts.Balance != nil || opts.ModelID != "" {
		var err error
		estimate, err = estimateCost(drafts, opts.ModelID, opts.Card)
		if err != nil {
			return err
		}
	}
	if opts.Balance == nil || estimate.Units <= *opts.Balance {
		return nil
	}
	if !opts.Force {
		return msg.Err(msg.ErrRenderBudgetExceeded, msg.Params{
			"needed":  roundUnits(estimate.Units),
			"balance": *opts.Balance,
			"card":    estimate.Card,
			"model":   estimate.ModelID,
		})
	}
	diag.Log(w.Store.Root, "render.budget_override", "needed", roundUnits(estimate.Units), "balance", *opts.Balance, "card", estimate.Card, "model", estimate.ModelID, "force", true)
	return nil
}

// pendingRenderJobs 完成启动前的逐句检查，再按清单去掉已完成的 cue，最后按引擎键分组。
// 全部命中时返回空计划，调用方不会创建后台操作，也就不会请求引擎。
func (w *Workbench) pendingRenderJobs(s schema.State, drafts []schema.Draft) ([]renderJob, string, error) {
	jobs, err := w.planRenderJobs(s, drafts)
	if err != nil {
		return nil, "", err
	}
	manifest, err := ReadRenderManifest(w.Store.Root)
	if err != nil {
		return nil, "", err
	}
	engineVersion := schema.Deref(s.RuntimeVersion)
	pending := PendingRenderCues(renderCues(jobs), manifest, engineVersion)
	if len(pending) == 0 {
		return nil, engineVersion, nil
	}
	ordered, err := orderRenderJobs(jobs, PlanLanes(pending))
	if err != nil {
		return nil, "", err
	}
	return ordered, engineVersion, nil
}

// renderReady 检查内核与模型都已经就绪；缺任何一项都在启动引擎之前返回。
func renderReady(s schema.State, jobs []renderJob) error {
	if !RuntimeReady(s) {
		return msg.Err(msg.ErrRuntimeRequired, nil)
	}
	for _, job := range jobs {
		if err := modelEngineReady(s, job.part.model.ID); err != nil {
			return err
		}
		if installing(s, job.part.model.ID) {
			return msg.Err(msg.ErrBusy, nil)
		}
	}
	return nil
}

// renderBatchJobs 按分组顺序逐句渲染；单条失败记入清单后继续，最后把第一条错误交给操作状态。
func (w *Workbench) renderBatchJobs(ctx context.Context, s schema.State, jobs []renderJob, batchID, engineVersion string) error {
	if err := w.ensureKokoro(ctx, s, synthesisParts(jobs)); err != nil {
		return err
	}
	var firstErr error
	for index, job := range jobs {
		if err := ctx.Err(); err != nil {
			return err
		}
		_ = w.Store.Update(func(state *schema.State) {
			if state.Activity != nil {
				state.Activity.CueID = job.cue.CueID
			}
		}, false)
		take, err := w.renderCueTake(ctx, s, job, batchID, index, int64(len(jobs)))
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if firstErr == nil {
				firstErr = err
			}
			entry := manifestEntry(job.cue, "failed")
			entry.ErrorCode = renderErrorCode(err)
			if writeErr := writeRenderEntry(w.Store.Root, engineVersion, entry); writeErr != nil && firstErr == nil {
				firstErr = writeErr
			}
			continue
		}
		entry := manifestEntry(job.cue, "done")
		entry.TakeID, entry.FileName = take.ID, take.FileName
		if err := writeRenderEntry(w.Store.Root, engineVersion, entry); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// renderJob 把缓存计划与启动引擎所需的逐句参数放在一起，避免渲染时再读一次状态。
type renderJob struct {
	cue  RenderCue
	part synthesisPart
}

// planRenderJobs 逐句展开作品并完成引擎启动前的全部检查：模型已安装、音色存在、请求参数合法。
// 这里失败时还没有启动任何引擎。
func (w *Workbench) planRenderJobs(s schema.State, drafts []schema.Draft) ([]renderJob, error) {
	jobs := make([]renderJob, 0)
	anchors := map[string]string{}
	for _, d := range drafts {
		if d.Subtitles == nil {
			parts, err := w.prepareSynthesis(d, s)
			if err != nil {
				return nil, err
			}
			for _, part := range parts {
				cue := w.renderCue(d, schema.SubtitleCue{}, "", part, s, anchors)
				jobs = append(jobs, renderJob{cue: cue, part: part})
			}
			continue
		}
		cueDrafts, err := d.SubtitleDrafts()
		if err != nil {
			return nil, err
		}
		for index, cueDraft := range cueDrafts {
			if strings.TrimSpace(cueDraft.Text) == "" {
				continue
			}
			parts, err := w.prepareSynthesis(cueDraft, s)
			if err != nil {
				return nil, err
			}
			part := parts[0]
			cue := d.Subtitles.Cues[index]
			name := cueSpeakerName(*d.Subtitles, cue.SpeakerID)
			part.segment = &schema.GenerationSegment{CueID: cue.ID, SpeakerID: cue.SpeakerID, SpeakerName: name, Index: index, Placement: "ripple"}
			jobs = append(jobs, renderJob{cue: w.renderCue(cueDraft, cue, name, part, s, anchors), part: part})
		}
	}
	if len(jobs) == 0 {
		return nil, msg.Err(msg.ErrTextRequired, nil)
	}
	return jobs, nil
}

// renderCue 从逐句参数生成缓存计划条目：说话人是 take 身份的一部分，改派后必须重新渲染。
func (w *Workbench) renderCue(draft schema.Draft, cue schema.SubtitleCue, speakerName string, part synthesisPart, s schema.State, anchors map[string]string) RenderCue {
	return RenderCue{
		DraftID:        draft.ID,
		CueID:          cue.ID,
		SpeakerID:      cue.SpeakerID,
		SpeakerName:    speakerName,
		ModelID:        part.model.ID,
		AnchorRev:      w.anchorRevision(draft, anchors),
		Text:           part.draft.Text,
		Instruction:    strings.TrimSpace(part.draft.VoiceDescription),
		CFG:            part.draft.GuidanceScale,
		Seed:           part.draft.Seed,
		SessionOptions: laneSessionOptions(part.draft, s),
	}
}

// anchorRevision 用锚点音频的内容摘要作为锚点版本：换文件必然换缓存键（ADR-0014 的内容寻址）。
// 读不到文件时退回音色 ID，至少换锚点不会命中旧键。
func (w *Workbench) anchorRevision(draft schema.Draft, cache map[string]string) string {
	id := schema.Deref(draft.VoiceID)
	if id == "" {
		return ""
	}
	if revision, ok := cache[id]; ok {
		return revision
	}
	revision := id
	if path, err := w.MediaFile("voices", id); err == nil {
		if b, err := os.ReadFile(path); err == nil {
			sum := sha256.Sum256(b)
			revision = hex.EncodeToString(sum[:])
		}
	}
	cache[id] = revision
	return revision
}

// laneSessionOptions 是引擎启动键里的会话参数：模型目录的默认值加上用户为该模型族设置的覆盖值，
// 再加串流/离线模式。它们与模型一起决定是否要重启引擎。
func laneSessionOptions(d schema.Draft, s schema.State) map[string]string {
	definition, err := catalog.Lookup(d.ModelID)
	if err != nil {
		return nil
	}
	options := make(map[string]string, len(definition.SessionOptions)+1)
	for key, value := range definition.SessionOptions {
		options[key] = value
	}
	if s.Preferences.Engine != nil {
		for key, value := range s.Preferences.Engine.Session {
			if strings.HasPrefix(key, definition.Family+".") {
				options[key] = fmt.Sprint(value)
			}
		}
	}
	mode := "offline"
	if d.Runtime != nil && d.Runtime.Streaming {
		mode = "streaming"
	}
	options["mode"] = mode
	return options
}

// RenderCacheKey 是 take 身份的纯函数哈希：模型、锚点、文本、演绎指令、CFG、种子与说话人。
// 必须包含 speakerId：改派后同一句文本不能再命中旧说话人的音频（流不变量 2）。
func RenderCacheKey(cue RenderCue) string {
	payload := struct {
		ModelID     string  `json:"modelId"`
		AnchorRev   string  `json:"anchorRev"`
		Text        string  `json:"text"`
		Instruction string  `json:"instruction"`
		CFG         float64 `json:"cfg"`
		Seed        *int    `json:"seed"`
		SpeakerID   string  `json:"speakerId"`
	}{cue.ModelID, cue.AnchorRev, cue.Text, cue.Instruction, cue.CFG, cue.Seed, cue.SpeakerID}
	b, _ := json.Marshal(payload)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// renderCues 取出计划里的 cue 视图，供清单过滤与分组使用。
func renderCues(jobs []renderJob) []RenderCue {
	cues := make([]RenderCue, len(jobs))
	for i, job := range jobs {
		cues[i] = job.cue
	}
	return cues
}

// synthesisParts 取出计划里的逐句参数，供 Kokoro 发音库等启动前检查使用。
func synthesisParts(jobs []renderJob) []synthesisPart {
	parts := make([]synthesisPart, len(jobs))
	for i, job := range jobs {
		parts[i] = job.part
	}
	return parts
}

// orderRenderJobs 把逐句计划按车道顺序重排；找不到对应 job 说明计划与任务不一致，直接拒绝。
func orderRenderJobs(jobs []renderJob, lanes []RenderLane) ([]renderJob, error) {
	byCue := make(map[string]renderJob, len(jobs))
	for _, job := range jobs {
		byCue[renderCueIdentity(job.cue)] = job
	}
	ordered := make([]renderJob, 0, len(jobs))
	for _, cue := range FlattenLanes(lanes) {
		job, ok := byCue[renderCueIdentity(cue)]
		if !ok {
			return nil, msg.Err(msg.ErrRequestInvalid, nil)
		}
		ordered = append(ordered, job)
	}
	return ordered, nil
}

func renderCueIdentity(cue RenderCue) string { return cue.DraftID + "\x00" + cue.CueID }

// cueSpeakerName 返回说话人的显示名；只用于版本记录。
func cueSpeakerName(document schema.SubtitleDocument, id string) string {
	for _, speaker := range document.Speakers {
		if speaker.ID == id {
			return speaker.SourceName
		}
	}
	return ""
}

// renderCueTake 渲染并保存一句，返回生成的版本；失败时删除半成品音频。
func (w *Workbench) renderCueTake(ctx context.Context, s schema.State, job renderJob, batchID string, index int, total int64) (schema.Generation, error) {
	id := schema.NewID()
	file := outputFileName(id, time.Now(), outputSummary(job.part.draft.Text, job.part.draft.Title))
	path, err := w.Store.MediaPath("outputs", file)
	if err != nil {
		return schema.Generation{}, err
	}
	keep := false
	defer func() {
		if !keep {
			_ = os.Remove(path)
		}
	}()
	diag.Log(w.Store.Root, "segment.started", "project_id", job.part.draft.ID, "batch_id", batchID, "cue_id", job.cue.CueID, "generation_id", id, "model_id", job.part.model.ID, "index", index)
	_, err = w.synthesizePart(ctx, s, job.part, path, func(_ msg.Code, _ msg.Params) {
		w.progress(msg.ActivitySubtitle, msg.Params{"n": index + 1, "total": int(total)}, int64(index), total)
	})
	if err != nil {
		return schema.Generation{}, err
	}
	duration, err := audio.Duration(path)
	if err == nil {
		err = ctx.Err()
	}
	if err != nil {
		return schema.Generation{}, err
	}
	// 与逐句生成一致，快照不携带时间轴，避免历史记录随剪辑反复膨胀。
	snapshot := job.part.draft
	snapshot.Timeline = nil
	snapshot.Kind = "text"
	g := schema.Generation{ID: id, Title: schema.TruncateRunes(job.part.draft.Text, 60), FileName: file, CreatedAt: time.Now().UTC(), Duration: duration, Settings: snapshot, Segment: job.part.segment, Check: w.checkAudio(path, "story", id)}
	if err = w.Store.Update(func(state *schema.State) {
		state.History = append([]schema.Generation{g}, state.History...)
	}, true); err != nil {
		return schema.Generation{}, err
	}
	keep = true
	diag.Log(w.Store.Root, "segment.saved", "project_id", job.part.draft.ID, "batch_id", batchID, "cue_id", job.cue.CueID, "generation_id", id, "duration", duration)
	return g, nil
}

// RenderManifest 是批量渲染的可续跑清单，停在数据目录的 render-manifest.json。
// 每个完成的 take 一行；会话断掉后按缓存键跳过已完成的 cue，失败条目仍然待做。
type RenderManifest struct {
	EngineVersion string                `json:"engineVersion,omitempty"`
	UpdatedAt     string                `json:"updatedAt,omitempty"`
	Entries       []RenderManifestEntry `json:"entries"`
}

// RenderManifestEntry 是一条 take 的完成记录：Key 是缓存键，TakeID/FileName 指向产物。
type RenderManifestEntry struct {
	Key       string `json:"key"`
	DraftID   string `json:"draftId"`
	CueID     string `json:"cueId"`
	SpeakerID string `json:"speakerId"`
	ModelID   string `json:"modelId"`
	TakeID    string `json:"takeId,omitempty"`
	FileName  string `json:"fileName,omitempty"`
	Status    string `json:"status"` // done | failed
	ErrorCode string `json:"errorCode,omitempty"`
	UpdatedAt string `json:"updatedAt,omitempty"`
}

// renderManifestFile 是清单文件名；与 outputs、voices 同级，属于整个数据目录。
const renderManifestFile = "render-manifest.json"

func renderManifestPath(root string) string { return filepath.Join(root, renderManifestFile) }

// ReadRenderManifest 读取清单；还没有清单时返回空清单，第一次渲染从零开始。
func ReadRenderManifest(root string) (RenderManifest, error) {
	b, err := os.ReadFile(renderManifestPath(root))
	if os.IsNotExist(err) {
		return RenderManifest{Entries: []RenderManifestEntry{}}, nil
	}
	if err != nil {
		return RenderManifest{}, err
	}
	var manifest RenderManifest
	if err = json.Unmarshal(b, &manifest); err != nil {
		return RenderManifest{}, err
	}
	if manifest.Entries == nil {
		manifest.Entries = []RenderManifestEntry{}
	}
	return manifest, nil
}

// WriteRenderManifest 原子写入清单：中断时不会留下半份 JSON，续跑读到的是上一次完整状态。
func WriteRenderManifest(root string, manifest RenderManifest) error {
	if manifest.Entries == nil {
		manifest.Entries = []RenderManifestEntry{}
	}
	b, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	return platform.WriteFileAtomic(renderManifestPath(root), append(b, '\n'), 0600)
}

// Put 按缓存键写入一条记录：完成的 take 覆盖失败记录，重跑不会堆积重复行。
func (m *RenderManifest) Put(entry RenderManifestEntry) {
	for i := range m.Entries {
		if m.Entries[i].Key == entry.Key {
			m.Entries[i] = entry
			return
		}
	}
	m.Entries = append(m.Entries, entry)
}

// Done 判断该缓存键是否已完成。清单记录的引擎版本与当前不一致时全部视为未完成：
// 换了 audio.cpp 或模型文件，旧产物不能再冒充新版本（ADR-0010）。
func (m RenderManifest) Done(key, engineVersion string) bool {
	if m.EngineVersion != "" && engineVersion != "" && m.EngineVersion != engineVersion {
		return false
	}
	return slices.ContainsFunc(m.Entries, func(entry RenderManifestEntry) bool {
		return entry.Key == key && entry.Status == "done"
	})
}

// PendingRenderCues 去掉清单里已完成的 cue，保留原有顺序；失败条目仍然待做。
func PendingRenderCues(cues []RenderCue, manifest RenderManifest, engineVersion string) []RenderCue {
	pending := make([]RenderCue, 0, len(cues))
	for _, cue := range cues {
		if !manifest.Done(RenderCacheKey(cue), engineVersion) {
			pending = append(pending, cue)
		}
	}
	return pending
}

// manifestEntry 把一条 cue 转成清单记录；状态由调用方填写，时间戳在写入时统一设置。
func manifestEntry(cue RenderCue, status string) RenderManifestEntry {
	return RenderManifestEntry{
		Key:       RenderCacheKey(cue),
		DraftID:   cue.DraftID,
		CueID:     cue.CueID,
		SpeakerID: cue.SpeakerID,
		ModelID:   cue.ModelID,
		Status:    status,
	}
}

// writeRenderEntry 读改写清单。批量渲染同一时间只有一个操作，不存在并发写。
func writeRenderEntry(root, engineVersion string, entry RenderManifestEntry) error {
	manifest, err := ReadRenderManifest(root)
	if err != nil {
		return err
	}
	// 引擎版本变了，旧条目不能再用来续跑；重新开始记这一轮。
	if manifest.EngineVersion != "" && engineVersion != "" && manifest.EngineVersion != engineVersion {
		manifest.Entries = nil
	}
	if engineVersion != "" {
		manifest.EngineVersion = engineVersion
	}
	entry.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	manifest.UpdatedAt = entry.UpdatedAt
	manifest.Put(entry)
	return WriteRenderManifest(root, manifest)
}

// renderErrorCode 把渲染错误转成清单里的稳定错误码，便于续跑面板统计。
func renderErrorCode(err error) string {
	var ce *msg.CallError
	if errors.As(err, &ce) && ce != nil {
		return string(ce.Code)
	}
	return string(msg.ErrUnknown)
}

// selectDrafts 按给定顺序取出作品；不存在的 ID 直接拒绝，避免静默漏渲。
func selectDrafts(s schema.State, draftIDs []string) ([]schema.Draft, error) {
	if len(draftIDs) == 0 {
		return nil, msg.Err(msg.ErrDraftIDInvalid, nil)
	}
	drafts := make([]schema.Draft, 0, len(draftIDs))
	for _, id := range draftIDs {
		i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == id })
		if i < 0 {
			return nil, msg.Err(msg.ErrDraftIDInvalid, nil)
		}
		drafts = append(drafts, s.Drafts[i])
	}
	return drafts, nil
}

// estimateCost 是 EstimateCost 与预算闸门共用的换算；modelID 为空时逐作品取自己的模型。
func estimateCost(drafts []schema.Draft, modelID, card string) (CostEstimate, error) {
	card = strings.ToUpper(strings.TrimSpace(card))
	if card == "" {
		card = defaultRenderCard
	}
	price, ok := renderUnitPrice(card)
	if !ok {
		return CostEstimate{}, msg.Err(msg.ErrParamsOutOfRange, msg.Params{"card": card})
	}
	estimate := CostEstimate{Card: card, UnitPrice: price}
	models := make([]string, 0, 1)
	rtf := 0.0
	for _, d := range drafts {
		model := modelID
		if model == "" {
			model = d.ModelID
		}
		value, ok := renderRTFFor(model)
		if !ok {
			return CostEstimate{}, msg.Err(msg.ErrModelUnsupported, msg.Params{"model": model})
		}
		chars := draftChars(d)
		seconds := float64(chars) / audiobookCharsPerSecond
		estimate.Chars += chars
		estimate.AudioSeconds += seconds
		estimate.GPUHours += renderGPUHours(seconds, value)
		if !slices.Contains(models, model) {
			models = append(models, model)
		}
		if modelID != "" {
			rtf = value
		}
	}
	estimate.AudioHours = estimate.AudioSeconds / 3600
	estimate.Units = estimate.GPUHours * price
	switch {
	case modelID != "":
		estimate.ModelID, estimate.RTF = modelID, rtf
	case len(models) == 1:
		estimate.ModelID = models[0]
		estimate.RTF, _ = renderRTFFor(models[0])
	default:
		// 多个模型混排时没有单一 RTF；GPU 小时已按各自的 RTF 分别累加。
		estimate.ModelID = "mixed"
	}
	return estimate, nil
}

// draftChars 按 cue 正文计字数；没有字幕的作品退回整篇正文。字数包含空格，与脚本时代的口径一致。
func draftChars(d schema.Draft) int {
	if d.Subtitles != nil && len(d.Subtitles.Cues) > 0 {
		chars := 0
		for _, cue := range d.Subtitles.Cues {
			chars += utf8.RuneCountInString(cue.Text)
		}
		return chars
	}
	return utf8.RuneCountInString(d.Text)
}

// renderGPUHours 是成本换算的唯一公式：音频秒数 ÷ RTF ÷ 3600。
// 调用方已从 RTF 表取到正数，不需要再处理除零。
func renderGPUHours(audioSeconds, rtf float64) float64 {
	return audioSeconds / rtf / 3600
}

// renderRTFFor 按模型 ID 前缀查基准 RTF；未知模型不能假装知道成本。
func renderRTFFor(modelID string) (float64, bool) {
	id := strings.ToLower(strings.TrimSpace(modelID))
	for _, entry := range renderRTFTable {
		if strings.HasPrefix(id, entry.prefix) {
			return entry.rtf, true
		}
	}
	return 0, false
}

// renderUnitPrice 返回卡型每小时的计算单元单价（估算值）。
func renderUnitPrice(card string) (float64, bool) {
	switch strings.ToUpper(strings.TrimSpace(card)) {
	case "A100":
		return unitsPerGPUHourA100, true
	case "T4":
		return unitsPerGPUHourT4, true
	case "L4":
		return unitsPerGPUHourL4, true
	}
	return 0, false
}

// roundUnits 把计算单元保留一位小数，用于错误提示与覆盖日志。
func roundUnits(value float64) float64 { return math.Round(value*10) / 10 }
