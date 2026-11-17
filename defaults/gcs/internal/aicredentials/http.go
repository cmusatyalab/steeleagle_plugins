package aicredentials

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

const (
	api     = "/api/ai/credentials"
	maxBody = 32 << 10
)

type providerStatus struct {
	Configured bool   `json:"configured"`
	Source     string `json:"source"`
}

type credentialStatus struct {
	CanManage bool           `json:"can_manage"`
	OpenAI    providerStatus `json:"openai"`
	Anthropic providerStatus `json:"anthropic"`
}

func (s *Store) status(canManage bool) credentialStatus {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return credentialStatus{
		CanManage: canManage,
		OpenAI:    statusOf(s.saved.OpenAI, s.env.OpenAI),
		Anthropic: statusOf(s.saved.Anthropic, s.env.Anthropic),
	}
}

func statusOf(saved, env string) providerStatus {
	switch {
	case saved != "":
		return providerStatus{Configured: true, Source: "saved"}
	case env != "":
		return providerStatus{Configured: true, Source: "environment"}
	default:
		return providerStatus{Source: "missing"}
	}
}

// Register mounts credential metadata and same-origin loopback management.
func (s *Store) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET "+api, func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, s.status(s.canManage(r, false)))
	})
	mux.HandleFunc("PUT "+api+"/{provider}", s.update)
	mux.HandleFunc("DELETE "+api+"/{provider}", s.update)
}

func (s *Store) update(w http.ResponseWriter, r *http.Request) {
	if !s.canManage(r, true) {
		failure(w, http.StatusForbidden, "Credential changes require a direct, same-origin loopback connection.")
		return
	}
	provider := r.PathValue("provider")
	if provider != openAI && provider != anthropic {
		failure(w, http.StatusNotFound, "Unknown credential provider.")
		return
	}
	key := ""
	if r.Method == http.MethodPut {
		body := struct {
			Key string `json:"key"`
		}{}
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBody))
		decoder.DisallowUnknownFields()
		err := decoder.Decode(&body)
		if err == nil {
			err = decoder.Decode(new(json.RawMessage))
			if err == nil {
				err = errors.New("unexpected trailing JSON")
			} else if errors.Is(err, io.EOF) {
				err = nil
			}
		}
		var oversized *http.MaxBytesError
		if errors.As(err, &oversized) {
			failure(w, http.StatusRequestEntityTooLarge, "Credential request is too large.")
			return
		}
		key = strings.TrimSpace(body.Key)
		if err != nil || !validKey(key) {
			failure(w, http.StatusBadRequest, "Provide a valid key in a JSON object.")
			return
		}
	}
	if err := s.replace(provider, key); err != nil {
		failure(w, http.StatusInternalServerError, "The credential could not be saved.")
		return
	}
	writeJSON(w, http.StatusOK, s.status(true))
}

func (s *Store) canManage(r *http.Request, requireOrigin bool) bool {
	if s.path == "" {
		return false
	}
	remote, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || !net.ParseIP(remote).IsLoopback() {
		return false
	}
	host, err := url.Parse("http://" + r.Host)
	if err != nil || host.Host != r.Host || strings.HasSuffix(r.Host, ":") {
		return false
	}
	if port := host.Port(); port != "" {
		if n, err := strconv.ParseUint(port, 10, 16); err != nil || n == 0 {
			return false
		}
	}
	localhost := strings.EqualFold(host.Hostname(), "localhost") && !strings.HasPrefix(r.Host, "[")
	if !localhost && !net.ParseIP(host.Hostname()).IsLoopback() {
		return false
	}
	for name := range r.Header {
		name = strings.ToLower(name)
		switch name {
		case "forwarded", "via", "x-real-ip", "x-client-ip", "true-client-ip", "cf-connecting-ip", "x-rewrite-url":
			return false
		}
		if strings.HasPrefix(name, "x-forwarded") || strings.HasPrefix(name, "proxy-") ||
			strings.HasPrefix(name, "x-proxy-") || strings.HasPrefix(name, "x-original-") {
			return false
		}
	}
	origins := r.Header.Values("Origin")
	if len(origins) == 0 {
		return !requireOrigin
	}
	scheme := "http://"
	if r.TLS != nil {
		scheme = "https://"
	}
	return len(origins) == 1 && origins[0] == scheme+r.Host
}

func failure(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
