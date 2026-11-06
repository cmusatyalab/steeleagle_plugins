package daemons

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// fakeTransfer is a daemon's TransferService: it saves what it is sent by
// kind and name, acknowledging each chunk as the daemon does.
type fakeTransfer struct {
	daemonpb.UnimplementedTransferServiceServer
	// err, if set, ends every transfer before its first chunk
	err error
	// block, if set, closes blocked once a transfer has its first chunk, then
	// holds it until it is cancelled, then closes cancelled
	block              bool
	blocked, cancelled chan struct{}

	mu    sync.Mutex
	saved map[string][]byte // "kind/name"
	keys  []string
}

type chunk interface {
	GetChunk() []byte
	GetNumber() int32
	GetTotal() int32
	GetFilename() string
}

func (f *fakeTransfer) TransferFile(s grpc.BidiStreamingServer[daemonpb.TransferFileRequest, daemonpb.TransferFileResponse]) error {
	return f.receive("file", s.Context(),
		func() (chunk, error) { return s.Recv() },
		func(n int32) error { return s.Send(daemonpb.TransferFileResponse_builder{Received: n}.Build()) })
}

func (f *fakeTransfer) TransferModel(s grpc.BidiStreamingServer[daemonpb.TransferModelRequest, daemonpb.TransferModelResponse]) error {
	return f.receive("model", s.Context(),
		func() (chunk, error) { return s.Recv() },
		func(n int32) error { return s.Send(daemonpb.TransferModelResponse_builder{Received: n}.Build()) })
}

func (f *fakeTransfer) receive(kind string, ctx context.Context, recv func() (chunk, error), ack func(int32) error) error {
	md, _ := metadata.FromIncomingContext(ctx)
	f.mu.Lock()
	f.keys = append(f.keys, strings.Join(md.Get(routeHeader), ","))
	f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	var data []byte
	var name string
	for n := int32(1); ; n++ {
		c, err := recv()
		if err != nil {
			return err
		}
		if n == 1 {
			name = c.GetFilename()
		}
		if c.GetNumber() != n {
			return status.Errorf(codes.InvalidArgument, "expected chunk %d, got %d", n, c.GetNumber())
		}
		data = append(data, c.GetChunk()...)
		if f.block {
			close(f.blocked)
			<-ctx.Done()
			close(f.cancelled)
			return ctx.Err()
		}
		if n == c.GetTotal() {
			f.mu.Lock()
			f.saved[kind+"/"+name] = data
			f.mu.Unlock()
			return ack(n)
		}
		if err := ack(n); err != nil {
			return err
		}
	}
}

// transferServer serves Transfers, with chunks of chunkSize, to a fake
// daemon f.
func transferServer(t *testing.T, f *fakeTransfer, chunkSize int) *httptest.Server {
	t.Helper()
	if f.saved == nil {
		f.saved = map[string][]byte{}
	}
	ln := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	daemonpb.RegisterTransferServiceServer(gs, f)
	go gs.Serve(ln)
	t.Cleanup(gs.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return ln.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	srv := httptest.NewServer(NewTransfers(daemonpb.NewTransferServiceClient(conn), chunkSize, zerolog.Nop()))
	t.Cleanup(srv.Close)
	return srv
}

func dialTransfer(t *testing.T, srv *httptest.Server, query url.Values) *websocket.Conn {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http")+"/?"+query.Encode(), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.CloseNow() })
	return c
}

type event struct {
	Type      string `json:"type"`
	ChunkSize int    `json:"chunk_size"`
	Sent      int    `json:"sent"`
	Total     int    `json:"total"`
	Size      int    `json:"size"`
	Detail    string `json:"detail"`
}

func readEvent(t *testing.T, c *websocket.Conn) event {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var ev event
	if err := wsjson.Read(ctx, c, &ev); err != nil {
		t.Fatalf("read event: %v", err)
	}
	return ev
}

// upload sends data as a browser does, a chunk of the size the server asks
// for after each acknowledgement, and returns every event it was sent.
func upload(t *testing.T, c *websocket.Conn, data []byte) []event {
	t.Helper()
	ready := readEvent(t, c)
	if ready.Type != "ready" {
		return []event{ready}
	}
	events := []event{ready}
	for off := 0; ; {
		end := min(off+ready.ChunkSize, len(data))
		if err := c.Write(context.Background(), websocket.MessageBinary, data[off:end]); err != nil {
			t.Fatal(err)
		}
		off = end
		ev := readEvent(t, c)
		events = append(events, ev)
		if ev.Type != "progress" {
			return events
		}
	}
}

func query(ip, kind, name string, size int) url.Values {
	return url.Values{"ip": {ip}, "kind": {kind}, "name": {name}, "size": {fmt.Sprint(size)}}
}

func TestTransferFileInChunks(t *testing.T) {
	f := &fakeTransfer{}
	srv := transferServer(t, f, 4)
	data := []byte("0123456789") // 2.5 chunks
	events := upload(t, dialTransfer(t, srv, query("100.64.0.2", "file", "notes.txt", len(data))), data)

	want := []event{
		{Type: "ready", ChunkSize: 4},
		{Type: "progress", Sent: 4, Total: 10},
		{Type: "progress", Sent: 8, Total: 10},
		{Type: "done", Size: 10},
	}
	if fmt.Sprint(events) != fmt.Sprint(want) {
		t.Errorf("events = %+v\nwant     %+v", events, want)
	}
	if got := f.saved["file/notes.txt"]; !bytes.Equal(got, data) {
		t.Errorf("saved %q", got)
	}
	if fmt.Sprint(f.keys) != "[100.64.0.2]" {
		t.Errorf("route keys = %q", f.keys)
	}
}

func TestTransferModel(t *testing.T) {
	f := &fakeTransfer{}
	srv := transferServer(t, f, 1<<20)
	data := []byte("weights")
	events := upload(t, dialTransfer(t, srv, query("100.64.0.2", "model", "yolo.pt", len(data))), data)
	if last := events[len(events)-1]; last.Type != "done" || last.Size != len(data) {
		t.Fatalf("events = %+v", events)
	}
	if !bytes.Equal(f.saved["model/yolo.pt"], data) || len(f.saved) != 1 {
		t.Errorf("saved %q", f.saved)
	}
}

func TestTransferEmptyFile(t *testing.T) {
	f := &fakeTransfer{}
	srv := transferServer(t, f, 4)
	events := upload(t, dialTransfer(t, srv, query("100.64.0.2", "file", "empty", 0)), nil)
	if last := events[len(events)-1]; last.Type != "done" || last.Size != 0 {
		t.Fatalf("events = %+v", events)
	}
	if got, ok := f.saved["file/empty"]; !ok || len(got) != 0 {
		t.Errorf("saved %q (%v)", got, ok)
	}
}

func TestTransferRejectsBadRequests(t *testing.T) {
	for name, q := range map[string]url.Values{
		"no ip":         query("", "file", "a", 1),
		"bad kind":      query("100.64.0.2", "package", "a", 1),
		"empty name":    query("100.64.0.2", "file", "", 1),
		"directory":     query("100.64.0.2", "file", "../a", 1),
		"backslash":     query("100.64.0.2", "file", `a\b`, 1),
		"dot":           query("100.64.0.2", "file", ".", 1),
		"dot dot":       query("100.64.0.2", "file", "..", 1),
		"negative size": query("100.64.0.2", "file", "a", -1),
		"no size":       {"ip": {"100.64.0.2"}, "kind": {"file"}, "name": {"a"}},
	} {
		t.Run(name, func(t *testing.T) {
			f := &fakeTransfer{}
			srv := transferServer(t, f, 4)
			ev := readEvent(t, dialTransfer(t, srv, q))
			if ev.Type != "error" || ev.Detail == "" {
				t.Errorf("event = %+v, want an error", ev)
			}
			if len(f.keys) != 0 {
				t.Errorf("daemon was called")
			}
		})
	}
}

func TestTransferDaemonErrorHasAuthHint(t *testing.T) {
	f := &fakeTransfer{err: status.Error(codes.PermissionDenied, "not allowed")}
	srv := transferServer(t, f, 4)
	events := upload(t, dialTransfer(t, srv, query("100.64.0.2", "file", "a", 3)), []byte("abc"))
	last := events[len(events)-1]
	if last.Type != "error" || last.Detail != "not allowed"+authHint {
		t.Errorf("events = %+v", events)
	}
}

func TestTransferRejectsWrongChunkSize(t *testing.T) {
	f := &fakeTransfer{}
	srv := transferServer(t, f, 4)
	c := dialTransfer(t, srv, query("100.64.0.2", "file", "a", 10))
	readEvent(t, c) // ready
	if err := c.Write(context.Background(), websocket.MessageBinary, []byte("abc")); err != nil {
		t.Fatal(err)
	}
	if ev := readEvent(t, c); ev.Type != "error" || !strings.Contains(ev.Detail, "chunk") {
		t.Errorf("event = %+v", ev)
	}
	if len(f.saved) != 0 {
		t.Errorf("saved %q", f.saved)
	}
}

func TestTransferClosingCancelsTheDaemonStream(t *testing.T) {
	f := &fakeTransfer{block: true, blocked: make(chan struct{}), cancelled: make(chan struct{})}
	srv := transferServer(t, f, 4)
	c := dialTransfer(t, srv, query("100.64.0.2", "file", "a", 10))
	readEvent(t, c) // ready
	if err := c.Write(context.Background(), websocket.MessageBinary, []byte("0123")); err != nil {
		t.Fatal(err)
	}
	<-f.blocked
	c.Close(websocket.StatusNormalClosure, "cancelled")
	select {
	case <-f.cancelled:
	case <-time.After(5 * time.Second):
		t.Fatal("the daemon's stream wasn't cancelled")
	}
}

// The route is a GET websocket; anything else isn't served
func TestTransferRequiresWebsocket(t *testing.T) {
	srv := transferServer(t, &fakeTransfer{}, 4)
	resp, err := http.Get(srv.URL + "/?" + query("100.64.0.2", "file", "a", 1).Encode())
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusSwitchingProtocols {
		t.Errorf("status = %d", resp.StatusCode)
	}
}
