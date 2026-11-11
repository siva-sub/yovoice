package schema

import (
	"encoding/json"
	"strings"
	"testing"
)

// 角色权限：owner 全部可改，reviewer 只能改 speakerId/kind，listener 与未登记角色只读。
func TestCanEdit(t *testing.T) {
	cases := []struct {
		role  string
		field string
		want  bool
	}{
		{RoleOwner, "text", true},
		{RoleOwner, "speakerId", true},
		{RoleOwner, "kind", true},
		{RoleReviewer, "speakerId", true},
		{RoleReviewer, "kind", true},
		{RoleReviewer, "text", false},
		{RoleReviewer, "confidence", false},
		{RoleReviewer, "evidence", false},
		{RoleListener, "speakerId", false},
		{RoleListener, "kind", false},
		{RoleListener, "text", false},
		{"", "speakerId", false},
		{"unknown", "speakerId", false},
	}
	for _, c := range cases {
		if got := CanEdit(c.role, c.field); got != c.want {
			t.Errorf("CanEdit(%q, %q) = %v，应为 %v", c.role, c.field, got, c.want)
		}
	}
}

// 旧状态没有 reviewers 字段时能正常反序列化；带角色表时按 ID、姓名、角色往返。
func TestReviewersOptionalRoundTrip(t *testing.T) {
	legacy := State{}
	data, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), `"reviewers"`) {
		t.Fatalf("未设置角色表时不应出现在 JSON 里：%s", data)
	}
	var decoded State
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("旧状态应可读取：%v", err)
	}
	if len(decoded.Reviewers) != 0 {
		t.Fatalf("旧状态的角色表应为空：%+v", decoded.Reviewers)
	}

	want := []Reviewer{
		{ID: "reviewer-a", Name: "审校甲", Role: RoleReviewer},
		{ID: "listener-b", Name: "听众乙", Role: RoleListener},
	}
	data, err = json.Marshal(State{Reviewers: want})
	if err != nil {
		t.Fatal(err)
	}
	decoded = State{}
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded.Reviewers) != 2 || decoded.Reviewers[0] != want[0] || decoded.Reviewers[1] != want[1] {
		t.Fatalf("角色表未按原值往返：%+v", decoded.Reviewers)
	}
}
