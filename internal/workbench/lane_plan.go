package workbench

import (
	"cmp"
	"encoding/json"
	"maps"
	"slices"
)

// RenderCue 是一条待渲染 cue 的身份与缓存字段。模型、锚点、文本、演绎指令、CFG、种子与说话人
// 共同决定缓存键；模型与会话参数决定它属于哪条车道。
type RenderCue struct {
	DraftID   string
	CueID     string
	SpeakerID string
	// SpeakerName 只用于版本记录与日志，不参与缓存键。
	SpeakerName string
	ModelID     string
	AnchorRev   string
	Text        string
	Instruction string
	CFG         float64
	Seed        *int
	// SessionOptions 是启动引擎时随模型传入的会话参数；与 ModelID 一起构成车道键。
	SessionOptions map[string]string
}

// RenderLane 是同一引擎键下的一组 cue：模型与会话参数相同，连续渲染不会重启引擎。
type RenderLane struct {
	ModelID        string
	SessionOptions map[string]string
	Cues           []RenderCue
}

// PlanLanes 把一批 cue 按 (modelId, sessionOptions) 分组排序，返回有序计划。
// 引擎启动键一变就要停掉再起一个（ADR-0003），冷启动慢一个数量级，所以同一键的 cue 必须连续；
// 组内保持输入顺序，组间按引擎键排序，输出对同一输入稳定。会话参数按键排序后参与分组，
// 参数顺序不同不会拆成两条车道。
func PlanLanes(cues []RenderCue) []RenderLane {
	lanes := make([]RenderLane, 0)
	position := map[string]int{}
	for _, cue := range cues {
		key := laneKey(cue.ModelID, cue.SessionOptions)
		i, ok := position[key]
		if !ok {
			i = len(lanes)
			position[key] = i
			lanes = append(lanes, RenderLane{ModelID: cue.ModelID, SessionOptions: maps.Clone(cue.SessionOptions)})
		}
		lanes[i].Cues = append(lanes[i].Cues, cue)
	}
	slices.SortFunc(lanes, func(a, b RenderLane) int {
		return cmp.Compare(laneKey(a.ModelID, a.SessionOptions), laneKey(b.ModelID, b.SessionOptions))
	})
	return lanes
}

// FlattenLanes 按分组后的顺序展开全部 cue，供批量渲染逐条执行。
func FlattenLanes(lanes []RenderLane) []RenderCue {
	total := 0
	for _, lane := range lanes {
		total += len(lane.Cues)
	}
	cues := make([]RenderCue, 0, total)
	for _, lane := range lanes {
		cues = append(cues, lane.Cues...)
	}
	return cues
}

// laneKey 是引擎键里与分组相关的部分：模型加规范化的会话参数。
// json.Marshal 对 map 按键排序，所以 nil 与空参数得到同一个键。
func laneKey(modelID string, options map[string]string) string {
	if len(options) == 0 {
		return modelID + "|{}"
	}
	b, _ := json.Marshal(options)
	return modelID + "|" + string(b)
}
