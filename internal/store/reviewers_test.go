package store

import (
	"slices"
	"testing"

	"yovoice/internal/schema"
)

// 复核角色表要真正持久化：写入一个 reviewer 与一个 listener，重开后姓名与角色都在。
func TestReviewersRoundTrip(t *testing.T) {
	root := t.TempDir()
	s, err := New(root)
	must(t, err)
	want := []schema.Reviewer{
		{ID: schema.NewID(), Name: "审校甲", Role: schema.RoleReviewer},
		{ID: schema.NewID(), Name: "听众乙", Role: schema.RoleListener},
	}
	must(t, s.Update(func(state *schema.State) {
		state.Reviewers = slices.Clone(want)
	}, true))
	must(t, s.Close())

	s, err = New(root)
	must(t, err)
	defer s.Close()
	if got := s.Read().Reviewers; !slices.Equal(got, want) {
		t.Fatalf("复核角色表未往返：%+v", got)
	}
}

// 旧库没有 reviewers 表（或没有任何角色）时读取不报错；保存其他字段不会凭空造出角色。
func TestLegacyStateWithoutReviewersStaysEmpty(t *testing.T) {
	root := t.TempDir()
	s, err := New(root)
	must(t, err)
	// 退回没有角色表的版本，模拟旧状态库；重新打开时由迁移补表。
	_, err = s.db.Exec("DROP TABLE reviewers")
	must(t, err)
	_, err = s.db.Exec("PRAGMA user_version = 6")
	must(t, err)
	must(t, s.Close())

	s, err = New(root)
	must(t, err)
	if got := s.Read().Reviewers; len(got) != 0 {
		t.Fatalf("旧库的角色表应为空：%+v", got)
	}
	must(t, s.Update(func(state *schema.State) {
		state.Drafts[0].Title = "旧数据再保存"
	}, true))
	var count int
	must(t, s.db.QueryRow("SELECT COUNT(*) FROM reviewers").Scan(&count))
	if count != 0 {
		t.Fatalf("保存其他字段不应造出角色：%d", count)
	}
	must(t, s.Close())

	s, err = New(root)
	must(t, err)
	defer s.Close()
	if got := s.Read().Reviewers; len(got) != 0 {
		t.Fatalf("重开后角色表仍应为空：%+v", got)
	}
}
