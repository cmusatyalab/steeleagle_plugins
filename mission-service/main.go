// missionservice is a mission launcher: it stays running for the vehicle's
// whole lifetime, handling MissionService calls, while UploadMission stores a
// self-contained mission binary and StartMission execs it as a subprocess. The
// mission binary talks to the drone directly over the client socket it is
// provided.
package main

import (
	"context"
	"flag"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/cmusatyalab/steeleagle/core/util"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"google.golang.org/grpc"

	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
)

func main() {
	logLevel := flag.String("log-level", zerolog.InfoLevel.String(), "log level: trace, debug, info, warn, error, fatal, panic, or disabled")
	preloadPath := flag.String("mission-file", "", "dev/testing only: path to a pre-built mission binary to preload on startup, standing in for a live UploadMission RPC. A StartMission RPC is still required to run it.")
	flag.Parse()

	level, err := zerolog.ParseLevel(*logLevel)
	if err != nil {
		log.Fatal().Msgf("parsing -log-level: %v", err)
	}
	zerolog.SetGlobalLevel(level)

	listenSocket := os.Getenv(util.ListenSockEnv)
	if listenSocket == "" {
		log.Fatal().Msgf("%s not set", util.ListenSockEnv)
	}
	clientSocket := os.Getenv(util.ClientSockEnv)
	if clientSocket == "" {
		log.Fatal().Msgf("%s not set", util.ClientSockEnv)
	}

	// Nest the uploaded mission binary's directory under this plugin's own
	// per-instance runtime directory (filepath.Dir(listenSocket), which
	// core/util.BasePlugin creates before spawning us and removes once we
	// exit) rather than a throwaway scratch directory we'd have to clean up
	// ourselves.
	runDir, err := util.GetPluginDirByName("mission", filepath.Dir(listenSocket))
	if err != nil {
		log.Fatal().Err(err).Msg("creating mission binary directory")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	ln, err := net.Listen("unix", listenSocket)
	if err != nil {
		log.Fatal().Err(err).Str("socket", listenSocket).Msg("listening on plugin socket")
	}

	srv := newServer(clientSocket, runDir)
	grpcServer := grpc.NewServer()
	missionpb.RegisterMissionServiceServer(grpcServer, srv)

	if *preloadPath != "" {
		srv.preloadMission(*preloadPath)
		log.Info().Str("path", *preloadPath).Msg("preloaded mission binary")
	}

	go func() {
		<-ctx.Done()
		log.Info().Msg("shutting down")
		srv.stop(context.Background())
		grpcServer.GracefulStop()
	}()

	log.Info().Str("socket", listenSocket).Msg("missionservice listening")
	if err := grpcServer.Serve(ln); err != nil {
		log.Fatal().Err(err).Msg("serving MissionService")
	}
}
