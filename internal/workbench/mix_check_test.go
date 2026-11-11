package workbench

import (
	"testing"

	"yovoice/internal/schema"
)

func mixTestCue(id string, startMS int64) schema.SubtitleCue {
	return schema.SubtitleCue{ID: id, Start: startMS, End: startMS + 2000, Text: "台词", SpeakerID: "speaker"}
}

func mixTestDraft(tracks ...schema.AudioLane) schema.Draft {
	return schema.Draft{Subtitles: &schema.SubtitleDocument{Cues: []schema.SubtitleCue{mixTestCue("cue-1", 10000)}}, Timeline: &schema.AudioTimeline{Tracks: tracks}}
}

func mixTestLane(role string, gain, duck float64, clips ...schema.AudioClip) schema.AudioLane {
	return schema.AudioLane{ID: "track-" + role, Name: role, Role: role, GainDB: gain, DuckDB: duck, Clips: clips}
}

func issuesWithCode(issues []MixIssue, code MixIssueCode) []MixIssue {
	var matched []MixIssue
	for _, issue := range issues {
		if issue.Code == code {
			matched = append(matched, issue)
		}
	}
	return matched
}

// 四层音轨都在 ADR-0004 的电平区间内、侧链达到 16 dB、hit 提前 40% 时不应报问题。
func TestCheckMixAcceptsADRLayers(t *testing.T) {
	d := mixTestDraft(
		mixTestLane(schema.TrackRoleAmbience, -27, 18, schema.AudioClip{ID: "amb", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleScore, -26, 18, schema.AudioClip{ID: "score", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleBed, -21.5, 20, schema.AudioClip{ID: "bed", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleHit, -12, 18, schema.AudioClip{ID: "hit", CueID: "cue-1", Start: 8.4, Duration: 4}),
	)
	if issues := CheckMix(d, DefaultMixPolicy()); len(issues) != 0 {
		t.Fatalf("合规的四层配置不应报问题：%+v", issues)
	}
}

// 四类角色各有一条电平越界时各报一条；边界端点算通过。
func TestCheckMixReportsGainPerRole(t *testing.T) {
	d := mixTestDraft(
		mixTestLane(schema.TrackRoleAmbience, -20, 18, schema.AudioClip{ID: "amb", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleScore, -5, 18, schema.AudioClip{ID: "score", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleBed, -30, 18, schema.AudioClip{ID: "bed", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleHit, -20, 18, schema.AudioClip{ID: "hit", CueID: "cue-1", Start: 8.4, Duration: 4}),
	)
	issues := CheckMix(d, DefaultMixPolicy())
	gains := issuesWithCode(issues, MixIssueGain)
	if len(gains) != 4 {
		t.Fatalf("四类角色越界应各报一条电平问题，得到 %+v", issues)
	}
	roles := map[string]bool{}
	for _, issue := range gains {
		roles[issue.Role] = true
	}
	for _, role := range []string{schema.TrackRoleAmbience, schema.TrackRoleScore, schema.TrackRoleBed, schema.TrackRoleHit} {
		if !roles[role] {
			t.Fatalf("%s 的电平越界没有报出来：%+v", role, gains)
		}
	}

	edges := mixTestDraft(
		mixTestLane(schema.TrackRoleAmbience, -30, 16, schema.AudioClip{ID: "amb", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleScore, -28, 16, schema.AudioClip{ID: "score", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleBed, -20, 16, schema.AudioClip{ID: "bed", Start: 0, Duration: 20}),
		mixTestLane(schema.TrackRoleHit, -8, 16, schema.AudioClip{ID: "hit", CueID: "cue-1", Start: 8.4, Duration: 4}),
	)
	if issues := CheckMix(edges, DefaultMixPolicy()); len(issues) != 0 {
		t.Fatalf("电平与侧链的边界值应算通过：%+v", issues)
	}
}

// 侧链不足 16 dB 报一条；达到 16 dB 不报。
func TestCheckMixReportsShallowSidechain(t *testing.T) {
	clip := schema.AudioClip{ID: "bed", Start: 0, Duration: 20}
	d := mixTestDraft(mixTestLane(schema.TrackRoleBed, -21, 12, clip))
	issues := CheckMix(d, DefaultMixPolicy())
	sidechains := issuesWithCode(issues, MixIssueSidechain)
	if len(sidechains) != 1 || sidechains[0].TrackID != "track-bed" {
		t.Fatalf("侧链 12 dB 应报一条问题：%+v", issues)
	}

	d.Timeline.Tracks[0].DuckDB = 16
	if issues = CheckMix(d, DefaultMixPolicy()); len(issues) != 0 {
		t.Fatalf("侧链 16 dB 应通过：%+v", issues)
	}
}

// hit 前移 30%/40%/50% 都在 40%±10 内；20% 与 60% 越界各报一条。
func TestCheckMixHitLeadTolerance(t *testing.T) {
	clips := []schema.AudioClip{
		{ID: "lead-30", CueID: "cue-1", Start: 8.8, Duration: 4},
		{ID: "lead-40", CueID: "cue-1", Start: 8.4, Duration: 4},
		{ID: "lead-50", CueID: "cue-1", Start: 8.0, Duration: 4},
		{ID: "lead-20", CueID: "cue-1", Start: 9.2, Duration: 4},
		{ID: "lead-60", CueID: "cue-1", Start: 7.6, Duration: 4},
	}
	d := mixTestDraft(mixTestLane(schema.TrackRoleHit, -12, 18, clips...))
	issues := issuesWithCode(CheckMix(d, DefaultMixPolicy()), MixIssueHitLead)
	if len(issues) != 2 || issues[0].ClipID != "lead-20" || issues[1].ClipID != "lead-60" {
		t.Fatalf("只有 20%% 与 60%% 的 hit 应报提前量问题：%+v", issues)
	}
}

// hit 没有绑定 cue 时无法核对提前量，必须报出来而不是静默放过。
func TestCheckMixHitWithoutCue(t *testing.T) {
	d := mixTestDraft(mixTestLane(schema.TrackRoleHit, -12, 18, schema.AudioClip{ID: "hit", Start: 8.4, Duration: 4}))
	issues := CheckMix(d, DefaultMixPolicy())
	if len(issues) != 1 || issues[0].Code != MixIssueHitCue {
		t.Fatalf("没有 cue 的 hit 应报一条问题：%+v", issues)
	}
}

// 空角色的旧音轨与 mute 不参与四层校验，未知角色要被指出。
func TestCheckMixSkipsLegacyAndMuteTracks(t *testing.T) {
	d := mixTestDraft(
		schema.AudioLane{ID: "legacy", Name: "旁白", GainDB: 3, Clips: []schema.AudioClip{{ID: "clip", Start: 0, Duration: 1}}},
		schema.AudioLane{ID: "mute", Name: "静音", Role: schema.TrackRoleMute, GainDB: 12, Clips: []schema.AudioClip{{ID: "gap", Start: 0, Duration: 1}}},
	)
	if issues := CheckMix(d, DefaultMixPolicy()); len(issues) != 0 {
		t.Fatalf("旧音轨与 mute 不应被四层规则误报：%+v", issues)
	}

	d.Timeline.Tracks = []schema.AudioLane{{ID: "odd", Name: "怪轨", Role: "reverb", GainDB: -12, Clips: []schema.AudioClip{{ID: "clip", Start: 0, Duration: 1}}}}
	issues := CheckMix(d, DefaultMixPolicy())
	if len(issues) != 1 || issues[0].Code != MixIssueRole {
		t.Fatalf("未知角色应报一条问题：%+v", issues)
	}
}
