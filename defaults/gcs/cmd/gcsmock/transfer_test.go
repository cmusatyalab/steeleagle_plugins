package main

import (
	"strings"
	"testing"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestMockTransferFile(t *testing.T) {
	h := newHarness(t)
	h.d.transferDelay = 0
	stream, err := daemonpb.NewTransferServiceClient(h.conn).TransferFile(routed("100.64.0.2"))
	if err != nil {
		t.Fatal(err)
	}
	var acks []int32
	for n, chunk := range []string{"abc", "de"} {
		if err := stream.Send(daemonpb.TransferFileRequest_builder{Chunk: []byte(chunk), Number: int32(n + 1), Total: 2, Filename: "notes.txt"}.Build()); err != nil {
			t.Fatal(err)
		}
		resp, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		acks = append(acks, resp.GetReceived())
	}
	if len(acks) != 2 || acks[0] != 1 || acks[1] != 2 {
		t.Errorf("acks = %v", acks)
	}
	if got := h.d.store.transferred("100.64.0.2", "files/notes.txt"); got != 5 {
		t.Errorf("saved %d bytes, want 5", got)
	}
}

func TestMockTransferModelRejectsBadName(t *testing.T) {
	h := newHarness(t)
	h.d.transferDelay = 0
	stream, err := daemonpb.NewTransferServiceClient(h.conn).TransferModel(routed("100.64.0.2"))
	if err != nil {
		t.Fatal(err)
	}
	_ = stream.Send(daemonpb.TransferModelRequest_builder{Chunk: []byte("x"), Number: 1, Total: 1, Filename: "../x"}.Build())
	if _, err := stream.Recv(); status.Code(err) != codes.InvalidArgument {
		t.Errorf("err = %v, want InvalidArgument", err)
	}
}

func TestMockTransferDenied(t *testing.T) {
	h := newHarness(t, func(top *Topology) { top.DenyConfig = map[string]bool{"100.64.0.2": true} })
	h.d.transferDelay = 0
	stream, err := daemonpb.NewTransferServiceClient(h.conn).TransferModel(routed("100.64.0.2"))
	if err != nil {
		t.Fatal(err)
	}
	_ = stream.Send(daemonpb.TransferModelRequest_builder{Chunk: []byte("x"), Number: 1, Total: 1, Filename: "m.pt"}.Build())
	_, err = stream.Recv()
	if status.Code(err) != codes.PermissionDenied || !strings.Contains(status.Convert(err).Message(), "TransferService/TransferModel") {
		t.Errorf("err = %v, want PermissionDenied", err)
	}
}
