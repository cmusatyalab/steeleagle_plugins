package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
	"syscall"
	"time"

	"github.com/cmusatyalab/steeleagle/core/util"
	"github.com/rs/zerolog/log"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
)

// stopTimeout bounds how long StopMission and shutdown wait for the mission
// subprocess to exit after it's been signaled.
const stopTimeout = 10 * time.Second

// binaryName is the filename the uploaded mission binary is stored under in
// runDir.
const binaryName = "mission"

// tmpBinaryName is the filename an in-flight upload is streamed into before
// being atomically renamed to binaryName.
const tmpBinaryName = "mission.tmp"

// maxMissionSize bounds an uploaded mission binary.
const maxMissionSize = 128 << 20

// server implements MissionServiceServer, launching an uploaded mission binary
// as a subprocess and tracking its lifecycle.
type server struct {
	missionpb.UnimplementedMissionServiceServer

	clientSocket string // socket the mission subprocess dials to reach control/data services
	runDir       string // directory the uploaded mission binary is stored in

	mu         sync.Mutex
	binaryPath string             // set once UploadMission has stored a binary
	uploading  bool               // true while an UploadMission stream is being received
	cancel     context.CancelFunc // cancels the running mission's context; nil if nothing is running
	done       chan struct{}      // closed once the running mission's subprocess exits; nil if nothing is running
}

func newServer(clientSocket, runDir string) *server {
	return &server{clientSocket: clientSocket, runDir: runDir}
}

// preloadMission sets path as the mission binary to run on the next
// StartMission call, standing in for a live UploadMission RPC. Dev/testing
// only: unlike UploadMission, it doesn't copy path into runDir, so path must
// keep existing for as long as the mission might be (re)started.
func (s *server) preloadMission(path string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.binaryPath = path
}

// GetMissionInfo reports the architecture this mission service runs on, so an
// uploader can send the matching binary variant.
func (s *server) GetMissionInfo(ctx context.Context, req *missionpb.GetMissionInfoRequest) (*missionpb.GetMissionInfoResponse, error) {
	return missionpb.GetMissionInfoResponse_builder{Arch: runtime.GOARCH}.Build(), nil
}

// UploadMission receives a streamed mission binary (header, then chunks),
// verifies it, and atomically replaces any previously uploaded one. A failed
// or aborted upload never disturbs the previous binary. Fails if a mission is
// running or another upload is already in flight.
func (s *server) UploadMission(stream grpc.ClientStreamingServer[missionpb.UploadMissionRequest, missionpb.UploadMissionResponse]) error {
	first, err := stream.Recv()
	if err == io.EOF {
		return status.Error(codes.InvalidArgument, "upload stream was empty; the first message must be a header")
	}
	if err != nil {
		return err
	}
	if !first.HasHeader() {
		return status.Error(codes.InvalidArgument, "the first upload message must be a header")
	}
	header := first.GetHeader()
	if err := validateHeader(header); err != nil {
		return err
	}

	if err := s.beginUpload(); err != nil {
		return err
	}
	defer s.endUpload()

	tmp := filepath.Join(s.runDir, tmpBinaryName)
	if err := receiveBinary(stream, header, tmp); err != nil {
		os.Remove(tmp)
		return err
	}
	path, err := s.commit(tmp)
	if err != nil {
		os.Remove(tmp)
		return err
	}
	log.Info().Str("path", path).Uint64("bytes", header.GetSize()).Msg("mission uploaded")
	return stream.SendAndClose(&missionpb.UploadMissionResponse{})
}

// validateHeader rejects a header this host can't accept before any chunks
// are read.
func validateHeader(h *missionpb.MissionHeader) error {
	switch {
	case h.GetSize() == 0:
		return status.Error(codes.InvalidArgument, "mission size must be positive")
	case h.GetSize() > maxMissionSize:
		return status.Errorf(codes.InvalidArgument, "mission size %d exceeds the %d byte limit", h.GetSize(), maxMissionSize)
	case len(h.GetSha256()) != sha256.Size:
		return status.Errorf(codes.InvalidArgument, "sha256 must be %d bytes, got %d", sha256.Size, len(h.GetSha256()))
	case h.GetArch() != runtime.GOARCH:
		return status.Errorf(codes.InvalidArgument, "mission built for %s, this vehicle is %s", h.GetArch(), runtime.GOARCH)
	}
	return nil
}

// beginUpload marks an upload in flight, refusing if a mission is running or
// another upload already is.
func (s *server) beginUpload() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cancel != nil {
		return status.Error(codes.FailedPrecondition, "cannot upload a new mission while one is running")
	}
	if s.uploading {
		return status.Error(codes.Aborted, "upload already in progress")
	}
	s.uploading = true
	return nil
}

// endUpload clears the in-flight flag set by beginUpload.
func (s *server) endUpload() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.uploading = false
}

// receiveBinary streams the remaining chunks into path, checking the ELF
// header as soon as enough bytes arrive, and verifies the total size and
// SHA-256 against header before syncing and marking the file executable.
func receiveBinary(stream grpc.ClientStreamingServer[missionpb.UploadMissionRequest, missionpb.UploadMissionResponse], header *missionpb.MissionHeader, path string) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return status.Errorf(codes.Internal, "creating mission file: %v", err)
	}
	defer f.Close()

	hash := sha256.New()
	var received uint64
	var prefix []byte // leading bytes, kept only until the ELF check runs
	checked := false
	for {
		msg, err := stream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if !msg.HasChunk() {
			return status.Error(codes.InvalidArgument, "only the first upload message may be a header")
		}
		chunk := msg.GetChunk()
		received += uint64(len(chunk))
		if received > header.GetSize() {
			return status.Errorf(codes.InvalidArgument, "received more than the declared %d bytes", header.GetSize())
		}
		if !checked {
			prefix = append(prefix, chunk...)
			if len(prefix) >= elfPrefixLen {
				if err := checkELF(prefix, runtime.GOARCH); err != nil {
					return err
				}
				checked, prefix = true, nil
			}
		}
		hash.Write(chunk)
		if _, err := f.Write(chunk); err != nil {
			return status.Errorf(codes.Internal, "writing mission file: %v", err)
		}
	}
	if !checked {
		if err := checkELF(prefix, runtime.GOARCH); err != nil {
			return err
		}
	}
	if received != header.GetSize() {
		return status.Errorf(codes.InvalidArgument, "received %d bytes, header declared %d", received, header.GetSize())
	}
	if !bytes.Equal(hash.Sum(nil), header.GetSha256()) {
		return status.Error(codes.InvalidArgument, "sha256 mismatch: the mission was corrupted in transit")
	}
	if err := f.Sync(); err != nil {
		return status.Errorf(codes.Internal, "syncing mission file: %v", err)
	}
	if err := f.Chmod(0o755); err != nil {
		return status.Errorf(codes.Internal, "marking mission executable: %v", err)
	}
	if err := f.Close(); err != nil {
		return status.Errorf(codes.Internal, "closing mission file: %v", err)
	}
	return nil
}

// commit atomically installs the verified temp file as the mission binary,
// re-checking that no mission started while the upload was streaming.
func (s *server) commit(tmp string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cancel != nil {
		return "", status.Error(codes.FailedPrecondition, "a mission started while this upload was in progress")
	}
	path := filepath.Join(s.runDir, binaryName)
	if err := os.Rename(tmp, path); err != nil {
		return "", status.Errorf(codes.Internal, "installing mission binary: %v", err)
	}
	s.binaryPath = path
	return path, nil
}

// StartMission execs the uploaded mission binary as a subprocess, passing it
// this plugin's client socket so it can reach the vehicle's control/data
// services directly. Fails if no mission has been uploaded or one is already
// running.
func (s *server) StartMission(ctx context.Context, req *missionpb.StartMissionRequest) (*missionpb.StartMissionResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cancel != nil {
		return nil, status.Error(codes.FailedPrecondition, "mission already running")
	}
	if s.binaryPath == "" {
		return nil, status.Error(codes.FailedPrecondition, "no mission uploaded")
	}

	runCtx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(runCtx, s.binaryPath)
	cmd.Env = append(os.Environ(),
		util.ClientSockEnv+"="+s.clientSocket,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	// Run in its own process group and kill the whole group on stop/cancel,
	// not just the immediate child, in case the mission binary spawns its own
	// children.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}

	if err := cmd.Start(); err != nil {
		cancel()
		return nil, status.Errorf(codes.Internal, "starting mission binary: %v", err)
	}

	done := make(chan struct{})
	s.cancel = cancel
	s.done = done
	go s.watch(cmd, done)

	log.Info().Str("path", s.binaryPath).Int("pid", cmd.Process.Pid).Msg("mission started")
	return &missionpb.StartMissionResponse{}, nil
}

// watch waits for cmd to exit, logs the result, and clears the running-mission
// state so a subsequent StartMission is allowed.
func (s *server) watch(cmd *exec.Cmd, done chan struct{}) {
	err := cmd.Wait()
	if err != nil {
		log.Warn().Err(err).Msg("mission exited")
	} else {
		log.Info().Msg("mission exited")
	}
	close(done)

	s.mu.Lock()
	defer s.mu.Unlock()
	// Only clear state if this run is still the current one. A concurrent
	// StopMission/StartMission may have already replaced it.
	if s.done == done {
		s.cancel = nil
		s.done = nil
	}
}

// StopMission stops the running mission, if any, and waits for its subprocess
// to exit. A no-op if no mission is running.
func (s *server) StopMission(ctx context.Context, req *missionpb.StopMissionRequest) (*missionpb.StopMissionResponse, error) {
	if err := s.stop(ctx); err != nil {
		return nil, err
	}
	return &missionpb.StopMissionResponse{}, nil
}

// stop is StopMission's implementation, also used for shutdown.
func (s *server) stop(ctx context.Context) error {
	s.mu.Lock()
	cancel, done := s.cancel, s.done
	s.mu.Unlock()
	if cancel == nil {
		return nil
	}

	cancel()
	select {
	case <-done:
		return nil
	case <-time.After(stopTimeout):
		return status.Errorf(codes.DeadlineExceeded, "mission did not stop within %s", stopTimeout)
	}
}
