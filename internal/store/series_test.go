package store

import (
	"slices"
	"testing"
	"yovoice/internal/schema"
)

// 系列是新表：新库能保存与读回；从还没有系列表的旧版本升级后，系列为空且原数据保持可读。
func TestSeriesPersistAndLegacyMigration(t *testing.T) {
	root := t.TempDir()
	s, err := New(root)
	must(t, err)
	series := schema.Series{ID: "shadow-slave", Title: "Shadow Slave", CreatedAt: "2026-11-11T00:00:00Z"}
	must(t, s.Update(func(state *schema.State) {
		state.Series = []schema.Series{series}
	}, true))
	must(t, s.Close())

	s, err = New(root)
	must(t, err)
	if got := s.Read().Series; len(got) != 1 || got[0] != series {
		t.Fatalf("系列应能保存与读回：%+v", got)
	}
	chapter := schema.Draft{ID: schema.NewID(), Title: "第 1 章", Kind: "story", Text: "正文"}
	must(t, s.Update(func(state *schema.State) {
		state.Drafts = append(state.Drafts, chapter)
	}, true))
	// 回到还没有系列表的版本；升级步骤应补建空表，旧卷与章节不受影响。
	_, err = s.db.Exec("DROP TABLE series")
	must(t, err)
	_, err = s.db.Exec("PRAGMA user_version = 5")
	must(t, err)
	must(t, s.Close())

	s, err = New(root)
	must(t, err)
	defer s.Close()
	state := s.Read()
	if len(state.Series) != 0 {
		t.Fatalf("旧库升级后不应凭空出现系列：%+v", state.Series)
	}
	if len(state.Books) != 1 || state.Books[0].ID != schema.UnfiledBookID {
		t.Fatalf("升级不应影响默认卷：%+v", state.Books)
	}
	if !slices.ContainsFunc(state.Drafts, func(d schema.Draft) bool { return d.ID == chapter.ID }) {
		t.Fatal("升级不应丢失章节")
	}
}
