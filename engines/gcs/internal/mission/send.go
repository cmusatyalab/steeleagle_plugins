package mission

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"sync/atomic"
	"time"

	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// routeHeader is the metadata key the daemon routes calls to vehicles by.
const routeHeader = "x-route-key"

// Texts of the results a vehicle can have, besides a gRPC failure.
const (
	// DetailsUploaded is the result of a vehicle the mission was uploaded to
	// and loaded on.
	DetailsUploaded = "uploaded and loaded"
	// DetailsAlreadyLoaded is the result of a vehicle that already had the
	// identical mission, so the upload was a no-op apart from loading it.
	DetailsAlreadyLoaded = "identical mission already on vehicle; loaded without re-uploading"
	// textRunning is why a mission was uploaded but not loaded.
	textRunningSkipped = "identical mission already on vehicle, but a mission is running; hold, then upload again to load it"
	textRunning        = "uploaded, but a mission is running; hold, then upload again to load it"
)

// Progress reports that sent of total chunks of a mission have been
// acknowledged by a vehicle.
type Progress func(sent, total int)

// Sender uploads missions to vehicles through the daemon, and loads them.
type Sender struct {
	conn        grpc.ClientConnInterface
	chunkSize   int
	callTimeout time.Duration
}

// NewSender returns a Sender that makes its calls on conn, sends missions in
// chunks of chunkSize bytes, and gives each call (for an upload, each wait
// for an acknowledgement) callTimeout to finish.
func NewSender(conn grpc.ClientConnInterface, chunkSize int, callTimeout time.Duration) *Sender {
	return &Sender{conn: conn, chunkSize: chunkSize, callTimeout: callTimeout}
}

// Send uploads the mission binary to the vehicle under name, unless the
// vehicle already has a mission of that name with the same contents, and
// loads it. progress, if not nil, is called as chunks are acknowledged.
//
// details is DetailsAlreadyLoaded if the upload was skipped, else
// DetailsUploaded. An
// error's text is the vehicle's result: "upload failed: <code> - <message>",
// "load failed: <code> - <message>", or that the mission is running.
func (s *Sender) Send(ctx context.Context, vehicle, name string, binary []byte, progress Progress) (details string, err error) {
	ctx = metadata.AppendToOutgoingContext(ctx, routeHeader, vehicle)
	client := vehiclepb.NewLoaderServiceClient(s.conn)

	skipped := s.has(ctx, client, name, binary)
	if skipped {
		if progress != nil {
			progress(1, 1)
		}
		details = DetailsAlreadyLoaded
	} else if err := s.upload(ctx, client, name, binary, progress); err != nil {
		return "", fmt.Errorf("upload failed: %s", describe(err))
	} else {
		details = DetailsUploaded
	}

	loadCtx, cancel := context.WithTimeout(ctx, s.callTimeout)
	defer cancel()
	_, err = client.LoadMission(loadCtx, vehiclepb.LoadMissionRequest_builder{Name: name}.Build())
	if status.Code(err) == codes.FailedPrecondition {
		if skipped {
			return "", errors.New(textRunningSkipped)
		}
		return "", errors.New(textRunning)
	} else if err != nil {
		return "", fmt.Errorf("load failed: %s", describe(err))
	}
	return details, nil
}

// has reports whether the vehicle has a mission called name with the
// contents of binary. If it can't tell it reports false, so the mission is
// uploaded anyway.
func (s *Sender) has(ctx context.Context, client vehiclepb.LoaderServiceClient, name string, binary []byte) bool {
	ctx, cancel := context.WithTimeout(ctx, s.callTimeout)
	defer cancel()
	resp, err := client.GetMissions(ctx, &vehiclepb.GetMissionsRequest{})
	if err != nil {
		return false
	}
	sum := sha256.Sum256(binary)
	hash := hex.EncodeToString(sum[:])
	for _, m := range resp.GetMissions() {
		if m.GetName() == name && m.GetHash() == hash {
			return true
		}
	}
	return false
}

// upload streams binary to the vehicle in chunks, waiting for the
// acknowledgement of each before sending the next.
func (s *Sender) upload(ctx context.Context, client vehiclepb.LoaderServiceClient, name string, binary []byte, progress Progress) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := client.UploadMission(ctx)
	if err != nil {
		return err
	}

	// wait runs a blocking stream call, cancelling the stream if it takes
	// longer than the call timeout.
	var timedOut atomic.Bool
	wait := func(call func() error) error {
		timer := time.AfterFunc(s.callTimeout, func() {
			timedOut.Store(true)
			cancel()
		})
		err := call()
		timer.Stop()
		if err != nil && timedOut.Load() {
			return status.Error(codes.DeadlineExceeded, "timed out waiting for the vehicle")
		}
		return err
	}

	total := max(1, (len(binary)+s.chunkSize-1)/s.chunkSize)
	for n := 1; n <= total; n++ {
		end := min(n*s.chunkSize, len(binary))
		req := vehiclepb.UploadMissionRequest_builder{
			Filename: name,
			Chunk:    binary[(n-1)*s.chunkSize : end],
			Number:   int32(n),
			Total:    int32(total),
		}.Build()
		err := wait(func() error { return stream.Send(req) })
		if errors.Is(err, io.EOF) {
			// The stream ended; Recv has the status it ended with
			_, err = stream.Recv()
			if err == nil || errors.Is(err, io.EOF) {
				err = status.Error(codes.Unknown, "vehicle ended the upload early")
			}
		}
		if err != nil {
			return err
		}
		if n == total {
			break
		}
		if err := wait(func() error { _, err := stream.Recv(); return err }); err != nil {
			if errors.Is(err, io.EOF) {
				return status.Errorf(codes.Unknown, "vehicle ended the upload after chunk %d of %d", n, total)
			}
			return err
		}
		if progress != nil {
			progress(n, total)
		}
	}
	if err := wait(stream.CloseSend); err != nil {
		return err
	}
	// The vehicle acknowledges the last chunk, then ends the stream
	for {
		err := wait(func() error { _, err := stream.Recv(); return err })
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
	}
	if progress != nil {
		progress(total, total)
	}
	return nil
}

// describe formats a gRPC error as "<code> - <message>".
func describe(err error) string {
	st, ok := status.FromError(err)
	if !ok {
		if st = status.FromContextError(err); st.Code() == codes.Unknown {
			return fmt.Sprintf("%s - %s", codes.Unknown, err)
		}
	}
	return fmt.Sprintf("%s - %s", st.Code(), st.Message())
}
