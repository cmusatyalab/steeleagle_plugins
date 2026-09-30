package main

import (
	"context"
	"crypto/sha256"
	"debug/elf"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	missionpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/mission"
)

// testChunkSize is small so multi-chunk paths are exercised with small files.
const testChunkSize = 4096

// wantCode fails the test unless err is a gRPC status error with the given
// code.
func wantCode(t *testing.T, err error, code codes.Code) {
	t.Helper()
	if status.Code(err) != code {
		t.Fatalf("got error %v, want code %s", err, code)
	}
}

// startServer serves s over an in-memory listener and returns a client to it.
func startServer(t *testing.T, s *server) missionpb.MissionServiceClient {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	g := grpc.NewServer()
	missionpb.RegisterMissionServiceServer(g, s)
	go g.Serve(lis)
	t.Cleanup(g.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("dialing bufconn: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return missionpb.NewMissionServiceClient(conn)
}

// headerFor builds a correct header for data.
func headerFor(arch string, data []byte) *missionpb.MissionHeader {
	sum := sha256.Sum256(data)
	return missionpb.MissionHeader_builder{Arch: arch, Size: uint64(len(data)), Sha256: sum[:]}.Build()
}

// openUpload opens an upload stream and sends header plus data in
// testChunkSize chunks without closing it, so callers can interleave other
// actions before finishing it with CloseAndRecv.
func openUpload(t *testing.T, ctx context.Context, client missionpb.MissionServiceClient, header *missionpb.MissionHeader, data []byte) grpc.ClientStreamingClient[missionpb.UploadMissionRequest, missionpb.UploadMissionResponse] {
	t.Helper()
	stream, err := client.UploadMission(ctx)
	if err != nil {
		t.Fatalf("opening UploadMission: %v", err)
	}
	if err := stream.Send(missionpb.UploadMissionRequest_builder{Header: header}.Build()); err != nil {
		return stream // the server already rejected it; CloseAndRecv reports why
	}
	for off := 0; off < len(data); off += testChunkSize {
		end := min(off+testChunkSize, len(data))
		if err := stream.Send(missionpb.UploadMissionRequest_builder{Chunk: data[off:end]}.Build()); err != nil {
			return stream
		}
	}
	return stream
}

// sendUpload performs a complete upload and returns its final error.
func sendUpload(t *testing.T, client missionpb.MissionServiceClient, header *missionpb.MissionHeader, data []byte) error {
	t.Helper()
	_, err := openUpload(t, t.Context(), client, header, data).CloseAndRecv()
	return err
}

// readMock returns the built mock mission binary.
func readMock(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile(mockMissionBinary)
	if err != nil {
		t.Fatalf("reading mock mission binary: %v", err)
	}
	return data
}

// upload uploads the mock mission binary to s.
func upload(t *testing.T, s *server) {
	t.Helper()
	data := readMock(t)
	if err := sendUpload(t, startServer(t, s), headerFor(runtime.GOARCH, data), data); err != nil {
		t.Fatalf("UploadMission: %v", err)
	}
}

// waitUploading blocks until s reports an upload in flight.
func waitUploading(t *testing.T, s *server) {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		s.mu.Lock()
		uploading := s.uploading
		s.mu.Unlock()
		if uploading {
			return
		}
		select {
		case <-deadline:
			t.Fatal("upload never became in-flight")
		case <-time.After(10 * time.Millisecond):
		}
	}
}

// startBlockingMission uploads and starts the mock mission, which blocks
// until stopped.
func startBlockingMission(t *testing.T, s *server, runDir string) {
	t.Helper()
	t.Setenv("MOCK_MISSION_MARKER", filepath.Join(runDir, "marker"))
	t.Setenv("MOCK_MISSION_BLOCK", "1")
	upload(t, s)
	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission: %v", err)
	}
	t.Cleanup(func() { s.stop(context.Background()) })
}

func TestGetMissionInfoReportsHostArch(t *testing.T) {
	client := startServer(t, newServer("", t.TempDir()))
	resp, err := client.GetMissionInfo(t.Context(), &missionpb.GetMissionInfoRequest{})
	if err != nil {
		t.Fatalf("GetMissionInfo: %v", err)
	}
	if resp.GetArch() != runtime.GOARCH {
		t.Fatalf("arch = %q, want %q", resp.GetArch(), runtime.GOARCH)
	}
}

func TestUploadMissionCommitsExecutableBinary(t *testing.T) {
	runDir := t.TempDir()
	upload(t, newServer("", runDir))
	info, err := os.Stat(filepath.Join(runDir, binaryName))
	if err != nil {
		t.Fatalf("stat committed binary: %v", err)
	}
	if info.Mode().Perm() != 0o755 {
		t.Fatalf("mode = %v, want 0755", info.Mode().Perm())
	}
	if _, err := os.Stat(filepath.Join(runDir, tmpBinaryName)); !os.IsNotExist(err) {
		t.Fatalf("temp file still present after commit: %v", err)
	}
}

func TestUploadMissionRejectsEmptyStream(t *testing.T) {
	client := startServer(t, newServer("", t.TempDir()))
	stream, err := client.UploadMission(t.Context())
	if err != nil {
		t.Fatalf("opening UploadMission: %v", err)
	}
	_, err = stream.CloseAndRecv()
	wantCode(t, err, codes.InvalidArgument)
}

func TestUploadMissionRejectsChunkBeforeHeader(t *testing.T) {
	client := startServer(t, newServer("", t.TempDir()))
	stream, err := client.UploadMission(t.Context())
	if err != nil {
		t.Fatalf("opening UploadMission: %v", err)
	}
	stream.Send(missionpb.UploadMissionRequest_builder{Chunk: []byte("abc")}.Build())
	_, err = stream.CloseAndRecv()
	wantCode(t, err, codes.InvalidArgument)
}

func TestUploadMissionRejectsBadHeaders(t *testing.T) {
	data := readMock(t)
	sum := sha256.Sum256(data)
	cases := map[string]*missionpb.MissionHeader{
		"zero size":  missionpb.MissionHeader_builder{Arch: runtime.GOARCH, Size: 0, Sha256: sum[:]}.Build(),
		"oversize":   missionpb.MissionHeader_builder{Arch: runtime.GOARCH, Size: maxMissionSize + 1, Sha256: sum[:]}.Build(),
		"short hash": missionpb.MissionHeader_builder{Arch: runtime.GOARCH, Size: uint64(len(data)), Sha256: sum[:5]}.Build(),
		"wrong arch": missionpb.MissionHeader_builder{Arch: "not-" + runtime.GOARCH, Size: uint64(len(data)), Sha256: sum[:]}.Build(),
	}
	for name, h := range cases {
		t.Run(name, func(t *testing.T) {
			client := startServer(t, newServer("", t.TempDir()))
			wantCode(t, sendUpload(t, client, h, data), codes.InvalidArgument)
		})
	}
}

func TestUploadMissionRejectsSizeAndHashMismatches(t *testing.T) {
	data := readMock(t)
	cases := map[string]struct {
		header *missionpb.MissionHeader
		body   []byte
	}{
		"short stream": {headerFor(runtime.GOARCH, data), data[:len(data)-1]},
		"long stream":  {headerFor(runtime.GOARCH, data), append(append([]byte{}, data...), 0)},
		"hash mismatch": {func() *missionpb.MissionHeader {
			h := headerFor(runtime.GOARCH, data)
			bad := append([]byte{}, h.GetSha256()...)
			bad[0] ^= 0xff
			return missionpb.MissionHeader_builder{Arch: h.GetArch(), Size: h.GetSize(), Sha256: bad}.Build()
		}(), data},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			runDir := t.TempDir()
			client := startServer(t, newServer("", runDir))
			wantCode(t, sendUpload(t, client, c.header, c.body), codes.InvalidArgument)
			if _, err := os.Stat(filepath.Join(runDir, tmpBinaryName)); !os.IsNotExist(err) {
				t.Fatalf("temp file left behind: %v", err)
			}
		})
	}
}

func TestUploadMissionRejectsWrongELFMachine(t *testing.T) {
	other := elf.EM_AARCH64
	if runtime.GOARCH == "arm64" {
		other = elf.EM_X86_64
	}
	data := append(fakeELF(other), make([]byte, 1000)...)
	client := startServer(t, newServer("", t.TempDir()))
	wantCode(t, sendUpload(t, client, headerFor(runtime.GOARCH, data), data), codes.InvalidArgument)
}

func TestUploadMissionRejectsNonELF(t *testing.T) {
	data := []byte("#!/bin/sh\necho definitely not a mission binary\n")
	client := startServer(t, newServer("", t.TempDir()))
	wantCode(t, sendUpload(t, client, headerFor(runtime.GOARCH, data), data), codes.InvalidArgument)
}

// TestUploadMissionWhileRunning confirms UploadMission is rejected while a
// mission is running, so it can't be swapped out from under a live run.
func TestUploadMissionWhileRunning(t *testing.T) {
	runDir := t.TempDir()
	s := newServer("", runDir)
	startBlockingMission(t, s, runDir)
	data := readMock(t)
	wantCode(t, sendUpload(t, startServer(t, s), headerFor(runtime.GOARCH, data), data), codes.FailedPrecondition)
}

func TestUploadMissionRejectsConcurrentUpload(t *testing.T) {
	s := newServer("", t.TempDir())
	client := startServer(t, s)
	data := readMock(t)
	first := openUpload(t, t.Context(), client, headerFor(runtime.GOARCH, data), data[:testChunkSize])
	waitUploading(t, s)

	wantCode(t, sendUpload(t, client, headerFor(runtime.GOARCH, data), data), codes.Aborted)
	first.CloseAndRecv()
}

// TestUploadMissionMissionStartedDuringUpload covers the commit-time re-check:
// a mission that starts while an upload is streaming blocks the commit.
func TestUploadMissionMissionStartedDuringUpload(t *testing.T) {
	runDir := t.TempDir()
	t.Setenv("MOCK_MISSION_MARKER", filepath.Join(runDir, "marker"))
	t.Setenv("MOCK_MISSION_BLOCK", "1")
	s := newServer("", runDir)
	client := startServer(t, s)
	data := readMock(t)

	stream := openUpload(t, t.Context(), client, headerFor(runtime.GOARCH, data), data)
	waitUploading(t, s)
	s.preloadMission(mockMissionBinary)
	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission: %v", err)
	}
	t.Cleanup(func() { s.stop(context.Background()) })

	_, err := stream.CloseAndRecv()
	wantCode(t, err, codes.FailedPrecondition)
	if _, err := os.Stat(filepath.Join(runDir, binaryName)); !os.IsNotExist(err) {
		t.Fatalf("binary was committed despite a running mission: %v", err)
	}
}

func TestUploadMissionFailedUploadKeepsPreviousBinary(t *testing.T) {
	runDir := t.TempDir()
	s := newServer("", runDir)
	upload(t, s)
	before, err := os.ReadFile(filepath.Join(runDir, binaryName))
	if err != nil {
		t.Fatalf("reading committed binary: %v", err)
	}

	data := readMock(t)
	wantCode(t, sendUpload(t, startServer(t, s), headerFor(runtime.GOARCH, data), data[:len(data)/2]), codes.InvalidArgument)

	after, err := os.ReadFile(filepath.Join(runDir, binaryName))
	if err != nil || string(after) != string(before) {
		t.Fatalf("previous binary changed or vanished after a failed upload (err=%v)", err)
	}
}

// TestUploadMissionClientCancelKeepsPreviousBinary covers a client (swarm
// controller, ultimately a closed browser tab) disconnecting mid-upload.
func TestUploadMissionClientCancelKeepsPreviousBinary(t *testing.T) {
	runDir := t.TempDir()
	s := newServer("", runDir)
	upload(t, s)
	before, _ := os.ReadFile(filepath.Join(runDir, binaryName))

	ctx, cancel := context.WithCancel(t.Context())
	data := readMock(t)
	openUpload(t, ctx, startServer(t, s), headerFor(runtime.GOARCH, data), data[:len(data)/2])
	waitUploading(t, s)
	cancel()

	deadline := time.After(5 * time.Second)
	for {
		s.mu.Lock()
		uploading := s.uploading
		s.mu.Unlock()
		if !uploading {
			break
		}
		select {
		case <-deadline:
			t.Fatal("cancelled upload never cleared its in-flight flag")
		case <-time.After(10 * time.Millisecond):
		}
	}
	after, err := os.ReadFile(filepath.Join(runDir, binaryName))
	if err != nil || string(after) != string(before) {
		t.Fatalf("previous binary changed after a cancelled upload (err=%v)", err)
	}
	if _, err := os.Stat(filepath.Join(runDir, tmpBinaryName)); !os.IsNotExist(err) {
		t.Fatalf("temp file left behind after cancel: %v", err)
	}
}

func TestStartMissionWithoutUpload(t *testing.T) {
	s := newServer("", t.TempDir())
	_, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{})
	wantCode(t, err, codes.FailedPrecondition)
}

func TestStopMissionWithoutRunningIsNoop(t *testing.T) {
	s := newServer("", t.TempDir())
	if _, err := s.StopMission(t.Context(), &missionpb.StopMissionRequest{}); err != nil {
		t.Fatalf("StopMission with nothing running: %v", err)
	}
}

// TestStartMissionRunsBinaryWithClientSocket uploads the mock mission binary,
// starts it, and confirms it actually ran and received the client socket
// missionservice was configured with.
func TestStartMissionRunsBinaryWithClientSocket(t *testing.T) {
	runDir := t.TempDir()
	marker := filepath.Join(runDir, "marker")
	t.Setenv("MOCK_MISSION_MARKER", marker)
	t.Setenv("MOCK_MISSION_BLOCK", "1")

	s := newServer("/tmp/fake-client.sock", runDir)
	upload(t, s)

	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission: %v", err)
	}
	t.Cleanup(func() { s.stop(context.Background()) })

	deadline := time.After(5 * time.Second)
	for {
		data, err := os.ReadFile(marker)
		if err == nil {
			if got := string(data); got != s.clientSocket {
				t.Fatalf("mission subprocess saw client socket %q, want %q", got, s.clientSocket)
			}
			break
		}
		select {
		case <-deadline:
			t.Fatalf("mock mission binary never wrote its marker: %v", err)
		case <-time.After(20 * time.Millisecond):
		}
	}
}

// TestStartMissionAlreadyRunning confirms a second StartMission is rejected
// while the first mission is still running.
func TestStartMissionAlreadyRunning(t *testing.T) {
	runDir := t.TempDir()
	t.Setenv("MOCK_MISSION_MARKER", filepath.Join(runDir, "marker"))
	t.Setenv("MOCK_MISSION_BLOCK", "1")

	s := newServer("", runDir)
	upload(t, s)
	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission: %v", err)
	}
	t.Cleanup(func() { s.stop(context.Background()) })

	_, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{})
	wantCode(t, err, codes.FailedPrecondition)
}

// TestStopMissionKillsSubprocessAndAllowsRestart confirms StopMission
// terminates a still-running mission and that the server accepts a fresh
// StartMission afterward.
func TestStopMissionKillsSubprocessAndAllowsRestart(t *testing.T) {
	runDir := t.TempDir()
	t.Setenv("MOCK_MISSION_MARKER", filepath.Join(runDir, "marker"))
	t.Setenv("MOCK_MISSION_BLOCK", "1")

	s := newServer("", runDir)
	upload(t, s)
	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission: %v", err)
	}

	if _, err := s.StopMission(t.Context(), &missionpb.StopMissionRequest{}); err != nil {
		t.Fatalf("StopMission: %v", err)
	}

	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission after stop: %v", err)
	}
	t.Cleanup(func() { s.stop(context.Background()) })
}

// TestMissionExitOnItsOwnClearsState confirms a mission that exits by itself
// (rather than being stopped) still clears run state so a new one can start.
func TestMissionExitOnItsOwnClearsState(t *testing.T) {
	runDir := t.TempDir()
	t.Setenv("MOCK_MISSION_MARKER", filepath.Join(runDir, "marker"))
	t.Setenv("MOCK_MISSION_BLOCK", "0")

	s := newServer("", runDir)
	upload(t, s)
	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission: %v", err)
	}

	deadline := time.After(5 * time.Second)
	for {
		s.mu.Lock()
		running := s.cancel != nil
		s.mu.Unlock()
		if !running {
			break
		}
		select {
		case <-deadline:
			t.Fatal("mission never cleared run state after exiting on its own")
		case <-time.After(20 * time.Millisecond):
		}
	}

	if _, err := s.StartMission(t.Context(), &missionpb.StartMissionRequest{}); err != nil {
		t.Fatalf("StartMission after previous mission exited: %v", err)
	}
	t.Cleanup(func() { s.stop(context.Background()) })
}
