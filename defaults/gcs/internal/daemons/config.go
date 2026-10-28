package daemons

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

// maxConfigBody bounds a request that carries a whole config.
const maxConfigBody = 1 << 20

// configPayload is a candidate config, as a form or as raw TOML, and the
// daemon it is for.
type configPayload struct {
	IP         string          `json:"ip"`
	ConfigHash string          `json:"config_hash"`
	To         string          `json:"to"`
	Form       json.RawMessage `json:"form"`
	RawTOML    *string         `json:"raw_toml"`
}

// candidate is the config text a payload stands for: the raw TOML exactly as
// given, or the form rendered.
func (p configPayload) candidate() ([]byte, error) {
	if p.RawTOML != nil {
		return []byte(*p.RawTOML), nil
	}
	if len(p.Form) == 0 || string(p.Form) == "null" {
		return nil, errors.New("a form or raw_toml is required")
	}
	f, err := DecodeFormJSON(bytes.NewReader(p.Form))
	if err != nil {
		return nil, fmt.Errorf("invalid form: %w", err)
	}
	return RenderForm(f)
}

// checkResult is what a check finds in a candidate config. Blocking is
// whether Apply refuses it: only on the GCS's own daemon, which takes the
// GCS down with it if its services fail.
type checkResult struct {
	Errors   []string `json:"errors"`
	Warnings []string `json:"warnings"`
	Blocking bool     `json:"blocking"`
}

func hashConfig(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func (h *Handler) own(ip string) bool { return ip == h.self.IP }

// readConfig reads the config of the daemon at ip.
func (h *Handler) readConfig(ctx context.Context, ip string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(routed(ctx, ip), readTimeout)
	defer cancel()
	resp, err := h.daemon.GetConfiguration(ctx, &daemonpb.GetConfigurationRequest{})
	if err != nil {
		return nil, err
	}
	return resp.GetConfigToml(), nil
}

// serveGetConfig serves GET /api/daemons/config?ip=: {raw_toml, config_hash,
// form, form_error, own, self_service}. form is null, with form_error, when
// the config can't be shown as a form.
func (h *Handler) serveGetConfig(w http.ResponseWriter, r *http.Request) {
	ip := r.URL.Query().Get("ip")
	if ip == "" {
		writeDetail(w, http.StatusBadRequest, "a daemon ip is required")
		return
	}
	raw, err := h.readConfig(r.Context(), ip)
	if err != nil {
		h.log.Warn().Err(err).Str("daemon", ip).Msg("get daemon config")
		writeRPCError(w, err)
		return
	}
	out := map[string]any{"raw_toml": string(raw), "config_hash": hashConfig(raw), "own": h.own(ip), "self_service": ""}
	if h.own(ip) {
		_, out["self_service"] = fleet.SplitName(h.self.Engine)
	}
	if f, err := ParseForm(raw); err != nil {
		out["form"], out["form_error"] = nil, err.Error()
	} else {
		out["form"] = f
	}
	writeJSON(w, http.StatusOK, out)
}

// serveConvert serves POST /api/daemons/config/convert: {"to": "raw",
// "form"} → {"raw_toml"}; {"to": "form", "raw_toml"} → {"form"}, or 422
// with a TOMLError, or the reason the form can't show it, as the detail.
func (h *Handler) serveConvert(w http.ResponseWriter, r *http.Request) {
	var p configPayload
	if !readJSON(w, r, maxConfigBody, &p) {
		return
	}
	switch p.To {
	case "raw":
		p.RawTOML = nil
		raw, err := p.candidate()
		if err != nil {
			writeDetail(w, http.StatusBadRequest, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"raw_toml": string(raw)})
	case "form":
		if p.RawTOML == nil {
			writeDetail(w, http.StatusBadRequest, "raw_toml is required")
			return
		}
		f, err := ParseForm([]byte(*p.RawTOML))
		var te *TOMLError
		switch {
		case errors.As(err, &te):
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": te})
		case err != nil:
			writeDetail(w, http.StatusUnprocessableEntity, err.Error())
		default:
			writeJSON(w, http.StatusOK, map[string]any{"form": f})
		}
	default:
		writeDetail(w, http.StatusBadRequest, `to must be "raw" or "form"`)
	}
}

// check validates raw as the daemon does, and warns of the packages it
// needs that the daemon at ip doesn't have.
func (h *Handler) check(ctx context.Context, ip string, raw []byte) (checkResult, error) {
	res := checkResult{Errors: []string{}, Warnings: []string{}}
	dc, err := config.ParseDaemonConfigFromBytes(raw)
	if err != nil {
		res.Errors = append(res.Errors, err.Error())
	} else {
		pkgs, err := h.packages(ctx, ip)
		if err != nil {
			return res, err
		}
		if w := MissingPackages(formOf(dc), pkgs); w != nil {
			res.Warnings = w
		}
	}
	res.Blocking = h.own(ip) && (len(res.Errors) > 0 || len(res.Warnings) > 0)
	return res, nil
}

// formOf is the part of a parsed config the package check reads.
func formOf(dc *config.DaemonConfig) Form {
	var f Form
	if dc.Simulator.Package != "" {
		f.Simulator = &SimulatorForm{Package: dc.Simulator.Package}
	}
	for _, s := range dc.Services {
		f.Services = append(f.Services, ServiceForm{Name: s.Name, Type: string(s.Type), Simulate: s.Simulate,
			Plugin: PluginForm{Plugin: s.Plugin.Plugin, Package: s.Plugin.Package}})
	}
	return f
}

// readCandidate reads a check or apply body and its candidate config,
// answering 400 and returning false if it can't.
func readCandidate(w http.ResponseWriter, r *http.Request) (configPayload, []byte, bool) {
	var p configPayload
	if !readJSON(w, r, maxConfigBody, &p) {
		return p, nil, false
	}
	if p.IP == "" {
		writeDetail(w, http.StatusBadRequest, "a daemon ip is required")
		return p, nil, false
	}
	raw, err := p.candidate()
	if err != nil {
		writeDetail(w, http.StatusBadRequest, err.Error())
		return p, nil, false
	}
	return p, raw, true
}

// serveCheck serves POST /api/daemons/config/check {"ip", "form" |
// "raw_toml"}: {errors, warnings, blocking}.
func (h *Handler) serveCheck(w http.ResponseWriter, r *http.Request) {
	p, raw, ok := readCandidate(w, r)
	if !ok {
		return
	}
	res, err := h.check(r.Context(), p.IP, raw)
	if err != nil {
		h.log.Warn().Err(err).Str("daemon", p.IP).Msg("check daemon config")
		writeRPCError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// serveApply serves PUT /api/daemons/config {"ip", "config_hash", "form" |
// "raw_toml"}: {"ok": true} once the daemon has taken the config (it then
// restarts). The config changing since it was read is a 409 conflict (best
// effort: the daemon has no atomic check); a config that fails the check is
// a 422 with its errors, as are its warnings on the GCS's own daemon.
func (h *Handler) serveApply(w http.ResponseWriter, r *http.Request) {
	p, raw, ok := readCandidate(w, r)
	if !ok {
		return
	}
	// Applying runs to the end even if the page goes away
	ctx := context.WithoutCancel(r.Context())
	current, err := h.readConfig(ctx, p.IP)
	if err != nil {
		h.log.Warn().Err(err).Str("daemon", p.IP).Msg("re-read daemon config")
		writeRPCError(w, err)
		return
	}
	if hashConfig(current) != p.ConfigHash {
		writeJSON(w, http.StatusConflict, map[string]any{"conflict": true, "detail": "the config changed since it was loaded"})
		return
	}
	res, err := h.check(ctx, p.IP, raw)
	if err != nil {
		h.log.Warn().Err(err).Str("daemon", p.IP).Msg("check daemon config")
		writeRPCError(w, err)
		return
	}
	switch {
	case len(res.Errors) > 0:
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "the config is invalid: " + strings.Join(res.Errors, "; "), "errors": res.Errors})
		return
	case res.Blocking:
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "refused on the GCS's own daemon: " + strings.Join(res.Warnings, "; "), "errors": res.Warnings})
		return
	}
	cctx, cancel := context.WithTimeout(routed(ctx, p.IP), writeTimeout)
	defer cancel()
	if _, err := h.daemon.Configure(cctx, daemonpb.ConfigureRequest_builder{ConfigToml: string(raw)}.Build()); err != nil {
		h.log.Warn().Err(err).Str("daemon", p.IP).Msg("configure daemon")
		writeRPCError(w, err)
		return
	}
	h.log.Info().Str("daemon", p.IP).Msg("configured daemon, restarting")
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}
