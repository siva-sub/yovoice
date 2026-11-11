package workbench

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"yovoice/internal/schema"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// assistantChapterToolNames 是这一轮新增的有声书流程工具；注册表里必须一个不少。
var assistantChapterToolNames = []string{"list_pending_chapters", "verify_chapter", "reassign_cue", "record_qa", "check_mix"}

// 新工具与既有工具注册在同一个 MCP server 上：assistantTools 允许的每个名字都必须真的注册，
// 否则 ocho 拿到 --tools 却找不到工具，助手只能瞎猜。
func TestAssistantChapterToolsRegistered(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	server, err := w.newAssistantServer().Connect(ctx, serverTransport, nil)
	must(t, err)
	defer server.Close()
	client, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil).Connect(ctx, clientTransport, nil)
	must(t, err)
	defer client.Close()
	listed, err := client.ListTools(ctx, nil)
	must(t, err)
	names := map[string]bool{}
	for _, tool := range listed.Tools {
		names[tool.Name] = true
	}
	for _, name := range assistantTools {
		if !names[name] {
			t.Errorf("assistantTools 里的 %s 没有注册到 MCP server", name)
		}
	}
	for _, name := range assistantChapterToolNames {
		if !names[name] {
			t.Errorf("有声书流程工具 %s 未注册", name)
		}
	}
	// 成片改派靠模型在 cue_ids 与 kind 上选参数，入参 schema 里必须真的有这两个名字。
	for _, tool := range listed.Tools {
		if tool.Name != "reassign_cue" {
			continue
		}
		input, err := json.Marshal(tool.InputSchema)
		must(t, err)
		if !strings.Contains(string(input), `"cue_ids"`) || !strings.Contains(string(input), `"kind"`) {
			t.Fatalf("reassign_cue 的入参应暴露 cue_ids 与 kind：%s", input)
		}
	}
}

// 改派后该句的机器置信度被清空（ADR-0002）；不传说话人、说话人或 cue 不存在都拒绝且不写状态。
func TestReassignCueClearsConfidence(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "夜里。", "我来了。")
	cue := d.Subtitles.Cues[0]
	if cue.Confidence == nil {
		t.Fatal("测试数据应带机器置信度")
	}
	before := reviewDraftsJSON(t, w)
	for _, tc := range []struct {
		name  string
		input reassignCueInput
	}{
		{"缺说话人", reassignCueInput{DraftID: d.ID, CueID: cue.ID}},
		{"说话人不存在", reassignCueInput{DraftID: d.ID, CueID: cue.ID, SpeakerID: "missing"}},
		{"cue 不存在", reassignCueInput{DraftID: d.ID, CueID: "missing", SpeakerID: "jet"}},
		{"作品不存在", reassignCueInput{DraftID: "missing", CueID: cue.ID, SpeakerID: "jet"}},
	} {
		if _, _, err := w.reassignCueTool(context.Background(), toolReq, tc.input); err == nil {
			t.Fatalf("%s 应被拒绝", tc.name)
		}
	}
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("被拒绝的改派不应写状态")
	}

	if _, _, err := w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueID: cue.ID, SpeakerID: "jet"}); err != nil {
		t.Fatal(err)
	}
	got := reviewLocalCue(t, w, d.ID, 0)
	if got.SpeakerID != "jet" {
		t.Fatalf("应改派给 Jet：%+v", got)
	}
	if got.Confidence != nil {
		t.Fatalf("改派后必须清空机器置信度：%v", *got.Confidence)
	}
	// 只改这一句：另一句仍是原说话人与原置信度。
	if other := reviewLocalCue(t, w, d.ID, 1); other.SpeakerID != "narrator" || other.Confidence == nil {
		t.Fatalf("其它句子不应被改动：%+v", other)
	}
}

// check_mix 读出超区间电平：score 轨 −5 dB 不在 −28…−24 dB 内，问题列表要报出来。
func TestCheckMixToolReportsGainOutsideRange(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := schema.DefaultDraft()
	d.Kind, d.Title, d.Text = "story", "第 1 章", "台词"
	d.Subtitles = &schema.SubtitleDocument{Speakers: []schema.SubtitleSpeaker{{ID: "narrator"}}, Cues: []schema.SubtitleCue{{ID: "cue-1", Start: 10000, End: 12000, Text: "台词", SpeakerID: "narrator"}}}
	d.Timeline = &schema.AudioTimeline{Tracks: []schema.AudioLane{{
		ID: "track-score", Name: "配乐", Role: schema.TrackRoleScore, GainDB: -5, DuckDB: 18,
		Clips: []schema.AudioClip{{ID: "clip-score", GenerationID: "take-1", Start: 0, Duration: 20}},
	}}}
	must(t, w.Store.Update(func(s *schema.State) {
		s.History = append(s.History, schema.Generation{ID: "take-1", FileName: "take-1.wav", Duration: 30, Settings: schema.Draft{ID: d.ID}})
	}, true))
	must(t, w.SaveDraft(d))

	_, out, err := w.checkMixTool(context.Background(), toolReq, workIDInput{ID: d.ID})
	must(t, err)
	summary, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("check_mix 应返回摘要：%T", out)
	}
	issues, ok := summary["issues"].([]MixIssue)
	if !ok || len(issues) == 0 || summary["clean"] != false {
		t.Fatalf("应报出混音问题：%+v", summary)
	}
	gains := issuesWithCode(issues, MixIssueGain)
	if len(gains) != 1 || gains[0].Role != schema.TrackRoleScore || !strings.Contains(gains[0].Message, "电平") {
		t.Fatalf("应报出 score 轨电平越界：%+v", issues)
	}
}

// verify_chapter 替用户确认已读并校验，闸门从 pending 直接到 verified，说明被保留。
func TestVerifyChapterToolConfirmsRead(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	if _, _, err := w.verifyChapterTool(context.Background(), toolReq, verifyChapterInput{ID: d.ID, Note: "通读确认"}); err != nil {
		t.Fatal(err)
	}
	gate := w.Store.Read().Drafts[0].Gate
	if gate == nil || gate.Status != schema.GateVerified || gate.TextSHA != schema.TextSHA(d.Text) || gate.Note != "通读确认" || gate.VerifiedAt == "" {
		t.Fatalf("应直接确认到 verified：%+v", gate)
	}
}

// record_qa 判 fail 缺 note 被拒；合法判定写入后能读回。
func TestRecordQAToolRequiresNoteOnFail(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	if _, _, err := w.recordQATool(context.Background(), toolReq, recordQAInput{ID: d.ID, Verdict: schema.QAVerdictFail}); err == nil {
		t.Fatal("fail 缺少 note 应被拒绝")
	}
	if qa := w.Store.Read().Drafts[0].QA; qa != nil {
		t.Fatalf("被拒绝的验收不应写入：%+v", qa)
	}
	if _, _, err := w.recordQATool(context.Background(), toolReq, recordQAInput{ID: d.ID, Verdict: schema.QAVerdictFail, Note: "第 2 段有 2 秒空洞", WER: .03, LUFS: -19.2, Peak: -3.4, Silences: 2}); err != nil {
		t.Fatal(err)
	}
	qa := w.Store.Read().Drafts[0].QA
	if qa == nil || qa.Verdict != schema.QAVerdictFail || qa.Note == "" || qa.WER != .03 || qa.At == "" {
		t.Fatalf("验收结果未写入：%+v", qa)
	}
}

// list_pending_chapters 只读返回书级待办，按章号升序；空 bookId 返回所有书。
func TestListPendingChaptersTool(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	reviewTestBook(t, w, "vol-6", "第六卷")
	reviewTestBook(t, w, "vol-7", "第七卷")
	first := reviewTestStory(t, w, "vol-6", "第 1 章", "未读。")
	second := reviewTestStory(t, w, "vol-6", "第 2 章", "已读未校验。")
	other := reviewTestStory(t, w, "vol-7", "第 1 章", "另一本书。")
	must(t, w.ConfirmRead(second.ID))
	before := reviewDraftsJSON(t, w)
	_, out, err := w.listPendingChaptersTool(context.Background(), toolReq, pendingChaptersInput{})
	must(t, err)
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("list_pending_chapters 是只读的，不应写状态")
	}
	summary, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("list_pending_chapters 应返回摘要：%T", out)
	}
	items, ok := summary["items"].([]QueueItem)
	if !ok || summary["total"] != 3 {
		t.Fatalf("应返回三件待办：%+v", summary)
	}
	if items[0].DraftID != first.ID || items[0].Kind != QueueRead || items[1].DraftID != second.ID || items[1].Kind != QueueVerify || items[2].DraftID != other.ID || items[2].Kind != QueueRead {
		t.Fatalf("待办顺序或类型不对：%+v", items)
	}
}

// reassign_cue 支持可选 kind 与 cue_ids：单句可同时改类型，cue_ids 一次改多句；非法 kind 与缺目标被拒，成片缺失一句时整批不写。
func TestReassignCueToolKindAndBatch(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := reviewTestStory(t, w, "vol-6", "第 1 章", "一", "二", "三", "四", "五")
	cues := d.Subtitles.Cues

	_, out, err := w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueID: cues[0].ID, SpeakerID: "jet", Kind: "dialogue"})
	must(t, err)
	if got := reviewLocalCue(t, w, d.ID, 0); got.SpeakerID != "jet" || got.Kind != "dialogue" || got.Confidence != nil {
		t.Fatalf("单句应同时改说话人与类型：%+v", got)
	}
	if summary, ok := out.(map[string]any); !ok || summary["cueId"] != cues[0].ID || summary["kind"] != "dialogue" {
		t.Fatalf("单句应返回改派摘要：%T %v", out, out)
	}

	ids := []string{cues[1].ID, cues[2].ID, cues[3].ID}
	_, out, err = w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueIDs: ids, SpeakerID: "narrator2", Kind: "thought"})
	must(t, err)
	if summary, ok := out.(map[string]any); !ok || summary["changed"] != 3 {
		t.Fatalf("cue_ids 应返回改派条数：%T %v", out, out)
	}
	for i := 1; i <= 3; i++ {
		if cue := reviewLocalCue(t, w, d.ID, i); cue.SpeakerID != "narrator2" || cue.Kind != "thought" || cue.Confidence != nil {
			t.Fatalf("第 %d 句应整批改派：%+v", i+1, cue)
		}
	}
	if cue := reviewLocalCue(t, w, d.ID, 4); cue.SpeakerID != "narrator" || cue.Kind != "narration" || cue.Confidence == nil {
		t.Fatalf("未选中的句子不应被改动：%+v", cue)
	}

	before := reviewDraftsJSON(t, w)
	if _, _, err := w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueIDs: []string{cues[4].ID}, Kind: "shout"}); err == nil {
		t.Fatal("非法 kind 应被拒绝")
	}
	if _, _, err := w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueIDs: append(ids, "missing"), SpeakerID: "narrator"}); err == nil {
		t.Fatal("cue 不存在时整批应被拒绝")
	}
	if _, _, err := w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueID: cues[4].ID}); err == nil {
		t.Fatal("说话人与类型都没给时应被拒绝")
	}
	if after := reviewDraftsJSON(t, w); after != before {
		t.Fatal("被拒绝的改派不应写状态")
	}

	// 只改类型：speakerId 留空时说话人保持原样；单句与成片两条路都支持。
	_, out, err = w.reassignCueTool(context.Background(), toolReq, reassignCueInput{DraftID: d.ID, CueID: cues[4].ID, Kind: "thought"})
	must(t, err)
	if summary, ok := out.(map[string]any); !ok || summary["changed"] != 1 {
		t.Fatalf("单句只改类型应返回 1 条：%T %v", out, out)
	}
	if got := reviewLocalCue(t, w, d.ID, 4); got.SpeakerID != "narrator" || got.Kind != "thought" || got.Confidence != nil {
		t.Fatalf("只改类型应保留说话人：%+v", got)
	}
}
