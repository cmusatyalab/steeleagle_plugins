package main

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/cmusatyalab/steeleagle/config"
	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	// restartWindow is how long a mock daemon is gone after Configure or
	// Restart, as a real one is while it restarts.
	restartWindow = 3 * time.Second

	mockVehiclePackage = "github.com/cmusatyalab/steeleagle_plugins//mock/vehicle"
	mockYoloPackage    = "github.com/cmusatyalab/steeleagle_plugins//mock/yolo"
	mockGCSPackage     = "github.com/cmusatyalab/steeleagle_plugins//defaults/gcs"
)

// daemonStore holds each mock daemon's config and packages, by daemon IP.
// The services ListServices lists still come from the topology: a config
// change only makes a daemon restart, or fail if it names a package the
// daemon doesn't have.
type daemonStore struct {
	mu      sync.Mutex
	now     func() time.Time
	gcsIP   string
	deny    map[string]bool
	daemons map[string]*storedDaemon
}

type storedDaemon struct {
	config   []byte
	packages map[string]*storedPackage
	// restartUntil is when the daemon is back from its last restart.
	restartUntil time.Time
}

type storedPackage struct {
	plugins  map[string][]byte
	modified time.Time
}

// newDaemonStore seeds a config and packages for every daemon in top: its
// vehicles, encoder and yolo engine, and the GCS on the GCS's daemon.
func newDaemonStore(top Topology) (*daemonStore, error) {
	s := &daemonStore{now: time.Now, gcsIP: top.GCSIP, deny: top.DenyConfig, daemons: map[string]*storedDaemon{}}
	driver, err := driverConfig()
	if err != nil {
		return nil, err
	}
	encoder, err := engineConfig(encoderService, "filter", []string{rawFrameID}, "")
	if err != nil {
		return nil, err
	}
	yolo, err := engineConfig(yoloService, "detection", []string{rawFrameID}, detectionID)
	if err != nil {
		return nil, err
	}
	gcs, err := engineConfig("gcs", "generic", []string{encodedFrameID, telemetryID}, "")
	if err != nil {
		return nil, err
	}
	vehicles := map[string][]string{top.GCSIP: nil}
	for _, n := range top.Vehicles {
		ip, bare := fleet.SplitName(n)
		vehicles[ip] = append(vehicles[ip], bare)
	}
	for ip, names := range vehicles {
		dc := config.DaemonConfig{Daemon: config.DaemonRoot{Port: 9000, Lcs: 9001, Hostname: "mock-" + ip}}
		pkgs := map[string]*storedPackage{}
		add := func(uri, plugin string, conf []byte) {
			if pkgs[uri] == nil {
				pkgs[uri] = &storedPackage{plugins: map[string][]byte{}, modified: s.now()}
			}
			pkgs[uri].plugins[plugin] = conf
		}
		service := func(name string, t config.ServiceType, auth config.Auth, plugin, uri string) {
			dc.Services = append(dc.Services, config.ServiceField{Name: name, Type: t, Auth: auth,
				Plugin: config.PluginField{Plugin: plugin, Package: uri}})
		}
		if top.EncoderDaemons[ip] {
			add(encoderURI, fleet.EncoderPlugin, encoder)
			service(encoderService, config.ServiceEngine, "", fleet.EncoderPlugin, encoderURI)
		}
		if top.YoloDaemons[ip] {
			add(mockYoloPackage, yoloService, yolo)
			service(yoloService, config.ServiceEngine, "", yoloService, mockYoloPackage)
		}
		if ip == top.GCSIP {
			add(mockGCSPackage, "gcs", gcs)
			service("gcs", config.ServiceEngine, config.AuthAdmin, "gcs", mockGCSPackage)
		}
		sort.Strings(names)
		for _, n := range names {
			add(mockVehiclePackage, "mock_vehicle", driver)
			service(n, config.ServiceVehicle, "", "mock_vehicle", mockVehiclePackage)
		}
		raw, err := dc.ToBytes()
		if err != nil {
			return nil, err
		}
		s.daemons[ip] = &storedDaemon{config: raw, packages: pkgs}
	}
	return s, nil
}

// lookup returns the daemon a call is for, by its route key as the router
// would route it: no key is the GCS's own daemon, an unknown one NotFound.
func (s *daemonStore) lookup(ctx context.Context) (string, *storedDaemon, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	ip := s.gcsIP
	if keys := md.Get(routeHeader); len(keys) > 0 && keys[0] != "" {
		ip = keys[0]
	}
	d, ok := s.daemons[ip]
	if !ok {
		return "", nil, status.Errorf(codes.NotFound, "no route for key %q", ip)
	}
	return ip, d, nil
}

// writable is lookup for a call that changes the daemon, which a daemon in
// DenyConfig refuses as one not run with admin auth would.
func (s *daemonStore) writable(ctx context.Context, method string) (*storedDaemon, error) {
	ip, d, err := s.lookup(ctx)
	if err != nil {
		return nil, err
	}
	if s.deny[ip] {
		return nil, status.Errorf(codes.PermissionDenied,
			"command external/steeleagle_protocol.v1.services.daemon.DaemonService/%s is not allowed in state LOCKED", method)
	}
	return d, nil
}

// down says why the daemon at ip lists no services, or "" if it is up:
// it's restarting, or its config names a package it doesn't have.
func (s *daemonStore) down(ip string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	d, ok := s.daemons[ip]
	if !ok {
		return ""
	}
	if s.now().Before(d.restartUntil) {
		return "restarting"
	}
	dc, err := config.ParseDaemonConfigFromBytes(d.config)
	if err != nil {
		return fmt.Sprintf("daemon failed to start services: %v", err)
	}
	for _, svc := range dc.Services {
		pkg, ok := d.packages[svc.Plugin.Package]
		if !ok {
			return fmt.Sprintf("daemon failed to start services: package %s not installed", svc.Plugin.Package)
		}
		if _, ok := pkg.plugins[svc.Plugin.Plugin]; !ok {
			return fmt.Sprintf("daemon failed to start services: package %s has no plugin %s", svc.Plugin.Package, svc.Plugin.Plugin)
		}
	}
	return ""
}

// GetConfiguration returns the daemon's stored config.
func (d *Daemon) GetConfiguration(ctx context.Context, _ *daemonpb.GetConfigurationRequest) (*daemonpb.GetConfigurationResponse, error) {
	s := d.store
	s.mu.Lock()
	defer s.mu.Unlock()
	_, sd, err := s.lookup(ctx)
	if err != nil {
		return nil, err
	}
	return daemonpb.GetConfigurationResponse_builder{ConfigToml: sd.config}.Build(), nil
}

// Configure validates and stores the config, then restarts the daemon.
func (d *Daemon) Configure(ctx context.Context, req *daemonpb.ConfigureRequest) (*daemonpb.ConfigureResponse, error) {
	s := d.store
	s.mu.Lock()
	defer s.mu.Unlock()
	sd, err := s.writable(ctx, "Configure")
	if err != nil {
		return nil, err
	}
	if _, err := config.ParseDaemonConfigFromBytes([]byte(req.GetConfigToml())); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid config: %v", err)
	}
	sd.config = []byte(req.GetConfigToml())
	sd.restartUntil = s.now().Add(restartWindow)
	d.log.Info().Msg("mock daemon configured, restarting")
	return &daemonpb.ConfigureResponse{}, nil
}

// Restart restarts the daemon.
func (d *Daemon) Restart(ctx context.Context, _ *daemonpb.RestartRequest) (*daemonpb.RestartResponse, error) {
	s := d.store
	s.mu.Lock()
	defer s.mu.Unlock()
	sd, err := s.writable(ctx, "Restart")
	if err != nil {
		return nil, err
	}
	sd.restartUntil = s.now().Add(restartWindow)
	return &daemonpb.RestartResponse{}, nil
}

// ListPackages lists the daemon's packages.
func (d *Daemon) ListPackages(ctx context.Context, _ *daemonpb.ListPackagesRequest) (*daemonpb.ListPackagesResponse, error) {
	s := d.store
	s.mu.Lock()
	defer s.mu.Unlock()
	_, sd, err := s.lookup(ctx)
	if err != nil {
		return nil, err
	}
	var out []*daemonpb.Package
	for uri, p := range sd.packages {
		out = append(out, daemonpb.Package_builder{Uri: uri, Plugins: p.plugins, Modified: timestamppb.New(p.modified)}.Build())
	}
	return daemonpb.ListPackagesResponse_builder{Packages: out}.Build(), nil
}

// InstallPackage adds a package holding one engine plugin, "plugin". Unlike
// the daemon's, it takes effect at once.
func (d *Daemon) InstallPackage(ctx context.Context, req *daemonpb.InstallPackageRequest) (*daemonpb.InstallPackageResponse, error) {
	s := d.store
	s.mu.Lock()
	defer s.mu.Unlock()
	sd, err := s.writable(ctx, "InstallPackage")
	if err != nil {
		return nil, err
	}
	if req.GetUri() == "" {
		return nil, status.Error(codes.InvalidArgument, "no package uri")
	}
	conf, err := engineConfig("plugin", "generic", []string{rawFrameID}, "")
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	sd.packages[req.GetUri()] = &storedPackage{plugins: map[string][]byte{"plugin": conf}, modified: s.now()}
	return &daemonpb.InstallPackageResponse{}, nil
}

// RemovePackage removes a package, NotFound if it isn't installed.
func (d *Daemon) RemovePackage(ctx context.Context, req *daemonpb.RemovePackageRequest) (*daemonpb.RemovePackageResponse, error) {
	s := d.store
	s.mu.Lock()
	defer s.mu.Unlock()
	sd, err := s.writable(ctx, "RemovePackage")
	if err != nil {
		return nil, err
	}
	if _, ok := sd.packages[req.GetUri()]; !ok {
		return nil, status.Errorf(codes.NotFound, "no package %s", req.GetUri())
	}
	delete(sd.packages, req.GetUri())
	return &daemonpb.RemovePackageResponse{}, nil
}
