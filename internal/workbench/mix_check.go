package workbench

import (
	"fmt"
	"yovoice/internal/schema"
)

// MixIssueCode 是混音校验问题的稳定分类，界面与脚本按它聚合，不解析文案。
type MixIssueCode string

const (
	MixIssueGain      MixIssueCode = "gain"
	MixIssueSidechain MixIssueCode = "sidechain"
	MixIssueHitLead   MixIssueCode = "hit-lead"
	MixIssueHitCue    MixIssueCode = "hit-cue"
	MixIssueRole      MixIssueCode = "role"
)

// MixIssue 是一条可读的混音配置问题。校验只报告、不改正，电平与位置仍由作者决定。
type MixIssue struct {
	Code      MixIssueCode `json:"code"`
	TrackID   string       `json:"trackId"`
	TrackName string       `json:"trackName,omitempty"`
	Role      string       `json:"role,omitempty"`
	ClipID    string       `json:"clipId,omitempty"`
	Message   string       `json:"message"`
}

// MixPolicy 是混音校验使用的角色规则表；零值使用 schema 里 ADR-0004 的默认表。
type MixPolicy struct {
	Roles map[string]schema.TrackRolePolicy
}

// DefaultMixPolicy 返回 ADR-0004 的默认规则表。
func DefaultMixPolicy() MixPolicy {
	return MixPolicy{Roles: schema.TrackRolePolicies()}
}

func (p MixPolicy) rolePolicy(role string) (schema.TrackRolePolicy, bool) {
	if p.Roles != nil {
		policy, ok := p.Roles[role]
		return policy, ok
	}
	return schema.TrackRolePolicyFor(role)
}

// CheckMix 按 ADR-0004 逐轨逐片段核对电平、侧链与 hit 提前量，返回问题列表；没有问题时返回 nil。
// 空角色的普通音轨与 mute 轨不做分层校验；未知角色与缺少 cue 锚点的 hit 片段会被报出来，
// 因为无法确认它们符合四层规则。
func CheckMix(d schema.Draft, policy MixPolicy) []MixIssue {
	if d.Timeline == nil {
		return nil
	}
	var issues []MixIssue
	for _, track := range d.Timeline.Tracks {
		if track.Role == "" || track.Role == schema.TrackRoleMute {
			continue
		}
		rolePolicy, ok := policy.rolePolicy(track.Role)
		if !ok {
			issues = append(issues, trackIssue(track, MixIssueRole, fmt.Sprintf("音轨 %q 的角色 %q 不属于 ambience、score、bed、hit、mute", track.Name, track.Role)))
			continue
		}
		if len(track.Clips) > 0 && track.DuckDB < rolePolicy.MinDuckDB {
			issues = append(issues, trackIssue(track, MixIssueSidechain, fmt.Sprintf("%s 轨 %q 的侧链只有 %.1f dB，至少需要 %.0f dB", track.Role, track.Name, track.DuckDB, rolePolicy.MinDuckDB)))
		}
		for _, clip := range track.Clips {
			gain := track.GainDB + clip.GainDB
			if !schema.InRange(gain, rolePolicy.MinGainDB, rolePolicy.MaxGainDB) {
				issues = append(issues, MixIssue{
					Code:      MixIssueGain,
					TrackID:   track.ID,
					TrackName: track.Name,
					Role:      track.Role,
					ClipID:    clip.ID,
					Message:   fmt.Sprintf("%s 轨 %q 的片段 %q 电平 %.1f dB，应在 %.0f…%.0f dB", track.Role, track.Name, clip.ID, gain, rolePolicy.MinGainDB, rolePolicy.MaxGainDB),
				})
			}
			if track.Role == schema.TrackRoleHit {
				issues = append(issues, checkHitLead(d, track, clip, rolePolicy)...)
			}
		}
	}
	return issues
}

func trackIssue(track schema.AudioLane, code MixIssueCode, message string) MixIssue {
	return MixIssue{Code: code, TrackID: track.ID, TrackName: track.Name, Role: track.Role, Message: message}
}

// checkHitLead 核对 hit 片段相对所属 cue 的提前量：按片段自身长度的 40% 前移，容差 ±10 个百分点。
// 起点已经贴住 0 秒、无法再前移时按章首钳制处理，不再报错（与 check-mix.py 一致）。
func checkHitLead(d schema.Draft, track schema.AudioLane, clip schema.AudioClip, policy schema.TrackRolePolicy) []MixIssue {
	cueStart, ok := cueStartSeconds(d, clip.CueID)
	if !ok {
		return []MixIssue{{
			Code:      MixIssueHitCue,
			TrackID:   track.ID,
			TrackName: track.Name,
			Role:      track.Role,
			ClipID:    clip.ID,
			Message:   fmt.Sprintf("hit 片段 %q 没有绑定有效的 cue，无法核对提前量", clip.ID),
		}}
	}
	if clip.Duration <= 0 {
		return nil
	}
	lead := cueStart - clip.Start
	ratio := lead / clip.Duration
	low, high := policy.LeadRatio-policy.LeadTolerance, policy.LeadRatio+policy.LeadTolerance
	if ratio >= low-1e-9 && ratio <= high+1e-9 {
		return nil
	}
	if clip.Start <= 1e-9 && lead >= 0 && ratio < low {
		return nil
	}
	return []MixIssue{{
		Code:      MixIssueHitLead,
		TrackID:   track.ID,
		TrackName: track.Name,
		Role:      track.Role,
		ClipID:    clip.ID,
		Message:   fmt.Sprintf("hit 片段 %q 提前 %.0f%%，应在 %.0f%%…%.0f%%（自身长度的约 %.0f%%）", clip.ID, ratio*100, low*100, high*100, policy.LeadRatio*100),
	}}
}

// cueStartSeconds 用台词在字幕里的开始时间（毫秒）作为 hit 的锚点；找不到时返回 false。
func cueStartSeconds(d schema.Draft, cueID string) (float64, bool) {
	if d.Subtitles == nil || cueID == "" {
		return 0, false
	}
	for _, cue := range d.Subtitles.Cues {
		if cue.ID == cueID {
			return float64(cue.Start) / 1000, true
		}
	}
	return 0, false
}
