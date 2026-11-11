package schema

// 复核角色（ADR-0011）：owner 负责整条流水线，reviewer 只能改归因，listener 只读。
const (
	RoleOwner    = "owner"
	RoleReviewer = "reviewer"
	RoleListener = "listener"
)

// Reviewer 是复核角色表的一员；旧数据没有角色表时为空，读取不报错。
type Reviewer struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Role string `json:"role"`
}

// CanEdit 判断角色能否修改作品字段：owner 全部可改，reviewer 只能改说话人与台词类型，
// listener 与未登记的角色只读。字段名沿用 JSON 名（speakerId、kind、text 等）。
func CanEdit(role, field string) bool {
	switch role {
	case RoleOwner:
		return true
	case RoleReviewer:
		return field == "speakerId" || field == "kind"
	default:
		return false
	}
}
