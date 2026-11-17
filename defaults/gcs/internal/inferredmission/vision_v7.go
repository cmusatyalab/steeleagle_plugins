package inferredmission

import (
	"encoding/json"
	"errors"
	"strings"
)

func (row *section) readVisionRecommendation(version, text string) (task, error) {
	if version == "v6" {
		return parseV6Instruction(text)
	}
	text = strings.TrimSpace(text)
	// Some vision endpoints wrap a JSON reply in a Markdown code fence.
	if body, ok := strings.CutPrefix(text, "```json"); ok && strings.HasSuffix(body, "```") {
		text = strings.TrimSuffix(body, "```")
	}
	var reply struct {
		Recommendation string `json:"recommendation"`
		Evidence       string `json:"evidence"`
	}
	if err := json.Unmarshal([]byte(text), &reply); err != nil {
		return task{}, errors.New("expected a JSON recommendation and observation")
	}
	proposed, err := parseV6Instruction(reply.Recommendation)
	if err != nil {
		return task{}, err
	}
	evidence := strings.Join(strings.Fields(reply.Evidence), " ")
	if evidence == "" || len(evidence) > 1024 {
		return task{}, errors.New("expected a brief supporting observation")
	}
	row.VisionInference = proposed.instructionV6()
	row.VisionEvidence = evidence
	return proposed, nil
}
