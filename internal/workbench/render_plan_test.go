package workbench

import (
	"context"
	"errors"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"yovoice/internal/catalog"
	"yovoice/internal/msg"
	"yovoice/internal/schema"
)

// ADR-0003 的换算基准：22 小时音频在 0.48× 下约 45.7 GPU 小时；卡型单价只负责把 GPU 小时折成计算单元。
func TestRenderCostUnits(t *testing.T) {
	const audioHours = 21.95
	if got := renderGPUHours(audioHours*3600, rtfBreezeTTS2); math.Abs(got-45.7)/45.7 > .01 {
		t.Fatalf("21.95 小时在 0.48× 下应约 45.7 GPU 小时，得到 %.3f", got)
	}
	if got := renderGPUHours(10*3600, 1); got != 10 {
		t.Fatalf("RTF 为 1 时 GPU 小时应等于音频小时：%.3f", got)
	}
	if got, ok := renderUnitPrice("a100"); !ok || math.Abs(got-11.77) > 1e-9 {
		t.Fatalf("A100 单价应为 11.77：%.2f %v", got, ok)
	}
	if got, ok := renderUnitPrice("T4"); !ok || math.Abs(got-1.68) > 1e-9 {
		t.Fatalf("T4 单价应为 1.68：%.2f %v", got, ok)
	}
	if got, ok := renderUnitPrice("L4"); !ok || math.Abs(got-4.82) > 1e-9 {
		t.Fatalf("L4 单价应为 4.82：%.2f %v", got, ok)
	}
	if _, ok := renderUnitPrice("H100"); ok {
		t.Fatal("未配置的卡型不能给出单价")
	}
}

// 成本预估按 cue 字数折算；模型可以是作品自己的模型，也可以是显式覆盖的模型。
func TestEstimateCost(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := schema.DefaultDraft()
	d.ID = schema.NewID()
	d.ModelID = "qwen3-tts-base-q8"
	d.Text = strings.Repeat("字", 14200) // 14.2 字符/秒 → 1000 秒音频
	must(t, w.Store.Update(func(s *schema.State) {
		s.Drafts = append(s.Drafts, d)
	}, true))

	estimate, err := w.EstimateCost([]string{d.ID}, "breeze-tts-2", "A100")
	must(t, err)
	if estimate.Chars != 14200 || math.Abs(estimate.AudioSeconds-1000) > 1e-9 || estimate.ModelID != "breeze-tts-2" || estimate.Card != "A100" {
		t.Fatalf("成本预估不正确：%+v", estimate)
	}
	if got := renderGPUHours(1000, rtfBreezeTTS2); math.Abs(estimate.GPUHours-got) > 1e-9 {
		t.Fatalf("GPU 小时应为 %.4f，得到 %.4f", got, estimate.GPUHours)
	}
	if math.Abs(estimate.Units-estimate.GPUHours*unitsPerGPUHourA100) > 1e-9 {
		t.Fatalf("计算单元应为 GPU 小时 × 单价：%+v", estimate)
	}
	// 空模型按作品自己的模型估算；未知模型不能假装知道成本。
	self, err := w.EstimateCost([]string{d.ID}, "", "T4")
	must(t, err)
	if self.ModelID != d.ModelID {
		t.Fatalf("未指定模型时应使用作品的模型：%+v", self)
	}
	var ce *msg.CallError
	if _, err = w.EstimateCost([]string{d.ID}, "no-such-model", "A100"); !errors.As(err, &ce) || ce.Code != msg.ErrModelUnsupported {
		t.Fatalf("未知模型应拒绝估算：%v", err)
	}
	if _, err = w.EstimateCost([]string{"missing"}, "breeze-tts-2", "A100"); !errors.As(err, &ce) || ce.Code != msg.ErrDraftIDInvalid {
		t.Fatalf("不存在的作品应拒绝估算：%v", err)
	}
}

// 闸门顺序固定：章节闸门 → 成本闸门 → 引擎启动。超额时后台操作根本没有创建；
// 显式 force 才放行，并留下覆盖日志。
func TestRenderBatchBudgetGate(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	verified := renderTestChapter(t, w, true)
	unverified := renderTestChapter(t, w, false)
	zero := 0.0
	opts := RenderOptions{ModelID: "breeze-tts-2", Card: "A100", Balance: &zero}
	var ce *msg.CallError

	if err = w.RenderBatch([]string{verified.ID}, opts); !errors.As(err, &ce) || ce.Code != msg.ErrRenderBudgetExceeded {
		t.Fatalf("结余为零时应拒绝渲染：%v", err)
	}
	if w.Store.Read().Activity != nil {
		t.Fatal("预算拒绝前不应启动后台操作")
	}
	select {
	case <-w.Done():
	default:
		t.Fatal("预算拒绝不应创建操作完成通道")
	}
	if _, err := os.Stat(filepath.Join(w.Store.Root, "runtime", "server.json")); !os.IsNotExist(err) {
		t.Fatal("预算拒绝不应写出引擎配置")
	}
	// 章节闸门排在成本闸门之前：未确认的章节即使超额也先报章节错误。
	if err = w.RenderBatch([]string{unverified.ID}, opts); !errors.As(err, &ce) || ce.Code != msg.ErrChapterNotVerified {
		t.Fatalf("未过章节闸门应先报章节错误：%v", err)
	}
	opts.Force = true
	// 放行后走到模型检查（测试没有安装模型），说明成本闸门没有拦住。
	if err = w.RenderBatch([]string{verified.ID}, opts); !errors.As(err, &ce) || ce.Code != msg.ErrModelRequired {
		t.Fatalf("force 应放行到模型检查：%v", err)
	}
	log, err := os.ReadFile(filepath.Join(w.Store.Root, "logs", "service.log"))
	must(t, err)
	if !strings.Contains(string(log), "render.budget_override") {
		t.Fatal("force 覆盖必须记录日志")
	}
	// 没填结余时不查成本表：目录里不在 ADR 基准表内的模型也可以批量渲染。
	if err = w.RenderBatch([]string{verified.ID}, RenderOptions{}); !errors.As(err, &ce) || ce.Code != msg.ErrModelRequired {
		t.Fatalf("没有结余约束时应跳过成本表：%v", err)
	}
}

// 缓存键必须包含 speakerId：改派后同一句文本必须得到新键。
func TestRenderCacheKeyIncludesSpeaker(t *testing.T) {
	seed := 7
	base := RenderCue{ModelID: "breeze-tts-2-q8", AnchorRev: "anchor-1", Text: "站住。", Instruction: "压低声音", CFG: 1.5, Seed: &seed, SpeakerID: "alice"}
	key := RenderCacheKey(base)
	if key == "" || key != RenderCacheKey(base) {
		t.Fatal("同一组输入应得到稳定的缓存键")
	}
	changed := base
	changed.SpeakerID = "bob"
	if RenderCacheKey(changed) == key {
		t.Fatal("改派后缓存键必须变化")
	}
	for name, mutate := range map[string]func(*RenderCue){
		"model":       func(c *RenderCue) { c.ModelID = "qwen3-tts-base-q8" },
		"anchor":      func(c *RenderCue) { c.AnchorRev = "anchor-2" },
		"text":        func(c *RenderCue) { c.Text = "回来。" },
		"instruction": func(c *RenderCue) { c.Instruction = "提高声音" },
		"cfg":         func(c *RenderCue) { c.CFG = 1.6 },
		"seed":        func(c *RenderCue) { n := 8; c.Seed = &n },
	} {
		next := base
		mutate(&next)
		if RenderCacheKey(next) == key {
			t.Errorf("修改 %s 后缓存键应变化", name)
		}
	}
}

// 分组后同一引擎键的 cue 连续出现，组与组之间不交错；会话参数不同要拆成两条车道。
func TestPlanLanesKeepsEngineGroupsContiguous(t *testing.T) {
	cues := []RenderCue{
		{CueID: "1", ModelID: "breeze-tts-2-q8", SessionOptions: map[string]string{"mode": "offline"}},
		{CueID: "2", ModelID: "qwen3-tts-base-q8", SessionOptions: map[string]string{"mode": "offline"}},
		{CueID: "3", ModelID: "breeze-tts-2-q8", SessionOptions: map[string]string{"mode": "offline"}},
		{CueID: "4", ModelID: "qwen3-tts-base-q8", SessionOptions: map[string]string{"mode": "offline"}},
	}
	lanes := PlanLanes(cues)
	if len(lanes) != 2 {
		t.Fatalf("应按两个模型分成两条车道：%+v", lanes)
	}
	flat := FlattenLanes(lanes)
	if len(flat) != len(cues) {
		t.Fatalf("分组不能丢 cue：%d", len(flat))
	}
	seen := map[string]bool{}
	last := ""
	for _, cue := range flat {
		group := laneKey(cue.ModelID, cue.SessionOptions)
		if group != last {
			if seen[group] {
				t.Fatalf("车道 %s 被拆开交错：%v", group, flat)
			}
			seen[group] = true
			last = group
		}
	}
	// 输入顺序 1,3,1,3 变成 1,3 连续。
	if flat[0].CueID != "1" || flat[1].CueID != "3" || flat[2].CueID != "2" || flat[3].CueID != "4" {
		t.Fatalf("组内应保持输入顺序：%+v", flat)
	}

	streaming := PlanLanes([]RenderCue{
		{CueID: "a", ModelID: "qwen3-tts-base-q8", SessionOptions: map[string]string{"mode": "offline"}},
		{CueID: "b", ModelID: "qwen3-tts-base-q8", SessionOptions: map[string]string{"mode": "streaming"}},
	})
	if len(streaming) != 2 {
		t.Fatalf("会话参数不同应拆成两条车道：%+v", streaming)
	}
}

// 清单续跑：已完成的 cue 不再出现；换引擎版本后旧记录全部失效；失败条目仍然待做。
func TestRenderManifestResume(t *testing.T) {
	root := t.TempDir()
	first := RenderCue{DraftID: "d", CueID: "one", SpeakerID: "s", ModelID: "m", Text: "一"}
	second := RenderCue{DraftID: "d", CueID: "two", SpeakerID: "s", ModelID: "m", Text: "二"}
	manifest := RenderManifest{EngineVersion: "v1"}
	manifest.Put(manifestEntry(first, "done"))
	must(t, WriteRenderManifest(root, manifest))

	loaded, err := ReadRenderManifest(root)
	must(t, err)
	if pending := PendingRenderCues([]RenderCue{first, second}, loaded, "v1"); len(pending) != 1 || pending[0].CueID != "two" {
		t.Fatalf("已完成的 cue 不应再出现：%+v", pending)
	}
	if pending := PendingRenderCues([]RenderCue{first, second}, loaded, "v2"); len(pending) != 2 {
		t.Fatalf("引擎版本变化后旧记录应失效：%+v", pending)
	}
	loaded.Entries[0].Status = "failed"
	must(t, WriteRenderManifest(root, loaded))
	if pending := PendingRenderCues([]RenderCue{first, second}, loaded, "v1"); len(pending) != 2 {
		t.Fatalf("失败条目应继续待做：%+v", pending)
	}
}

// 批量渲染跑完写清单；第二次启动时全部命中清单，不重新请求引擎。
func TestRenderBatchResumesCompletedCues(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	executable, err := os.Executable()
	must(t, err)
	model := filepath.Join(w.Store.Root, "models", "fake.gguf")
	must(t, os.WriteFile(model, []byte("test"), 0600))
	reference := filepath.Join(t.TempDir(), "reference.wav")
	must(t, os.WriteFile(reference, wav(), 0600))
	voice, err := w.ImportVoice(context.Background(), reference, "")
	must(t, err)
	d := renderTestChapter(t, w, true)
	for _, stored := range w.Store.Read().Drafts {
		if stored.ID == d.ID {
			d = stored
		}
	}
	d.VoiceID = &voice.ID
	must(t, w.SaveDraft(d))
	must(t, w.Store.Update(func(s *schema.State) {
		s.Models = []schema.InstalledModel{{ID: d.ModelID, Path: model}}
		s.RuntimePath = &executable
		s.RuntimeBackend = ptr("cpu")
		s.RuntimeVersion = ptr(catalog.EngineVersion)
		s.Preferences.Backend = "cpu"
	}, true))

	opts := RenderOptions{ModelID: "breeze-tts-2", Card: "A100"}
	must(t, w.RenderBatch([]string{d.ID}, opts))
	<-w.Done()
	state := w.Store.Read()
	if state.Activity == nil || state.Activity.Status != "completed" || len(state.History) != 2 {
		t.Fatalf("批量渲染应完成两句：%+v", state.Activity)
	}
	for _, g := range state.History {
		if g.Segment == nil || g.Segment.CueID == "" {
			t.Fatalf("批量渲染的版本应带上 cue 来源：%+v", g)
		}
	}
	manifest, err := ReadRenderManifest(w.Store.Root)
	must(t, err)
	if len(manifest.Entries) != 2 {
		t.Fatalf("清单应记录两条 take：%+v", manifest)
	}
	for _, entry := range manifest.Entries {
		if entry.Status != "done" || entry.TakeID == "" {
			t.Fatalf("清单条目应记录完成的 take：%+v", entry)
		}
	}
	// 第二次全部命中清单，不应再追加版本，也不应启动新的后台操作。
	must(t, w.RenderBatch([]string{d.ID}, opts))
	if len(w.Store.Read().History) != 2 {
		t.Fatal("已完成的 cue 不应重做")
	}
	select {
	case <-w.Done():
	default:
		t.Fatal("全部命中时不应启动后台操作")
	}
}

// renderTestChapter 保存一个两句的 story 章节，按需要过闸门。
func renderTestChapter(t *testing.T, w *Workbench, verify bool) schema.Draft {
	t.Helper()
	d := schema.DefaultDraft()
	d.Kind = "story"
	d.Title = "第 1 章"
	d.Text = "第一句。\n第二句。"
	d.Subtitles = &schema.SubtitleDocument{
		Speakers: []schema.SubtitleSpeaker{{ID: "narrator"}},
		Cues: []schema.SubtitleCue{
			{ID: "one", Start: 0, End: 1000, Text: "第一句。", SpeakerID: "narrator"},
			{ID: "two", Start: 1000, End: 2000, Text: "第二句。", SpeakerID: "narrator"},
		},
	}
	must(t, w.SaveDraft(d))
	if verify {
		must(t, w.ConfirmRead(d.ID))
		must(t, w.VerifyChapter(d.ID, ""))
	}
	return d
}
