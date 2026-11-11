package schema

import (
	"encoding/json"
	"testing"
)

// 系列是 ADR-0014 的最小集：默认状态有空的系列列表，旧数据没有 series 字段时也能读。
func TestSeriesDefaultsAndLegacyJSON(t *testing.T) {
	if DefaultState().Series == nil || len(DefaultState().Series) != 0 {
		t.Fatalf("默认状态应含空的系列列表：%+v", DefaultState().Series)
	}
	var state State
	if err := json.Unmarshal([]byte(`{"books":[{"id":"unfiled","title":"未归档"}]}`), &state); err != nil {
		t.Fatalf("没有 series 字段的旧状态应能读取：%v", err)
	}
	if len(state.Series) != 0 || len(state.Books) != 1 {
		t.Fatalf("旧状态的系列应为空且卷保持可读：%+v", state)
	}
	encoded, err := json.Marshal(Series{ID: "shadow-slave", Title: "Shadow Slave", CreatedAt: "2026-11-11T00:00:00Z"})
	if err != nil {
		t.Fatal(err)
	}
	if string(encoded) != `{"id":"shadow-slave","title":"Shadow Slave","createdAt":"2026-11-11T00:00:00Z"}` {
		t.Fatalf("系列字段名应与契约一致：%s", encoded)
	}
}
