package fleet

import (
	"context"
	"net"
	"sync"
	"testing"

	telemetrypb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/messages/telemetry"
	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	vehiclepb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/vehicle"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// fakeDaemon stands in for the daemon. It lists services, and serves the
// data calls of the vehicles it routes to by their route key. Data calls to
// the vehicles in block wait until their context is done. Set the fields
// before the first call.
type fakeDaemon struct {
	daemonpb.UnimplementedDaemonServiceServer
	vehiclepb.UnimplementedDataServiceServer

	services []*daemonpb.Service
	// unavailable are the remote daemons ListServices lists as unavailable.
	unavailable []*daemonpb.Unavailable
	telemetry   map[string]*telemetrypb.Telemetry // by vehicle
	flows       map[string][]byte                 // by vehicle
	block       map[string]bool
	// blockTelemetry is like block, but only for GetTelemetry.
	blockTelemetry map[string]bool
	listErr        error         // returned by ListServices when set
	setErr         error         // returned by SetDataflow when set
	setGate        chan struct{} // if set, SetDataflow waits for it to be closed
	// getFail is how many more GetFlow calls fail, by vehicle.
	getFail map[string]int

	cc *grpc.ClientConn

	mu       sync.Mutex
	setCalls []string // vehicles SetDataflow was called for, in order
	getCalls []string // vehicles GetFlow was called for, in order
}

// newFakeDaemon serves a fake daemon on a bufconn, until the test ends.
func newFakeDaemon(t *testing.T) *fakeDaemon {
	t.Helper()
	d := &fakeDaemon{
		telemetry:      map[string]*telemetrypb.Telemetry{},
		flows:          map[string][]byte{},
		block:          map[string]bool{},
		blockTelemetry: map[string]bool{},
		getFail:        map[string]int{},
	}
	ln := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	daemonpb.RegisterDaemonServiceServer(server, d)
	vehiclepb.RegisterDataServiceServer(server, d)
	go server.Serve(ln)
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return ln.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	d.cc = conn
	return d
}

// conn returns a connection to the fake daemon.
func (d *fakeDaemon) conn() *grpc.ClientConn { return d.cc }

// route returns the vehicle a call is routed to, and waits if the vehicle is
// blocked.
func (d *fakeDaemon) route(ctx context.Context) (string, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	keys := md.Get(routeHeader)
	if len(keys) != 1 {
		return "", status.Errorf(codes.InvalidArgument, "got route keys %v, want one", keys)
	}
	d.mu.Lock()
	blocked := d.block[keys[0]]
	d.mu.Unlock()
	if blocked {
		<-ctx.Done()
		return "", status.FromContextError(ctx.Err()).Err()
	}
	return keys[0], nil
}

func (d *fakeDaemon) ListServices(context.Context, *daemonpb.ListServicesRequest) (*daemonpb.ListServicesResponse, error) {
	if d.listErr != nil {
		return nil, d.listErr
	}
	return daemonpb.ListServicesResponse_builder{Services: d.services, Unavailable: d.unavailable}.Build(), nil
}

func (d *fakeDaemon) GetFlow(ctx context.Context, _ *vehiclepb.GetFlowRequest) (*vehiclepb.GetFlowResponse, error) {
	v, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.getCalls = append(d.getCalls, v)
	if d.getFail[v] > 0 {
		d.getFail[v]--
		return nil, status.Error(codes.Unavailable, "vehicle unreachable")
	}
	return vehiclepb.GetFlowResponse_builder{Toml: d.flows[v]}.Build(), nil
}

func (d *fakeDaemon) SetDataflow(ctx context.Context, req *vehiclepb.SetDataflowRequest) (*vehiclepb.SetDataflowResponse, error) {
	v, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	d.mu.Lock()
	d.setCalls = append(d.setCalls, v)
	gate, setErr := d.setGate, d.setErr
	d.mu.Unlock()
	if gate != nil {
		<-gate
	}
	if setErr != nil {
		return nil, setErr
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.flows[v] = req.GetToml()
	return &vehiclepb.SetDataflowResponse{}, nil
}

func (d *fakeDaemon) GetTelemetry(ctx context.Context, _ *vehiclepb.GetTelemetryRequest) (*vehiclepb.GetTelemetryResponse, error) {
	if md, ok := metadata.FromIncomingContext(ctx); ok {
		if keys := md.Get(routeHeader); len(keys) == 1 {
			d.mu.Lock()
			blocked := d.blockTelemetry[keys[0]]
			d.mu.Unlock()
			if blocked {
				<-ctx.Done()
				return nil, status.FromContextError(ctx.Err()).Err()
			}
		}
	}
	v, err := d.route(ctx)
	if err != nil {
		return nil, err
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	var out []*telemetrypb.Telemetry
	if tel, ok := d.telemetry[v]; ok {
		out = append(out, tel)
	}
	return vehiclepb.GetTelemetryResponse_builder{Telemetry: out}.Build(), nil
}
