package workbench

import (
	"errors"
	"testing"
	"time"

	"yovoice/internal/msg"
	"yovoice/internal/schema"
)

// chapterDraft 保存一个两句话的章节作品；模型没有安装，通过闸门后会停在模型检查上。
func chapterDraft(t *testing.T, w *Workbench, kind string) schema.Draft {
	t.Helper()
	d := schema.DefaultDraft()
	d.Kind = kind
	d.Title = "第 6 章"
	d.Text = "夜里。\n我来了。"
	d.Subtitles = &schema.SubtitleDocument{
		Speakers: []schema.SubtitleSpeaker{{ID: "narrator"}},
		Cues: []schema.SubtitleCue{
			{ID: "one", Start: 0, End: 1000, Text: "夜里。", SpeakerID: "narrator"},
			{ID: "two", Start: 1000, End: 2000, Text: "我来了。", SpeakerID: "narrator"},
		},
	}
	must(t, w.SaveDraft(d))
	return d
}

// 章节未 verified 时，整章与单句生成都在启动引擎之前被拒绝；通过闸门后才走到模型检查。
func TestGenerateStoryRequiresVerifiedGate(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	var ce *msg.CallError
	if err = w.Generate(d); !errors.As(err, &ce) || ce.Code != msg.ErrChapterNotVerified {
		t.Fatalf("未过闸门应拒绝生成：%v", err)
	}
	if err = w.GenerateCue(d, d.Subtitles.Cues[0].ID, ""); !errors.As(err, &ce) || ce.Code != msg.ErrChapterNotVerified {
		t.Fatalf("未过闸门应拒绝单句生成：%v", err)
	}
	if w.Store.Read().Activity != nil {
		t.Fatal("闸门拒绝前不应启动后台操作")
	}
	must(t, w.ConfirmRead(d.ID))
	must(t, w.VerifyChapter(d.ID, ""))
	verified := w.Store.Read().Drafts[0]
	if verified.ChapterStatus() != schema.GateVerified {
		t.Fatalf("确认后应为 verified：%+v", verified.Gate)
	}
	if err = w.Generate(verified); !errors.As(err, &ce) || ce.Code != msg.ErrModelRequired {
		t.Fatalf("通过闸门后应走到模型检查：%v", err)
	}
}

// 闸门只约束 kind=story；旧的 subtitle 与没有 kind 的字幕作品保持可生成，防止闸门被扩大到旧作品上。
func TestGateAppliesOnlyToStoryKind(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	for _, kind := range []string{"subtitle", ""} {
		d := chapterDraft(t, w, kind)
		err := w.Generate(d)
		if err == nil {
			t.Fatalf("kind=%q 没有模型时不应开始生成", kind)
		}
		var ce *msg.CallError
		if !errors.As(err, &ce) || ce.Code != msg.ErrModelRequired {
			t.Fatalf("kind=%q 应走到模型检查而不是闸门：%v", kind, err)
		}
	}
	// 同样的作品改成 story 就被闸门拦住。
	d := chapterDraft(t, w, "story")
	var ce *msg.CallError
	if err := w.Generate(d); !errors.As(err, &ce) || ce.Code != msg.ErrChapterNotVerified {
		t.Fatalf("kind=story 应被闸门拒绝：%v", err)
	}
}

// 闸门状态机：pending 不能直接确认；读过记指纹且不重算；verified 后确认读过是 no-op；重开回到 pending。
func TestChapterGateTransitions(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	var ce *msg.CallError
	if err = w.VerifyChapter(d.ID, "跳过通读"); !errors.As(err, &ce) || ce.Code != msg.ErrChapterNotVerified {
		t.Fatalf("pending 不能直接确认：%v", err)
	}
	must(t, w.ConfirmRead(d.ID))
	gate := w.Store.Read().Drafts[0].Gate
	if gate == nil || gate.Status != schema.GateRead || gate.TextSHA != schema.TextSHA(d.Text) {
		t.Fatalf("确认读过应记为 read 并记下正文指纹：%+v", gate)
	}
	readSHA := gate.TextSHA
	// 正文变化后再次确认读过不重算指纹：清空闸门由改写正文的一方负责（契约第四节）。
	must(t, w.Store.Update(func(s *schema.State) { s.Drafts[0].Text = "改过的正文" }, true))
	must(t, w.ConfirmRead(d.ID))
	if got := w.Store.Read().Drafts[0].Gate.TextSHA; got != readSHA {
		t.Fatal("ConfirmRead 重算了正文指纹")
	}
	must(t, w.Store.Update(func(s *schema.State) { s.Drafts[0].Text = d.Text }, true))
	must(t, w.VerifyChapter(d.ID, "通读确认"))
	gate = w.Store.Read().Drafts[0].Gate
	if gate.Status != schema.GateVerified || gate.TextSHA != readSHA || gate.Note != "通读确认" {
		t.Fatalf("确认应记为 verified 并保留指纹：%+v", gate)
	}
	if _, err := time.Parse(time.RFC3339, gate.VerifiedAt); err != nil {
		t.Fatalf("确认时间应为 RFC3339：%q", gate.VerifiedAt)
	}
	verifiedAt := gate.VerifiedAt
	// 已 verified 后再次确认读过不降级、不重算指纹（契约第三节）。
	must(t, w.Store.Update(func(s *schema.State) { s.Drafts[0].Text = "又改过的正文" }, true))
	must(t, w.ConfirmRead(d.ID))
	if got := w.Store.Read().Drafts[0].Gate; got.Status != schema.GateVerified || got.TextSHA != readSHA || got.VerifiedAt != verifiedAt {
		t.Fatalf("ConfirmRead 不应改动已确认的章节：%+v", got)
	}
	must(t, w.ReopenChapter(d.ID))
	if gate = w.Store.Read().Drafts[0].Gate; gate == nil || gate.Status != schema.GatePending || gate.TextSHA != "" || gate.VerifiedAt != "" || gate.Note != "" {
		t.Fatalf("重开应退回 pending 并清空确认记录：%+v", gate)
	}
}

// 归属只能指向已有的书；默认卷“未归档”始终可用。
func TestMoveToBookRequiresExistingBook(t *testing.T) {
	w, err := New(t.TempDir())
	must(t, err)
	defer w.Close()
	d := chapterDraft(t, w, "story")
	if err = w.MoveToBook(d.ID, "missing"); err == nil {
		t.Fatal("不存在的书应被拒绝")
	}
	must(t, w.MoveToBook(d.ID, schema.UnfiledBookID))
	if got := w.Store.Read().Drafts[0].BookID; got != schema.UnfiledBookID {
		t.Fatalf("应归入未归档：%q", got)
	}
	must(t, w.Store.Update(func(s *schema.State) {
		s.Books = append(s.Books, schema.Book{ID: "vol-7", Title: "第七卷"})
	}, true))
	must(t, w.MoveToBook(d.ID, "vol-7"))
	if got := w.Store.Read().Drafts[0].BookID; got != "vol-7" {
		t.Fatalf("应移到第七卷：%q", got)
	}
	if err = w.MoveToBook("missing", schema.UnfiledBookID); err == nil {
		t.Fatal("不存在的作品应被拒绝")
	}
}
