package daemons

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"github.com/rs/zerolog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// TransferChunkSize is the size of the chunks a file is sent to a daemon in,
// as eagle sends them.
const TransferChunkSize = 1 << 20

// transferClient is the part of the daemon's TransferService client
// Transfers needs.
type transferClient interface {
	TransferFile(ctx context.Context, opts ...grpc.CallOption) (grpc.BidiStreamingClient[daemonpb.TransferFileRequest, daemonpb.TransferFileResponse], error)
	TransferModel(ctx context.Context, opts ...grpc.CallOption) (grpc.BidiStreamingClient[daemonpb.TransferModelRequest, daemonpb.TransferModelResponse], error)
}

// Transfers serves GET /ws/daemons/transfer?ip=&kind=file|model&name=&size=,
// a websocket that uploads a file to a daemon's files or models directory
// with TransferFile or TransferModel, replacing any of the same name.
//
// The server sends JSON text messages: first {"type": "ready",
// "chunk_size"}, then {"type": "progress", "sent", "total"} in bytes as the
// daemon acknowledges each chunk but the last, then {"type": "done", "size"}
// once the daemon has saved the file, or {"type": "error", "detail"} at any
// point, after which it closes the socket. The page sends the file as binary
// messages of chunk_size bytes (the last one shorter, or one empty message
// for an empty file), each after the previous one's progress. Closing the
// socket cancels the transfer, and the daemon drops what it has received.
//
// It is a websocket, not a POST, so the page sees the daemon's
// acknowledgements while it is still sending: browsers don't read a response
// until they have sent the whole request.
type Transfers struct {
	client    transferClient
	chunkSize int
	log       zerolog.Logger
}

// NewTransfers returns Transfers through the daemon client c, which sends
// files in chunks of chunkSize bytes.
func NewTransfers(c transferClient, chunkSize int, log zerolog.Logger) *Transfers {
	return &Transfers{client: c, chunkSize: chunkSize, log: log}
}

// transferRequest is a transfer's query.
type transferRequest struct {
	ip, kind, name string
	size           int64
}

// parseTransfer reads a transfer's query, or says what's wrong with it.
func parseTransfer(r *http.Request) (transferRequest, error) {
	q := r.URL.Query()
	req := transferRequest{ip: q.Get("ip"), kind: q.Get("kind"), name: q.Get("name")}
	if req.ip == "" {
		return req, errors.New("a daemon ip is required")
	}
	if req.kind != "file" && req.kind != "model" {
		return req, fmt.Errorf("kind must be file or model, not %q", req.kind)
	}
	if err := checkFilename(req.name); err != nil {
		return req, err
	}
	size, err := strconv.ParseInt(q.Get("size"), 10, 64)
	if err != nil || size < 0 {
		return req, fmt.Errorf("invalid size %q", q.Get("size"))
	}
	req.size = size
	return req, nil
}

// checkFilename checks that name is a plain file name, as the daemon does.
func checkFilename(name string) error {
	if name == "" || name == "." || name == ".." || strings.ContainsAny(name, "/\\\x00") {
		return fmt.Errorf("invalid file name %q: it must be a plain name, without directories", name)
	}
	return nil
}

// chunkStream is a TransferFile or TransferModel stream.
type chunkStream struct {
	send      func(chunk []byte, number, total int32, name string) error
	recv      func() error
	closeSend func() error
}

func (t *Transfers) open(ctx context.Context, kind string) (chunkStream, error) {
	if kind == "model" {
		s, err := t.client.TransferModel(ctx)
		if err != nil {
			return chunkStream{}, err
		}
		return chunkStream{
			send: func(chunk []byte, number, total int32, name string) error {
				return s.Send(daemonpb.TransferModelRequest_builder{Chunk: chunk, Number: number, Total: total, Filename: name}.Build())
			},
			recv:      func() error { _, err := s.Recv(); return err },
			closeSend: s.CloseSend,
		}, nil
	}
	s, err := t.client.TransferFile(ctx)
	if err != nil {
		return chunkStream{}, err
	}
	return chunkStream{
		send: func(chunk []byte, number, total int32, name string) error {
			return s.Send(daemonpb.TransferFileRequest_builder{Chunk: chunk, Number: number, Total: total, Filename: name}.Build())
		},
		recv:      func() error { _, err := s.Recv(); return err },
		closeSend: s.CloseSend,
	}, nil
}

// errBadChunk is a page's chunk that isn't the size it should be.
type errBadChunk struct{ number, got, want int }

func (e errBadChunk) Error() string {
	return fmt.Sprintf("chunk %d is %d bytes, want %d", e.number, e.got, e.want)
}

func (t *Transfers) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		// Accept has answered
		return
	}
	defer conn.CloseNow()
	conn.SetReadLimit(int64(t.chunkSize) + 1024)
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	fail := func(detail string) {
		_ = wsjson.Write(ctx, conn, map[string]string{"type": "error", "detail": detail})
		conn.Close(websocket.StatusNormalClosure, "")
	}
	req, err := parseTransfer(r)
	if err != nil {
		fail(err.Error())
		return
	}
	log := t.log.With().Str("daemon", req.ip).Str("kind", req.kind).Str("name", req.name).Int64("size", req.size).Logger()

	// The page's messages are read for as long as the socket is open, so
	// closing it cancels ctx, and with it the daemon's stream, even while
	// waiting on the daemon
	chunks, closed := readChunks(ctx, cancel, conn)
	err = t.transfer(ctx, conn, chunks, req)
	if err != nil && ctx.Err() != nil && closed() {
		err = errClosed
	}
	var bad errBadChunk
	switch {
	case err == nil:
		log.Info().Msg("transferred")
		_ = wsjson.Write(ctx, conn, map[string]any{"type": "done", "size": req.size})
		conn.Close(websocket.StatusNormalClosure, "")
	case errors.Is(err, errClosed) || ctx.Err() != nil:
		// The page closed the socket, cancelling the transfer
		log.Info().Msg("transfer cancelled")
	case errors.As(err, &bad):
		log.Warn().Err(err).Msg("transfer")
		fail(err.Error())
	default:
		log.Warn().Err(err).Msg("transfer")
		fail(rpcDetail(err))
	}
}

// errClosed is a transfer the page cancelled by closing its socket.
var errClosed = errors.New("the page closed the transfer")

// transfer relays the page's chunks to the daemon, one at a time, telling
// the page of each acknowledgement.
func (t *Transfers) transfer(ctx context.Context, conn *websocket.Conn, chunks <-chan message, req transferRequest) error {
	// The daemon drops a transfer whose stream is cancelled
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := t.open(routed(ctx, req.ip), req.kind)
	if err != nil {
		return err
	}
	chunk := int64(t.chunkSize)
	total := max(1, (req.size+chunk-1)/chunk)
	if err := wsjson.Write(ctx, conn, map[string]any{"type": "ready", "chunk_size": t.chunkSize}); err != nil {
		return err
	}
	for n := int64(1); n <= total; n++ {
		var m message
		select {
		case m = <-chunks:
		case <-ctx.Done():
			return ctx.Err()
		}
		want := min(chunk, req.size-(n-1)*chunk)
		if m.msgType != websocket.MessageBinary || int64(len(m.data)) != want {
			return errBadChunk{int(n), len(m.data), int(want)}
		}
		if err := stream.send(m.data, int32(n), int32(total), req.name); err != nil {
			// The stream ended; recv has the status it ended with
			if rerr := stream.recv(); rerr != nil {
				return rerr
			}
			return err
		}
		if n == total {
			break
		}
		if err := stream.recv(); err != nil {
			return err
		}
		if err := wsjson.Write(ctx, conn, map[string]any{"type": "progress", "sent": n * chunk, "total": req.size}); err != nil {
			return err
		}
	}
	if err := stream.closeSend(); err != nil {
		return err
	}
	// The daemon acknowledges the last chunk once the file is saved, then
	// ends the stream
	return stream.recv()
}

// message is a websocket message from the page.
type message struct {
	msgType websocket.MessageType
	data    []byte
}

// readChunks reads the page's messages until ctx is done, cancelling it if
// the socket fails or closes; closed then reports whether it did. A page
// sends a chunk only after the previous one's progress, so at most one
// waits.
func readChunks(ctx context.Context, cancel context.CancelFunc, conn *websocket.Conn) (<-chan message, func() bool) {
	out := make(chan message)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			msgType, data, err := conn.Read(ctx)
			if err != nil {
				cancel()
				return
			}
			select {
			case out <- message{msgType, data}:
			case <-ctx.Done():
				return
			}
		}
	}()
	return out, func() bool {
		select {
		case <-done:
			return true
		default:
			return false
		}
	}
}

// rpcDetail is a daemon call's error as the page shows it: its message,
// with what the GCS needs if the daemon refused it.
func rpcDetail(err error) string {
	st := status.Convert(err)
	if st.Code() == codes.PermissionDenied {
		return st.Message() + authHint
	}
	return st.Message()
}
