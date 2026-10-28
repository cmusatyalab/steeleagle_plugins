package mission

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net"
	"reflect"
	"sync"
	"testing"
	"time"

	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// fakeLoader stands in for a vehicle's LoaderService, recording its calls.
// Set its fields before the first call.
type fakeLoader struct {
	vehiclepb.UnimplementedLoaderServiceServer

	missions []*vehiclepb.MissionInfo
	loadErr  error
	// failAfter, if not 0, makes an upload fail with uploadErr after the
	// chunk it names is acknowledged.
	failAfter int32
	uploadErr error
	getErr    error // returned by GetMissions when set
	// stall makes an upload never acknowledge its first chunk.
	stall bool

	mu       sync.Mutex
	routes   []string // route keys of all calls
	chunks   []*vehiclepb.UploadMissionRequest
	uploads  int
	loads    []string
	getCalls int
}

func (f *fakeLoader) route(ctx context.Context) {
	md, _ := metadata.FromIncomingContext(ctx)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.routes = append(f.routes, md.Get(routeHeader)...)
}

func (f *fakeLoader) GetMissions(ctx context.Context, _ *vehiclepb.GetMissionsRequest) (*vehiclepb.GetMissionsResponse, error) {
	f.route(ctx)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.getCalls++
	if f.getErr != nil {
		return nil, f.getErr
	}
	return vehiclepb.GetMissionsResponse_builder{Missions: f.missions}.Build(), nil
}

func (f *fakeLoader) LoadMission(ctx context.Context, req *vehiclepb.LoadMissionRequest) (*vehiclepb.LoadMissionResponse, error) {
	f.route(ctx)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.loads = append(f.loads, req.GetName())
	if f.loadErr != nil {
		return nil, f.loadErr
	}
	return &vehiclepb.LoadMissionResponse{}, nil
}

func (f *fakeLoader) UploadMission(stream grpc.BidiStreamingServer[vehiclepb.UploadMissionRequest, vehiclepb.UploadMissionResponse]) error {
	f.route(stream.Context())
	f.mu.Lock()
	f.uploads++
	f.mu.Unlock()
	for {
		req, err := stream.Recv()
		if err != nil {
			return err
		}
		f.mu.Lock()
		f.chunks = append(f.chunks, req)
		f.mu.Unlock()
		if f.stall {
			<-stream.Context().Done()
			return stream.Context().Err()
		}
		if f.failAfter != 0 && req.GetNumber() == f.failAfter {
			if err := stream.Send(vehiclepb.UploadMissionResponse_builder{Received: req.GetNumber()}.Build()); err != nil {
				return err
			}
			return f.uploadErr
		}
		if req.GetNumber() < req.GetTotal() {
			if err := stream.Send(vehiclepb.UploadMissionResponse_builder{Received: req.GetNumber()}.Build()); err != nil {
				return err
			}
			continue
		}
		return stream.Send(vehiclepb.UploadMissionResponse_builder{Received: req.GetNumber()}.Build())
	}
}

// newSender serves f on a bufconn and returns a Sender with 10-byte chunks.
func newSender(t *testing.T, f *fakeLoader) *Sender {
	t.Helper()
	ln := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	vehiclepb.RegisterLoaderServiceServer(server, f)
	go server.Serve(ln)
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///vehicle",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return ln.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return NewSender(conn, 10, 5*time.Second)
}

type progressLog [][2]int

func (p *progressLog) fn(sent, total int) { *p = append(*p, [2]int{sent, total}) }

func hashOf(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func TestSendSkipsWhenHashMatches(t *testing.T) {
	bin := []byte("some mission binary")
	f := &fakeLoader{missions: []*vehiclepb.MissionInfo{vehiclepb.MissionInfo_builder{Name: "n", Hash: hashOf(bin)}.Build()}}
	var p progressLog
	details, err := newSender(t, f).Send(context.Background(), "v1", "n", bin, p.fn)
	if err != nil {
		t.Fatal(err)
	}
	if details != "identical mission already on vehicle; loaded without re-uploading" {
		t.Errorf("details = %q", details)
	}
	if f.uploads != 0 || !reflect.DeepEqual(f.loads, []string{"n"}) {
		t.Errorf("uploads %d, loads %v", f.uploads, f.loads)
	}
	if !reflect.DeepEqual(p, progressLog{{1, 1}}) {
		t.Errorf("progress %v", p)
	}
	for _, r := range f.routes {
		if r != "v1" {
			t.Errorf("route key %q, want v1", r)
		}
	}
	if len(f.routes) != 2 {
		t.Errorf("got %d routed calls, want 2", len(f.routes))
	}
}

func TestSendUploadsInChunks(t *testing.T) {
	bin := bytes.Repeat([]byte("abcde"), 5) // 25 bytes: 2.5 chunks
	f := &fakeLoader{}
	var p progressLog
	details, err := newSender(t, f).Send(context.Background(), "v1", "n", bin, p.fn)
	if err != nil || details != "uploaded and loaded" {
		t.Fatalf("details %q, err %v", details, err)
	}
	var got []byte
	for i, c := range f.chunks {
		if c.GetNumber() != int32(i+1) || c.GetTotal() != 3 || c.GetFilename() != "n" {
			t.Errorf("chunk %d: number %d total %d filename %q", i, c.GetNumber(), c.GetTotal(), c.GetFilename())
		}
		got = append(got, c.GetChunk()...)
	}
	if len(f.chunks) != 3 || !bytes.Equal(got, bin) {
		t.Errorf("got %d chunks, %q", len(f.chunks), got)
	}
	if !reflect.DeepEqual(p, progressLog{{1, 3}, {2, 3}, {3, 3}}) {
		t.Errorf("progress %v", p)
	}
	if !reflect.DeepEqual(f.loads, []string{"n"}) {
		t.Errorf("loads %v", f.loads)
	}
	for _, r := range f.routes {
		if r != "v1" {
			t.Errorf("route key %q, want v1", r)
		}
	}
}

func TestSendUploadsWhenSameNameDifferentHash(t *testing.T) {
	f := &fakeLoader{missions: []*vehiclepb.MissionInfo{vehiclepb.MissionInfo_builder{Name: "n", Hash: hashOf([]byte("old"))}.Build()}}
	details, err := newSender(t, f).Send(context.Background(), "v1", "n", []byte("new"), nil)
	if err != nil || details != "uploaded and loaded" {
		t.Fatalf("details %q, err %v", details, err)
	}
	if f.uploads != 1 || len(f.loads) != 1 {
		t.Errorf("uploads %d, loads %v", f.uploads, f.loads)
	}
}

func TestSendUploadsWhenGetMissionsFails(t *testing.T) {
	f := &fakeLoader{getErr: status.Error(codes.Internal, "no directory")}
	details, err := newSender(t, f).Send(context.Background(), "v1", "n", []byte("x"), nil)
	if err != nil || details != "uploaded and loaded" {
		t.Fatalf("details %q, err %v", details, err)
	}
	if f.uploads != 1 || len(f.loads) != 1 {
		t.Errorf("uploads %d, loads %v", f.uploads, f.loads)
	}
}

func TestSendEmptyBinarySendsOneChunk(t *testing.T) {
	f := &fakeLoader{}
	var p progressLog
	if _, err := newSender(t, f).Send(context.Background(), "v1", "n", nil, p.fn); err != nil {
		t.Fatal(err)
	}
	if len(f.chunks) != 1 || f.chunks[0].GetTotal() != 1 || f.chunks[0].GetNumber() != 1 || len(f.chunks[0].GetChunk()) != 0 {
		t.Errorf("chunks %v", f.chunks)
	}
	if !reflect.DeepEqual(p, progressLog{{1, 1}}) {
		t.Errorf("progress %v", p)
	}
}

func TestSendLoadRefusedWhileRunning(t *testing.T) {
	f := &fakeLoader{loadErr: status.Error(codes.FailedPrecondition, "cannot load a mission while one is running")}
	_, err := newSender(t, f).Send(context.Background(), "v1", "n", []byte("x"), nil)
	want := "uploaded, but a mission is running; hold, then upload again to load it"
	if err == nil || err.Error() != want {
		t.Errorf("err = %v, want %q", err, want)
	}
}

func TestSendSkippedLoadRefusedWhileRunning(t *testing.T) {
	bin := []byte("some mission binary")
	f := &fakeLoader{
		missions: []*vehiclepb.MissionInfo{vehiclepb.MissionInfo_builder{Name: "n", Hash: hashOf(bin)}.Build()},
		loadErr:  status.Error(codes.FailedPrecondition, "cannot load a mission while one is running"),
	}
	_, err := newSender(t, f).Send(context.Background(), "v1", "n", bin, nil)
	want := "identical mission already on vehicle, but a mission is running; hold, then upload again to load it"
	if err == nil || err.Error() != want {
		t.Errorf("err = %v, want %q", err, want)
	}
	if f.uploads != 0 {
		t.Errorf("uploads = %d", f.uploads)
	}
}

func TestSendUploadErrorMidStream(t *testing.T) {
	f := &fakeLoader{failAfter: 2, uploadErr: status.Error(codes.Unavailable, "gone")}
	var p progressLog
	_, err := newSender(t, f).Send(context.Background(), "v1", "n", bytes.Repeat([]byte("a"), 35), p.fn)
	if err == nil || err.Error() != "upload failed: Unavailable - gone" {
		t.Errorf("err = %v", err)
	}
	if len(f.loads) != 0 {
		t.Errorf("loads %v, want none", f.loads)
	}
}

func TestSendLoadNotFound(t *testing.T) {
	f := &fakeLoader{loadErr: status.Error(codes.NotFound, `no mission named "n"`)}
	_, err := newSender(t, f).Send(context.Background(), "v1", "n", []byte("x"), nil)
	if err == nil || err.Error() != `load failed: NotFound - no mission named "n"` {
		t.Errorf("err = %v", err)
	}
}

func TestSendAckTimeout(t *testing.T) {
	f := &fakeLoader{stall: true}
	s := newSender(t, f)
	s.callTimeout = 100 * time.Millisecond
	_, err := s.Send(context.Background(), "v1", "n", bytes.Repeat([]byte("a"), 25), nil)
	if err == nil || err.Error() != "upload failed: DeadlineExceeded - timed out waiting for the vehicle" {
		t.Errorf("err = %v", err)
	}
	if len(f.loads) != 0 {
		t.Errorf("loads %v, want none", f.loads)
	}
}
