package schema

// 音轨角色（ADR-0004）：四层音效加真正的数字静音。空串表示旧数据里的普通音轨，不参与分层校验。
const (
	TrackRoleAmbience = "ambience"
	TrackRoleScore    = "score"
	TrackRoleBed      = "bed"
	TrackRoleHit      = "hit"
	TrackRoleMute     = "mute"
)

// 侧链与 hit 前移的默认值来自 ADR-0004 的实测：效果总线对人声至少压 16 dB；
// hit 的瞬态要出现在词之前，起点比所属 cue 提前自身长度的约 40%，容差 ±10 个百分点。
const (
	MixSidechainMinDB   = 16.0
	MixHitLeadRatio     = 0.40
	MixHitLeadTolerance = 0.10
)

// TrackRolePolicy 是一种音轨角色的默认电平区间与侧链要求；电平区间包含端点。
// LeadRatio 与 LeadTolerance 只对 hit 有意义，其余角色为零。
type TrackRolePolicy struct {
	MinGainDB     float64
	MaxGainDB     float64
	MinDuckDB     float64
	LeadRatio     float64
	LeadTolerance float64
}

// 电平区间分别来自 ADR-0004 的四层表；mute 是数字静音，不参与电平与侧链校验。
var trackRolePolicies = map[string]TrackRolePolicy{
	TrackRoleAmbience: {MinGainDB: -30, MaxGainDB: -25, MinDuckDB: MixSidechainMinDB},
	TrackRoleScore:    {MinGainDB: -28, MaxGainDB: -24, MinDuckDB: MixSidechainMinDB},
	TrackRoleBed:      {MinGainDB: -23, MaxGainDB: -20, MinDuckDB: MixSidechainMinDB},
	TrackRoleHit:      {MinGainDB: -16, MaxGainDB: -8, MinDuckDB: MixSidechainMinDB, LeadRatio: MixHitLeadRatio, LeadTolerance: MixHitLeadTolerance},
	TrackRoleMute:     {},
}

// TrackRolePolicyFor 返回角色的默认策略；空角色是旧数据的普通音轨，未知角色也返回 false。
func TrackRolePolicyFor(role string) (TrackRolePolicy, bool) {
	policy, ok := trackRolePolicies[role]
	return policy, ok
}

// TrackRolePolicies 返回默认角色表的副本，供混音校验按需覆盖。
func TrackRolePolicies() map[string]TrackRolePolicy {
	policies := make(map[string]TrackRolePolicy, len(trackRolePolicies))
	for role, policy := range trackRolePolicies {
		policies[role] = policy
	}
	return policies
}
