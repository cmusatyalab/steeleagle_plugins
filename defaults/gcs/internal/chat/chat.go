// Package chat serves the Plan page's AI Assistant.
package chat

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/mission"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/nl2dsl"
)

// Credentials reports whether the GCS has a key for each provider.
type Credentials struct {
	OpenAI    bool `json:"openai_configured"`
	Anthropic bool `json:"anthropic_configured"`
}

type Handler struct {
	provider    nl2dsl.Provider
	generate    nl2dsl.Generator
	active      chan struct{}
	credentials func() Credentials
}

func New(provider nl2dsl.Provider, generate nl2dsl.Generator, credentials func() Credentials) *Handler {
	return &Handler{provider: provider, generate: generate, active: make(chan struct{}, 4), credentials: credentials}
}

func (h *Handler) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/chat/status", h.status)
	mux.HandleFunc("POST /api/chat", h.chat)
}

func (h *Handler) status(w http.ResponseWriter, _ *http.Request) {
	label := map[string]string{"openai": "ChatGPT", "anthropic": "Claude"}[h.provider.Name]
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]any{
		"configured": h.provider.Configured(), "provider": h.provider.Name,
		"model": h.provider.ModelID, "label": label,
		"reasoning_effort": h.provider.Reasoning, "temperature": h.provider.Temperature, "credentials": h.credentials(),
	})
}

type request struct {
	Messages []nl2dsl.Message        `json:"messages"`
	Draft    string                  `json:"draft_dsl"`
	Plan     mission.PlanningContext `json:"planning_context"`
}

type event struct {
	name string
	data any
}

const chatRules = `For a draft, apply explicit user values first and preserve values in the prior draft unless the user requests a change.
Use the Chat preset of 20 m relative to takeoff for an unspecified takeoff or patrol altitude, where that action supports it. State when this default was applied.
An unspecified patrol area is named PatrolZone. Identify a missing matching Map feature in the summary.
Preserve requested completion and conditional transitions. Add an explicit completion transition when the user names the next action after the patrol.
Return a short explanation without a JSON preview.`

func (h *Handler) chat(w http.ResponseWriter, r *http.Request) {
	if !h.provider.Configured() {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"detail": "Configure an API key for the selected chat model."})
		return
	}
	var req request
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20))
	if err := dec.Decode(&req); err != nil || len(req.Draft) > 24_000 || len(req.Messages) > 128 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid chat request. Use a shorter conversation or start a new one."})
		return
	}
	messages := make([]nl2dsl.Message, 0, len(req.Messages))
	for _, message := range req.Messages {
		if (message.Role == "user" || message.Role == "assistant") && strings.TrimSpace(message.Content) != "" {
			messages = append(messages, message)
		}
	}
	if len(messages) == 0 || messages[len(messages)-1].Role != "user" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Send a user message to start a chat turn."})
		return
	}
	select {
	case h.active <- struct{}{}:
	default:
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"detail": "The GCS is handling too many chat requests. Try again shortly."})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
	defer cancel()
	events := make(chan event, 4)
	send := func(name string, data any) {
		select {
		case events <- event{name, data}:
		case <-ctx.Done():
		}
	}
	go func() {
		defer func() { <-h.active }()
		result, err := h.generate.Generate(ctx, nl2dsl.Options{
			Messages: messages, PriorDSL: req.Draft, Plan: req.Plan, Rules: chatRules,
			Progress: func(detail string) { send("status", map[string]string{"detail": detail}) },
		})
		if err != nil {
			send("error", map[string]string{"message": err.Error()})
			return
		}
		send("done", turnResult(result, req.Draft))
	}()

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	flush := http.NewResponseController(w).Flush
	if err := flush(); err != nil {
		return
	}
	keepalive := time.NewTicker(10 * time.Second)
	defer keepalive.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ctx.Done():
			writeEvent(w, "error", map[string]string{"message": "The chat request timed out."})
			return
		case <-keepalive.C:
			if _, err := io.WriteString(w, ": keepalive\n\n"); err != nil || flush() != nil {
				return
			}
		case item := <-events:
			if err := writeEvent(w, item.name, item.data); err != nil || flush() != nil || item.name != "status" {
				return
			}
		}
	}
}

const markdownFence = "\x60\x60\x60"

func turnResult(result nl2dsl.Result, prior string) map[string]any {
	text := result.Text
	artifacts := []any{}
	var draft any
	if prior != "" {
		draft = map[string]string{"normalized_dsl": prior}
	}
	if result.Draft.Status == "compiled" || result.Draft.Status == "needs_context" {
		if text == "" {
			text = "Here is the V4 mission draft."
		}
		if result.Draft.Status == "needs_context" {
			text += "\n\nThis draft needs planning context before it can be compiled."
		}
		text += "\n\n" + markdownFence + "dsl\n" + strings.TrimRight(result.Draft.DSL, "\n") + "\n" + markdownFence
		if strings.TrimSpace(result.Draft.DSL) != strings.TrimSpace(prior) {
			draft = map[string]string{"normalized_dsl": result.Draft.DSL}
			artifacts = append(artifacts, map[string]any{
				"id": "mission-draft", "type": "mission-draft", "target": "fsm-builder",
				"label": "Apply draft to Mission Builder", "payload": map[string]any{
					"normalized_dsl": result.Draft.DSL, "nodes": result.Draft.Graph.Nodes,
				},
			})
		}
	} else if result.Draft.Status != "" {
		text = "I couldn't produce a verified V4 draft."
	}
	if len(result.Draft.Errors) > 0 {
		text += "\n\n" + strings.Join(result.Draft.Errors, "\n")
	}
	if text == "" {
		text = "The model did not return a reply."
	}
	return map[string]any{"content": text, "artifacts": artifacts, "draft": draft}
}

func writeEvent(w io.Writer, name string, data any) error {
	encoded, err := json.Marshal(data)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", name, encoded)
	return err
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
