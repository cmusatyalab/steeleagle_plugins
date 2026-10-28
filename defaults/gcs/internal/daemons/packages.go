package daemons

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle/config"
)

// Package is a package installed on a daemon, with the plugins it holds.
type Package struct {
	URI      string    `json:"uri"`
	Modified time.Time `json:"modified"`
	Plugins  []Plugin  `json:"plugins"`
}

// Plugin is a plugin in a package, with the args its plugin TOML declares.
// Error is why its TOML couldn't be read, in which case it has no args.
type Plugin struct {
	Name        string `json:"name"`
	Type        string `json:"type"`
	Description string `json:"description"`
	Args        []Arg  `json:"args"`
	Error       string `json:"error,omitempty"`
}

// Arg is an argument a plugin declares (config.ArgsField, with JSON names).
type Arg struct {
	Name        string `json:"name"`
	Type        string `json:"type"`
	Description string `json:"description"`
	Default     any    `json:"default"`
}

// Catalog is a daemon's installed packages, sorted by URI, each with its
// plugins sorted by name.
func Catalog(resp *daemonpb.ListPackagesResponse) []Package {
	out := []Package{}
	for _, p := range resp.GetPackages() {
		pkg := Package{URI: p.GetUri(), Plugins: []Plugin{}}
		if p.GetModified() != nil {
			pkg.Modified = p.GetModified().AsTime()
		}
		for name, raw := range p.GetPlugins() {
			pkg.Plugins = append(pkg.Plugins, plugin(name, raw))
		}
		sort.Slice(pkg.Plugins, func(i, j int) bool { return pkg.Plugins[i].Name < pkg.Plugins[j].Name })
		out = append(out, pkg)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].URI < out[j].URI })
	return out
}

func plugin(name string, raw []byte) Plugin {
	pc, err := config.ParsePluginConfigFromBytes(raw)
	if err != nil {
		return Plugin{Name: name, Error: err.Error()}
	}
	p := Plugin{Name: name, Type: pc.Plugin.Type, Description: pc.Plugin.Description, Args: []Arg{}}
	for _, a := range pc.Args {
		p.Args = append(p.Args, Arg{Name: a.Name, Type: a.Type, Description: a.Description, Default: a.Default})
	}
	return p
}

// MissingPackages warns of each service (and the simulator) in f whose
// package isn't installed, or whose package has no such plugin. The daemon
// starts none of its services if one can't be set up.
func MissingPackages(f Form, pkgs []Package) []string {
	plugins := map[string]map[string]bool{}
	for _, p := range pkgs {
		names := map[string]bool{}
		for _, pl := range p.Plugins {
			names[pl.Name] = true
		}
		plugins[p.URI] = names
	}
	var out []string
	if f.Simulator != nil && f.Simulator.Package != "" && plugins[f.Simulator.Package] == nil {
		out = append(out, fmt.Sprintf("the simulator uses package %s, which isn't installed on this daemon", f.Simulator.Package))
	}
	for _, s := range f.Services {
		pkg := s.Plugin.Package
		if pkg == "" {
			continue
		}
		names, ok := plugins[pkg]
		switch {
		case !ok:
			out = append(out, fmt.Sprintf("service %q uses package %s, which isn't installed on this daemon", s.Name, pkg))
		case s.Plugin.Plugin != "" && !names[s.Plugin.Plugin]:
			out = append(out, fmt.Sprintf("package %s has no plugin %q (service %q)", pkg, s.Plugin.Plugin, s.Name))
		}
	}
	return out
}

// servePackages serves GET /api/daemons/packages?ip=: {"packages": [...]}.
func (h *Handler) servePackages(w http.ResponseWriter, r *http.Request) {
	ip := r.URL.Query().Get("ip")
	if ip == "" {
		writeDetail(w, http.StatusBadRequest, "a daemon ip is required")
		return
	}
	pkgs, err := h.packages(r.Context(), ip)
	if err != nil {
		h.log.Warn().Err(err).Str("daemon", ip).Msg("list packages")
		writeRPCError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"packages": pkgs})
}

// packages lists the packages installed on the daemon at ip.
func (h *Handler) packages(ctx context.Context, ip string) ([]Package, error) {
	ctx, cancel := context.WithTimeout(routed(ctx, ip), readTimeout)
	defer cancel()
	resp, err := h.daemon.ListPackages(ctx, &daemonpb.ListPackagesRequest{})
	if err != nil {
		return nil, err
	}
	return Catalog(resp), nil
}

// packageRequest is the body of an install or a remove.
type packageRequest struct {
	IP  string `json:"ip"`
	URI string `json:"uri"`
}

// readPackageRequest reads an install or remove body, answering 400 and
// returning false if it lacks an ip or a uri.
func readPackageRequest(w http.ResponseWriter, r *http.Request) (packageRequest, bool) {
	var req packageRequest
	if !readJSON(w, r, maxBody, &req) {
		return req, false
	}
	req.URI = strings.TrimSpace(req.URI)
	if req.IP == "" || req.URI == "" {
		writeDetail(w, http.StatusBadRequest, "a daemon ip and a package uri are required")
		return req, false
	}
	return req, true
}

// serveInstallPackage serves POST /api/daemons/packages/install {"ip",
// "uri"}: {"ok": true}. The package takes effect when the daemon next
// restarts.
func (h *Handler) serveInstallPackage(w http.ResponseWriter, r *http.Request) {
	req, ok := readPackageRequest(w, r)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(routed(context.WithoutCancel(r.Context()), req.IP), installTimeout)
	defer cancel()
	if _, err := h.daemon.InstallPackage(ctx, daemonpb.InstallPackageRequest_builder{Uri: req.URI}.Build()); err != nil {
		h.log.Warn().Err(err).Str("daemon", req.IP).Str("package", req.URI).Msg("install package")
		writeRPCError(w, err)
		return
	}
	h.log.Info().Str("daemon", req.IP).Str("package", req.URI).Msg("installed package")
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// serveRemovePackage serves POST /api/daemons/packages/remove {"ip", "uri"}:
// {"ok": true}. The removal takes effect when the daemon next restarts.
func (h *Handler) serveRemovePackage(w http.ResponseWriter, r *http.Request) {
	req, ok := readPackageRequest(w, r)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(routed(context.WithoutCancel(r.Context()), req.IP), writeTimeout)
	defer cancel()
	if _, err := h.daemon.RemovePackage(ctx, daemonpb.RemovePackageRequest_builder{Uri: req.URI}.Build()); err != nil {
		h.log.Warn().Err(err).Str("daemon", req.IP).Str("package", req.URI).Msg("remove package")
		writeRPCError(w, err)
		return
	}
	h.log.Info().Str("daemon", req.IP).Str("package", req.URI).Msg("removed package")
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}
