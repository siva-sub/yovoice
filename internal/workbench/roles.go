package workbench

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"yovoice/internal/msg"
	"yovoice/internal/schema"
)

// ApplyReviewPack 在导入复核包之前按角色检查字段权限（ADR-0011）：owner 可改全部字段，
// reviewer 只能改 speakerId 与 kind，listener 只读。任一越权字段都会整包拒绝，并在错误里
// 列出章节、cue 与字段名，不做静默丢弃，也不写回部分改动；通过检查后才交给 ImportReviewPack。
func (w *Workbench) ApplyReviewPack(data []byte, reviewer schema.Reviewer) (int, []Conflict, error) {
	var pack ReviewPack
	if err := json.Unmarshal(data, &pack); err != nil {
		return 0, nil, msg.Err(msg.ErrRequestInvalid, nil)
	}
	if err := reviewPackValid(pack); err != nil {
		return 0, nil, err
	}
	role := reviewer.Role
	if role == "" {
		if stored, ok := w.reviewerRole(reviewer.ID); ok {
			role = stored
		}
	}
	state := w.Store.Read()
	groups, err := reviewBookChapters(state, pack.BookID)
	if err != nil {
		return 0, nil, err
	}
	if violations := reviewPermissionViolations(pack, groups, role); len(violations) > 0 {
		return 0, nil, fmt.Errorf("复核角色 %s 越权：%s；已拒绝整包，未写入任何改动", reviewerRoleLabel(role), strings.Join(violations, "；"))
	}
	return w.ImportReviewPack(data, reviewerAuditName(reviewer))
}

// reviewerRole 从角色表里查已登记的角色；未登记时返回 false，调用方按只读处理。
func (w *Workbench) reviewerRole(id string) (string, bool) {
	if id == "" {
		return "", false
	}
	reviewers := w.Store.Read().Reviewers
	i := slices.IndexFunc(reviewers, func(r schema.Reviewer) bool { return r.ID == id })
	if i < 0 {
		return "", false
	}
	return reviewers[i].Role, true
}

// reviewPermissionViolations 比较复核包与本地 cue，列出角色无权修改的字段。
// base 是导出时的基线，只用于 ImportReviewPack 的三方合并；权限看的是包里的当前值。
func reviewPermissionViolations(pack ReviewPack, groups map[int][]schema.Draft, role string) []string {
	var violations []string
	for _, chapter := range pack.Chapters {
		parts, ok := groups[chapter.Chapter]
		if !ok {
			continue
		}
		refs := reviewCueRefs(parts)
		for _, cue := range reviewSortedCues(chapter.Cues) {
			ref, ok := refs[cue.Index]
			if !ok {
				continue
			}
			for _, field := range reviewCueFieldChanges(ref.cue, cue) {
				if !schema.CanEdit(role, field) {
					violations = append(violations, fmt.Sprintf("第 %d 章第 %d 条 cue 的 %s 字段", chapter.Chapter, cue.Index, field))
				}
			}
		}
	}
	return violations
}

// reviewCueFieldChanges 返回复核包相对本地 cue 改动的字段，字段名与 JSON 一致。
func reviewCueFieldChanges(local schema.SubtitleCue, remote ReviewCue) []string {
	var fields []string
	if remote.Text != local.Text {
		fields = append(fields, "text")
	}
	if remote.SpeakerID != local.SpeakerID {
		fields = append(fields, "speakerId")
	}
	if remote.Kind != local.Kind {
		fields = append(fields, "kind")
	}
	if !reviewSameConfidence(remote.Confidence, local.Confidence) {
		fields = append(fields, "confidence")
	}
	if remote.Evidence != local.Evidence {
		fields = append(fields, "evidence")
	}
	return fields
}

func reviewSameConfidence(a, b *float64) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

// reviewerAuditName 用姓名记审计轨迹；没有姓名时退回 ID，让导入方仍有可追溯的复核人。
func reviewerAuditName(reviewer schema.Reviewer) string {
	if reviewer.Name != "" {
		return reviewer.Name
	}
	return reviewer.ID
}

func reviewerRoleLabel(role string) string {
	if role == "" {
		return "未登记"
	}
	return role
}
