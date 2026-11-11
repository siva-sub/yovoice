package assistant

import (
	"encoding/json"
	"os"
	"regexp"
	"strings"
	"testing"
)

// yovoice-product 的错误码一节由 internal/msg 的错误码与界面中文文案生成，保证与界面一致。
// 错误码或文案变化后运行 YOVOICE_UPDATE_SKILLS=1 go test ./internal/assistant 重新生成。
func TestProductSkillErrors(t *testing.T) {
	source, err := os.ReadFile("../msg/msg.go")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile("../../web/src/shared/i18n/catalogs/zh-cn.json")
	if err != nil {
		t.Fatal(err)
	}
	var catalog map[string]struct {
		DefaultMessage string `json:"defaultMessage"`
	}
	if err = json.Unmarshal(raw, &catalog); err != nil {
		t.Fatal(err)
	}
	var lines []string
	for _, match := range regexp.MustCompile(`Err\w+\s+Code = "([^"]+)"`).FindAllStringSubmatch(string(source), -1) {
		entry, ok := catalog[match[1]]
		if !ok {
			t.Fatalf("界面缺少错误码 %s 的文案", match[1])
		}
		lines = append(lines, "- `"+strings.TrimPrefix(match[1], "@yovoice.")+"` "+strings.Join(strings.Fields(entry.DefaultMessage), " "))
	}
	const path = "skills/yovoice-product/SKILL.md"
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	// Windows 检出时可能换成 CRLF，按 LF 比较。
	body = []byte(strings.ReplaceAll(string(body), "\r\n", "\n"))
	section := regexp.MustCompile(`(?s)<!-- errors:begin -->.*<!-- errors:end -->`)
	want := section.ReplaceAllLiteralString(string(body), "<!-- errors:begin -->\n"+strings.Join(lines, "\n")+"\n<!-- errors:end -->")
	if want == string(body) {
		return
	}
	if os.Getenv("YOVOICE_UPDATE_SKILLS") == "1" {
		if err = os.WriteFile(path, []byte(want), 0644); err != nil {
			t.Fatal(err)
		}
		return
	}
	t.Fatal("yovoice-product 的错误码一节已过期，运行 YOVOICE_UPDATE_SKILLS=1 go test ./internal/assistant 重新生成")
}
