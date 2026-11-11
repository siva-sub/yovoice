package schema

import (
	"encoding/json"
	"strings"
	"testing"
)

// 默认电平区间与侧链要求来自 ADR-0004；空角色与未知角色不属于四层。
func TestTrackRolePoliciesMatchADR(t *testing.T) {
	for _, tc := range []struct {
		role       string
		min, max   float64
		duck, lead float64
	}{
		{TrackRoleAmbience, -30, -25, MixSidechainMinDB, 0},
		{TrackRoleScore, -28, -24, MixSidechainMinDB, 0},
		{TrackRoleBed, -23, -20, MixSidechainMinDB, 0},
		{TrackRoleHit, -16, -8, MixSidechainMinDB, MixHitLeadRatio},
	} {
		policy, ok := TrackRolePolicyFor(tc.role)
		if !ok || policy.MinGainDB != tc.min || policy.MaxGainDB != tc.max || policy.MinDuckDB != tc.duck || policy.LeadRatio != tc.lead {
			t.Errorf("%s 的策略不符合 ADR-0004：%+v %v", tc.role, policy, ok)
		}
	}
	if _, ok := TrackRolePolicyFor(""); ok {
		t.Fatal("空角色表示旧数据的普通音轨，不应有分层策略")
	}
	if _, ok := TrackRolePolicyFor("reverb"); ok {
		t.Fatal("未知角色不应有默认策略")
	}
}

// 新字段都是可选的：旧时间线序列化后不会多出 role/cueId，读回也保持零值。
func TestAudioRoleFieldsAreOptional(t *testing.T) {
	timeline := AudioTimeline{Tracks: []AudioLane{{ID: "track", Name: "旁白", Clips: []AudioClip{{ID: "clip", Start: 0, Duration: 1}}}}}
	data, err := json.Marshal(timeline)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), `"role"`) || strings.Contains(string(data), `"cueId"`) {
		t.Fatalf("旧数据不应写出新字段：%s", data)
	}
	var decoded AudioTimeline
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Tracks[0].Role != "" || decoded.Tracks[0].Clips[0].CueID != "" {
		t.Fatalf("旧数据读回后新字段应为空：%+v", decoded.Tracks[0])
	}
}
