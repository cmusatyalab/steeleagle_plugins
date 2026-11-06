package main

import (
	"context"
	"errors"
	"io"
	"strings"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// defaultTransferDelay is how long each 1 MiB chunk of a transfer takes, as
// over a slow link.
const defaultTransferDelay = 100 * time.Millisecond

// transferChunk is a TransferFile or TransferModel request.
type transferChunk interface {
	GetChunk() []byte
	GetNumber() int32
	GetTotal() int32
	GetFilename() string
}

// TransferFile receives a file as the daemon does, keeping only its size.
func (d *Daemon) TransferFile(stream grpc.BidiStreamingServer[daemonpb.TransferFileRequest, daemonpb.TransferFileResponse]) error {
	return d.receive(stream.Context(), "TransferFile", "files",
		func() (transferChunk, error) { return stream.Recv() },
		func(n int32) error { return stream.Send(daemonpb.TransferFileResponse_builder{Received: n}.Build()) })
}

// TransferModel receives a model as the daemon does, keeping only its size.
func (d *Daemon) TransferModel(stream grpc.BidiStreamingServer[daemonpb.TransferModelRequest, daemonpb.TransferModelResponse]) error {
	return d.receive(stream.Context(), "TransferModel", "models",
		func() (transferChunk, error) { return stream.Recv() },
		func(n int32) error { return stream.Send(daemonpb.TransferModelResponse_builder{Received: n}.Build()) })
}

func (d *Daemon) receive(ctx context.Context, method, dir string, recv func() (transferChunk, error), ack func(int32) error) error {
	d.store.mu.Lock()
	_, err := d.store.writableIn(ctx, "TransferService", method)
	d.store.mu.Unlock()
	if err != nil {
		return err
	}
	first, err := recv()
	if errors.Is(err, io.EOF) {
		return status.Error(codes.InvalidArgument, "transfer has no chunks")
	}
	if err != nil {
		return err
	}
	name, total := first.GetFilename(), first.GetTotal()
	if name == "" || name == "." || name == ".." || strings.ContainsAny(name, "/\\\x00") {
		return status.Errorf(codes.InvalidArgument, "invalid file name %q", name)
	}
	if total < 1 {
		return status.Errorf(codes.InvalidArgument, "invalid total of %d chunks", total)
	}
	size := 0
	req := first
	for n := int32(1); ; n++ {
		if req.GetNumber() != n || req.GetTotal() != total {
			return status.Errorf(codes.InvalidArgument, "expected chunk %d of %d, got chunk %d of %d", n, total, req.GetNumber(), req.GetTotal())
		}
		size += len(req.GetChunk())
		select {
		case <-time.After(d.transferDelay):
		case <-ctx.Done():
			return ctx.Err()
		}
		if n == total {
			break
		}
		if err := ack(n); err != nil {
			return err
		}
		if req, err = recv(); err != nil {
			if errors.Is(err, io.EOF) {
				return status.Errorf(codes.InvalidArgument, "transfer ended after chunk %d of %d", n, total)
			}
			return err
		}
	}
	d.store.mu.Lock()
	if _, sd, err := d.store.lookup(ctx); err == nil {
		sd.transfers[dir+"/"+name] = size
	}
	d.store.mu.Unlock()
	d.log.Info().Str("path", dir+"/"+name).Int("bytes", size).Msg("mock transfer saved")
	return ack(total)
}

// transferred is the size of the file or model saved as path ("files/name"
// or "models/name") on the daemon at ip, or -1 if there is none.
func (s *daemonStore) transferred(ip, path string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	d, ok := s.daemons[ip]
	if !ok {
		return -1
	}
	if n, ok := d.transfers[path]; ok {
		return n
	}
	return -1
}
