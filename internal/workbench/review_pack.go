package workbench

import (
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"yovoice/internal/msg"
	"yovoice/internal/schema"
)

// reviewPackVersion 是复核包格式的版本：格式变化（增删字段或改变语义）时递增，导入按版本拒绝不认识的数据。
const reviewPackVersion = 1

// 导入复核包时 Conflict.Kind 的取值：整章正文变化（拒绝）或两边都改过的 cue（提议）。
const (
	ReviewConflictTextSHA  = "textSha"
	ReviewConflictProposal = "proposal"
)

// ReviewPack 是复核包：owner 导出给 reviewer 的文本、证据与闸门指纹，不含音频与音色。
// 覆盖键沿用脚本时代的“章节:段落下标”，reviewer 导出的 overrides 可以直接导入。
type ReviewPack struct {
	Version    int             `json:"version"`
	BookID     string          `json:"bookId"`
	Reviewer   string          `json:"reviewer"`
	ExportedAt string          `json:"exportedAt"`
	Chapters   []ReviewChapter `json:"chapters"`
}

// ReviewChapter 是一章的复核内容；拆成多段的作品在导出时合并成一章，index 跨段连续。
type ReviewChapter struct {
	Chapter  int             `json:"chapter"`
	TextSHA  string          `json:"textSha"`
	Cues     []ReviewCue     `json:"cues"`
	Speakers []ReviewSpeaker `json:"speakers"`
}

// ReviewCue 是一句待复核的台词。Base 是导出时的说话人与类型，导入时作为三方合并的基线：
// 它不参与 textSha 校验（textSha 只覆盖 cue 正文），只用于判断 owner 是否也改过这一句。
type ReviewCue struct {
	Base       *CueAssignment `json:"base"`
	Index      int            `json:"index"`
	ID         string         `json:"id"`
	Text       string         `json:"text"`
	SpeakerID  string         `json:"speakerId"`
	Kind       string         `json:"kind,omitempty"`
	Confidence *float64       `json:"confidence,omitempty"`
	Evidence   string         `json:"evidence,omitempty"`
}

// CueAssignment 是 reviewer 可以改的两个字段：说话人与台词类型。音色锚点、正文与闸门状态都不在复核范围内。
type CueAssignment struct {
	SpeakerID string `json:"speakerId"`
	Kind      string `json:"kind,omitempty"`
}

// ReviewSpeaker 是复核包里的说话人表，只带显示身份，不带音色设置。
type ReviewSpeaker struct {
	ID          string `json:"id"`
	SourceName  string `json:"sourceName"`
	CharacterID string `json:"characterId,omitempty"`
}

// Conflict 是一次导入里没有自动采用的项：
// Kind=textSha 表示整章正文与复核包不一致，整章拒绝；Kind=proposal 表示两边都改过同一 cue，
// 记下 reviewer、时间与前后值，等 owner 逐条接受。
type Conflict struct {
	Kind         string         `json:"kind"`
	Chapter      int            `json:"chapter"`
	Key          string         `json:"key,omitempty"`
	CueID        string         `json:"cueId,omitempty"`
	Reviewer     string         `json:"reviewer,omitempty"`
	At           string         `json:"at,omitempty"`
	From         *CueAssignment `json:"from,omitempty"`
	To           *CueAssignment `json:"to,omitempty"`
	TextSHA      string         `json:"textSha,omitempty"`
	LocalTextSHA string         `json:"localTextSha,omitempty"`
	Reason       string         `json:"reason,omitempty"`
}

// 复核包按章号选择作品：优先用 Draft.Chapter，未设置时回落到标题末尾的数字
// （“第 1061 章”“书名 · 061”都能取到；“第 1061 章（1/2）”先去段后缀再取）。
var (
	reviewPartSuffix    = regexp.MustCompile(`\s*[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]\s*$`)
	reviewChapterNumber = regexp.MustCompile(`(\d+)\s*(?:[章回节卷集部篇幕])?\s*$`)
)

// ExportReviewPack 导出卷内指定章号的复核包；chapters 为空时导出整卷。
// 每章包含正文指纹、逐句文本与证据，以及一份只在导入时使用的合并基线 base。
func (w *Workbench) ExportReviewPack(bookID string, chapters []int, reviewer string) ([]byte, error) {
	state := w.Store.Read()
	groups, err := reviewBookChapters(state, bookID)
	if err != nil {
		return nil, err
	}
	numbers := slices.Clone(chapters)
	if len(numbers) == 0 {
		for chapter := range groups {
			numbers = append(numbers, chapter)
		}
	}
	slices.Sort(numbers)
	numbers = slices.Compact(numbers)
	pack := ReviewPack{Version: reviewPackVersion, BookID: bookID, Reviewer: reviewer, ExportedAt: time.Now().UTC().Format(time.RFC3339), Chapters: make([]ReviewChapter, 0, len(numbers))}
	for _, chapter := range numbers {
		parts, ok := groups[chapter]
		if !ok {
			return nil, fmt.Errorf("卷 %q 没有第 %d 章", bookID, chapter)
		}
		pack.Chapters = append(pack.Chapters, reviewChapterOf(chapter, parts))
	}
	return json.Marshal(pack)
}

// ImportReviewPack 逐章导入 reviewer 改过的复核包。正文指纹不一致的章整章拒绝，不做部分合并；
// 两边都改过的 cue 只作为 proposal 返回，绝不自动覆盖 owner 的值。
// changed 是实际写入的 cue 数。
func (w *Workbench) ImportReviewPack(data []byte, reviewer string) (changed int, conflicts []Conflict, err error) {
	var pack ReviewPack
	if json.Unmarshal(data, &pack) != nil {
		return 0, nil, msg.Err(msg.ErrRequestInvalid, nil)
	}
	if err = reviewPackValid(pack); err != nil {
		return 0, nil, err
	}
	if reviewer == "" {
		reviewer = pack.Reviewer
	}
	at := time.Now().UTC().Format(time.RFC3339)

	w.edit.Lock()
	defer w.edit.Unlock()
	state := w.Store.Read()
	groups, err := reviewBookChapters(state, pack.BookID)
	if err != nil {
		return 0, nil, err
	}

	type cueChange struct {
		draftID  string
		cueIndex int
		value    CueAssignment
	}
	var changes []cueChange
	for _, chapter := range pack.Chapters {
		parts, ok := groups[chapter.Chapter]
		if !ok {
			conflicts = append(conflicts, Conflict{Kind: ReviewConflictTextSHA, Chapter: chapter.Chapter, TextSHA: chapter.TextSHA, Reason: fmt.Sprintf("本地没有第 %d 章", chapter.Chapter)})
			continue
		}
		localSHA := reviewChapterSHA(parts)
		// 先校验包内正文与自带的 textSha 一致，再与本地比对：两个方向任一不一致都整章拒绝。
		if reviewCueSHA(chapter.Cues) != chapter.TextSHA {
			conflicts = append(conflicts, Conflict{Kind: ReviewConflictTextSHA, Chapter: chapter.Chapter, TextSHA: chapter.TextSHA, LocalTextSHA: localSHA, Reason: "复核包内正文与 textSha 不一致"})
			continue
		}
		if localSHA != chapter.TextSHA {
			conflicts = append(conflicts, Conflict{Kind: ReviewConflictTextSHA, Chapter: chapter.Chapter, TextSHA: chapter.TextSHA, LocalTextSHA: localSHA, Reason: "本地正文已变化，整章拒绝"})
			continue
		}
		refs := reviewCueRefs(parts)
		speakerIDs := reviewSpeakerIDs(parts)
		for _, cue := range reviewSortedCues(chapter.Cues) {
			desired := CueAssignment{SpeakerID: cue.SpeakerID, Kind: cue.Kind}
			ref, exists := refs[cue.Index]
			if !exists {
				conflicts = append(conflicts, reviewProposal(chapter.Chapter, cue, reviewer, at, nil, desired, "本地没有这个 cue，无法自动合并"))
				continue
			}
			base := *cue.Base
			local := CueAssignment{SpeakerID: ref.cue.SpeakerID, Kind: ref.cue.Kind}
			// reviewer 没改，或两边改成了同一个值：无需动作，也不能据此覆盖 owner。
			if desired == base || desired == local {
				continue
			}
			if local != base {
				conflicts = append(conflicts, reviewProposal(chapter.Chapter, cue, reviewer, at, &local, desired, "两边都改过，等待逐条接受"))
				continue
			}
			if !speakerIDs[desired.SpeakerID] {
				conflicts = append(conflicts, reviewProposal(chapter.Chapter, cue, reviewer, at, &local, desired, "本地没有说话人 "+desired.SpeakerID))
				continue
			}
			changes = append(changes, cueChange{draftID: ref.draftID, cueIndex: ref.cueIndex, value: desired})
		}
	}
	if len(changes) == 0 {
		return 0, conflicts, nil
	}
	if err = w.Store.Update(func(s *schema.State) {
		for _, change := range changes {
			i := slices.IndexFunc(s.Drafts, func(d schema.Draft) bool { return d.ID == change.draftID })
			if i < 0 || s.Drafts[i].Subtitles == nil || change.cueIndex < 0 || change.cueIndex >= len(s.Drafts[i].Subtitles.Cues) {
				continue
			}
			cue := &s.Drafts[i].Subtitles.Cues[change.cueIndex]
			cue.SpeakerID = change.value.SpeakerID
			cue.Kind = change.value.Kind
			// 人做过决定后机器置信度失去意义（ADR-0002），随这次改派一起清空。
			cue.Confidence = nil
		}
	}, true); err != nil {
		return 0, conflicts, err
	}
	return len(changes), conflicts, nil
}

// reviewBookChapters 把卷内的故事作品按章号分组并排好段序；章号取不到时报出书与标题，不猜也不落到顺序号。
func reviewBookChapters(state schema.State, bookID string) (map[int][]schema.Draft, error) {
	if schema.ValidateBookID(bookID) != nil {
		return nil, fmt.Errorf("卷标识 %q 不合法", bookID)
	}
	i := slices.IndexFunc(state.Books, func(b schema.Book) bool { return b.ID == bookID })
	if i < 0 {
		return nil, fmt.Errorf("找不到卷 %q", bookID)
	}
	book := state.Books[i]
	groups := map[int][]schema.Draft{}
	for _, draft := range state.Drafts {
		if draft.BookID != bookID || draft.Kind != "story" {
			continue
		}
		chapter, err := reviewDraftChapter(draft, book.Title)
		if err != nil {
			return nil, err
		}
		groups[chapter] = append(groups[chapter], draft)
	}
	for chapter := range groups {
		slices.SortStableFunc(groups[chapter], reviewPartCompare)
	}
	return groups, nil
}

// reviewDraftChapter 返回作品的章号：Draft.Chapter 优先，其次标题末尾的数字。
func reviewDraftChapter(draft schema.Draft, bookTitle string) (int, error) {
	if draft.Chapter > 0 {
		return draft.Chapter, nil
	}
	if draft.Chapter < 0 {
		return 0, fmt.Errorf("卷《%s》的作品 %q 的章号 %d 不合法", bookTitle, draft.Title, draft.Chapter)
	}
	title := strings.TrimSpace(reviewPartSuffix.ReplaceAllString(draft.Title, ""))
	match := reviewChapterNumber.FindStringSubmatch(title)
	if match == nil {
		return 0, fmt.Errorf("卷《%s》的作品 %q 取不到章号：标题需以数字结尾（如“第 1061 章”或“书名 · 061”）", bookTitle, draft.Title)
	}
	chapter, err := strconv.Atoi(match[1])
	if err != nil || chapter <= 0 {
		return 0, fmt.Errorf("卷《%s》的作品 %q 的章号 %q 不合法", bookTitle, draft.Title, match[1])
	}
	return chapter, nil
}

// reviewPartCompare 按段序排列同一章的多段作品：标题里带 (1/2) 的按段号，其余按创建时间。
func reviewPartCompare(a, b schema.Draft) int {
	ai, aok := reviewPartIndex(a.Title)
	bi, bok := reviewPartIndex(b.Title)
	if aok && bok && ai != bi {
		if ai < bi {
			return -1
		}
		return 1
	}
	if aok != bok {
		if aok {
			return -1
		}
		return 1
	}
	at, bt := reviewDraftStamp(a), reviewDraftStamp(b)
	if !at.Equal(bt) {
		if at.Before(bt) {
			return -1
		}
		return 1
	}
	return strings.Compare(a.ID, b.ID)
}

// reviewPartIndex 取标题末尾的段号；没有段后缀时返回 false。
func reviewPartIndex(title string) (int, bool) {
	match := reviewPartSuffix.FindStringSubmatch(title)
	if match == nil {
		return 0, false
	}
	part, err := strconv.Atoi(match[1])
	if err != nil || part <= 0 {
		return 0, false
	}
	return part, true
}

func reviewDraftStamp(draft schema.Draft) time.Time {
	if draft.CreatedAt != nil {
		return *draft.CreatedAt
	}
	if draft.UpdatedAt != nil {
		return *draft.UpdatedAt
	}
	return time.Time{}
}

// reviewChapterOf 把一章的各段拼成一个复核章节：cue 下标按段偏移连续编号，正文指纹覆盖整章。
func reviewChapterOf(chapter int, parts []schema.Draft) ReviewChapter {
	out := ReviewChapter{Chapter: chapter, Cues: []ReviewCue{}, Speakers: []ReviewSpeaker{}}
	seen := map[string]bool{}
	offset := 0
	for _, part := range parts {
		cues := reviewCues(part)
		used := map[string]bool{}
		for index, cue := range cues {
			used[cue.SpeakerID] = true
			out.Cues = append(out.Cues, ReviewCue{
				Base:       &CueAssignment{SpeakerID: cue.SpeakerID, Kind: cue.Kind},
				Index:      offset + index,
				ID:         cue.ID,
				Text:       cue.Text,
				SpeakerID:  cue.SpeakerID,
				Kind:       cue.Kind,
				Confidence: reviewConfidence(cue.Confidence),
				Evidence:   cue.Evidence,
			})
		}
		if part.Subtitles != nil {
			for _, speaker := range part.Subtitles.Speakers {
				if !used[speaker.ID] || seen[speaker.ID] {
					continue
				}
				seen[speaker.ID] = true
				out.Speakers = append(out.Speakers, ReviewSpeaker{ID: speaker.ID, SourceName: speaker.SourceName, CharacterID: speaker.CharacterID})
			}
		}
		offset += len(cues)
	}
	out.TextSHA = reviewChapterSHA(parts)
	return out
}

// reviewChapterSHA 与复核包里的 cue 正文使用同一种拼接方式：按段序、段内按 cue 顺序用换行连接。
// 这样 textSha 既能对上旧脚本按整章正文记录的指纹，也能由导入方从 cue 数组独立重算。
func reviewChapterSHA(parts []schema.Draft) string {
	texts := []string{}
	for _, part := range parts {
		for _, cue := range reviewCues(part) {
			texts = append(texts, cue.Text)
		}
	}
	return schema.TextSHA(strings.Join(texts, "\n"))
}

// reviewCueSHA 按 index 顺序拼接包内 cue 正文，用来确认包没有在传输中被改乱。
func reviewCueSHA(cues []ReviewCue) string {
	sorted := reviewSortedCues(cues)
	texts := make([]string, 0, len(sorted))
	for _, cue := range sorted {
		texts = append(texts, cue.Text)
	}
	return schema.TextSHA(strings.Join(texts, "\n"))
}

func reviewSortedCues(cues []ReviewCue) []ReviewCue {
	sorted := slices.Clone(cues)
	slices.SortFunc(sorted, func(a, b ReviewCue) int { return a.Index - b.Index })
	return sorted
}

func reviewCues(draft schema.Draft) []schema.SubtitleCue {
	if draft.Subtitles == nil {
		return nil
	}
	return draft.Subtitles.Cues
}

func reviewConfidence(value *float64) *float64 {
	if value == nil {
		return nil
	}
	copied := *value
	return &copied
}

type reviewCueRef struct {
	draftID  string
	cueIndex int
	cue      schema.SubtitleCue
}

// reviewCueRefs 建立“章内下标 → 具体作品与段内下标”的映射，写入时按段内下标定位。
func reviewCueRefs(parts []schema.Draft) map[int]reviewCueRef {
	refs := map[int]reviewCueRef{}
	offset := 0
	for _, part := range parts {
		cues := reviewCues(part)
		for index, cue := range cues {
			refs[offset+index] = reviewCueRef{draftID: part.ID, cueIndex: index, cue: cue}
		}
		offset += len(cues)
	}
	return refs
}

// reviewSpeakerIDs 收集本地这一章可用的说话人，reviewer 不能凭空引入新身份。
func reviewSpeakerIDs(parts []schema.Draft) map[string]bool {
	ids := map[string]bool{}
	for _, part := range parts {
		if part.Subtitles == nil {
			continue
		}
		for _, speaker := range part.Subtitles.Speakers {
			ids[speaker.ID] = true
		}
		for _, cue := range part.Subtitles.Cues {
			ids[cue.SpeakerID] = true
		}
	}
	return ids
}

func reviewProposal(chapter int, cue ReviewCue, reviewer, at string, from *CueAssignment, to CueAssignment, reason string) Conflict {
	return Conflict{
		Kind:     ReviewConflictProposal,
		Chapter:  chapter,
		Key:      fmt.Sprintf("%d:%d", chapter, cue.Index),
		CueID:    cue.ID,
		Reviewer: reviewer,
		At:       at,
		From:     from,
		To:       &to,
		Reason:   reason,
	}
}

// reviewPackValid 校验复核包的结构：版本、卷标识、章号与 cue 下标不重复，且每句都带合并基线。
func reviewPackValid(pack ReviewPack) error {
	bad := msg.Err(msg.ErrRequestInvalid, nil)
	if pack.Version != reviewPackVersion || schema.ValidateBookID(pack.BookID) != nil || len(pack.Chapters) == 0 {
		return bad
	}
	chapters := map[int]bool{}
	for _, chapter := range pack.Chapters {
		if chapter.Chapter <= 0 || chapters[chapter.Chapter] {
			return bad
		}
		chapters[chapter.Chapter] = true
		indexes := map[int]bool{}
		for _, cue := range chapter.Cues {
			if cue.Index < 0 || indexes[cue.Index] || cue.Base == nil {
				return bad
			}
			indexes[cue.Index] = true
			if cue.Kind != "" && !slices.Contains(schema.CueKinds, cue.Kind) {
				return msg.Err(msg.ErrSubtitleInvalid, nil)
			}
		}
	}
	return nil
}
