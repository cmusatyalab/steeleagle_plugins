package nl2dsl

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type Image struct {
	Time float64
	JPEG []byte
}

type Reply struct {
	Text string
	DSL  string
}

type Request struct {
	Instructions string
	Messages     []Message
	Images       []Image
	AllowDraft   bool
}

type Model interface {
	Complete(context.Context, Request) (Reply, error)
}

type Provider struct {
	Name      string
	ModelID   string
	BaseURL   string
	Reasoning string
	Key       string
	Client    *http.Client
}

func (p Provider) Configured() bool {
	return p.Key != "" && p.ModelID != "" && (p.Name == "openai" || p.Name == "anthropic")
}

func (p Provider) Complete(ctx context.Context, req Request) (Reply, error) {
	if !p.Configured() {
		return Reply{}, errors.New("Configure an API key for the selected model provider.")
	}
	endpoint, err := p.endpoint()
	if err != nil {
		return Reply{}, err
	}
	payload := p.openAIRequest(req)
	if p.Name == "anthropic" {
		payload = p.anthropicRequest(req)
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return Reply{}, errors.New("Could not encode model request.")
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(data))
	if err != nil {
		return Reply{}, errors.New("Invalid model endpoint.")
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if p.Name == "anthropic" {
		httpReq.Header.Set("x-api-key", p.Key)
		httpReq.Header.Set("anthropic-version", "2023-06-01")
	} else {
		httpReq.Header.Set("Authorization", "Bearer "+p.Key)
	}
	client := p.Client
	if client == nil {
		client = &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	}
	res, err := client.Do(httpReq)
	if err != nil {
		if ctx.Err() != nil {
			return Reply{}, ctx.Err()
		}
		return Reply{}, errors.New("Could not reach the selected model provider.")
	}
	defer res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return Reply{}, fmt.Errorf("The model provider returned HTTP %d. Check model access and credentials.", res.StatusCode)
	}
	var response modelResponse
	if err := json.NewDecoder(io.LimitReader(res.Body, 4<<20)).Decode(&response); err != nil {
		return Reply{}, errors.New("The model provider returned an unreadable response.")
	}
	if response.Status == "incomplete" || response.Status == "failed" || response.StopReason == "max_tokens" {
		return Reply{}, errors.New("The model did not finish its response.")
	}
	reply := response.reply(req.AllowDraft)
	if reply.Text == "" && reply.DSL == "" {
		return Reply{}, errors.New("The model returned no answer.")
	}
	return reply, nil
}

func (p Provider) endpoint() (string, error) {
	base, suffix := p.BaseURL, "/responses"
	if base == "" {
		base = "https://api.openai.com/v1"
		if p.Name == "anthropic" {
			base = "https://api.anthropic.com/v1"
		}
	}
	if p.Name == "anthropic" {
		suffix = "/messages"
	}
	u, err := url.Parse(base)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return "", errors.New("Invalid model base URL.")
	}
	host := u.Hostname()
	if u.Scheme != "https" && !(u.Scheme == "http" && (host == "localhost" || host == "127.0.0.1" || host == "::1")) {
		return "", errors.New("Use HTTPS for a remote model endpoint.")
	}
	return strings.TrimRight(u.String(), "/") + suffix, nil
}

const draftTool = "submit_mission_dsl"

var draftParameters = map[string]any{
	"type": "object",
	"properties": map[string]any{
		"dsl":     map[string]string{"type": "string", "description": "Complete V4 SteelEagle DSL."},
		"summary": map[string]string{"type": "string", "description": "Short explanation of the draft, defaults and caveats."},
	},
	"required": []string{"dsl", "summary"}, "additionalProperties": false,
}

func (p Provider) openAIRequest(req Request) map[string]any {
	input := make([]any, 0, len(req.Messages)+1)
	for _, message := range req.Messages {
		input = append(input, message)
	}
	if len(req.Images) > 0 {
		content := make([]any, 0, 2*len(req.Images))
		for _, img := range req.Images {
			content = append(content,
				map[string]string{"type": "input_text", "text": fmt.Sprintf("Frame at %.3f seconds from section start", img.Time)},
				map[string]string{"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(img.JPEG)})
		}
		input = append(input, map[string]any{"role": "user", "content": content})
	}
	body := map[string]any{"model": p.ModelID, "instructions": req.Instructions, "input": input, "store": false, "max_output_tokens": 8192}
	if p.Reasoning != "" && !strings.HasPrefix(p.ModelID, "gpt-4") && !strings.HasPrefix(p.ModelID, "gpt-3.5") {
		body["reasoning"] = map[string]string{"effort": p.Reasoning}
	}
	if req.AllowDraft {
		body["tools"] = []any{map[string]any{
			"type": "function", "name": draftTool, "description": "Submit a V4 DSL candidate for local validation.",
			"parameters": draftParameters, "strict": true,
		}}
		body["parallel_tool_calls"] = false
	}
	return body
}

func (p Provider) anthropicRequest(req Request) map[string]any {
	body := map[string]any{"model": p.ModelID, "system": req.Instructions, "messages": req.Messages, "max_tokens": 4096}
	if req.AllowDraft {
		body["tools"] = []any{map[string]any{
			"name": draftTool, "description": "Submit a V4 DSL candidate for local validation.", "input_schema": draftParameters,
		}}
	}
	return body
}

type modelBlock struct {
	Type      string          `json:"type"`
	Name      string          `json:"name"`
	Text      string          `json:"text"`
	Arguments string          `json:"arguments"`
	Input     json.RawMessage `json:"input"`
}

type modelResponse struct {
	Status     string       `json:"status"`
	StopReason string       `json:"stop_reason"`
	Content    []modelBlock `json:"content"`
	Output     []struct {
		modelBlock
		Content []modelBlock `json:"content"`
	} `json:"output"`
}

func (r modelResponse) reply(allowDraft bool) Reply {
	var reply Reply
	var text []string
	var summary string
	read := func(b modelBlock) {
		switch b.Type {
		case "output_text", "text", "refusal":
			text = append(text, b.Text)
		case "function_call", "tool_use":
			if !allowDraft || b.Name != draftTool || reply.DSL != "" {
				return
			}
			input := []byte(b.Arguments)
			if b.Type == "tool_use" {
				input = b.Input
			}
			var args struct {
				DSL     string `json:"dsl"`
				Summary string `json:"summary"`
			}
			if json.Unmarshal(input, &args) == nil {
				reply.DSL, summary = strings.TrimSpace(args.DSL), args.Summary
			}
		}
	}
	for _, b := range r.Content {
		read(b)
	}
	for _, item := range r.Output {
		read(item.modelBlock)
		for _, b := range item.Content {
			read(b)
		}
	}
	reply.Text = strings.TrimSpace(strings.Join(text, "\n\n"))
	if reply.Text == "" {
		reply.Text = summary
	}
	return reply
}
