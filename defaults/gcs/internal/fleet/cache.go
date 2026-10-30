package fleet

import (
	"context"
	"sync"
	"time"

	daemonpb "github.com/cmusatyalab/steeleagle/api/go/steeleagle_protocol/v1/services/daemon"
	"google.golang.org/grpc"
)

// CachedDaemon is a daemon client whose ListServices calls share one call to
// the daemon, made at most once per TTL however many callers there are: the
// vehicle list every browser polls, the Orchestrate page, the flow catalog
// and mission uploads. Listing asks every remote daemon too, so without it
// the load on the daemon grows with every open GCS page. Its other calls go
// straight to the daemon.
//
// Callers share the response, so they must not change it.
type CachedDaemon struct {
	daemonpb.DaemonServiceClient
	ttl     time.Duration
	timeout time.Duration
	now     func() time.Time

	mu sync.Mutex
	// call is the latest call, in flight or done; nil before the first
	call *listCall
}

// listCall is one ListServices call to the daemon. Its fields are set before
// done is closed.
type listCall struct {
	done chan struct{}
	at   time.Time // when it answered
	resp *daemonpb.ListServicesResponse
	err  error
}

// NewCachedDaemon returns c with its listings cached for ttl. Each call to
// the daemon gets timeout, whatever the caller's deadline, since it answers
// every caller waiting on it.
func NewCachedDaemon(c daemonpb.DaemonServiceClient, ttl, timeout time.Duration) *CachedDaemon {
	return &CachedDaemon{DaemonServiceClient: c, ttl: ttl, timeout: timeout, now: time.Now}
}

// ListServices answers with the latest listing if it is less than the TTL
// old, waits for the call in flight if there is one, and otherwise lists
// again. A failure is shared like an answer, so a struggling daemon isn't
// asked again until the TTL is up. It returns early if ctx ends.
func (c *CachedDaemon) ListServices(ctx context.Context, in *daemonpb.ListServicesRequest, opts ...grpc.CallOption) (*daemonpb.ListServicesResponse, error) {
	c.mu.Lock()
	call := c.call
	if call == nil || (call.answered() && c.now().Sub(call.at) >= c.ttl) {
		call = &listCall{done: make(chan struct{})}
		c.call = call
		go c.list(call, in, opts)
	}
	c.mu.Unlock()
	select {
	case <-call.done:
		return call.resp, call.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (c *CachedDaemon) list(call *listCall, in *daemonpb.ListServicesRequest, opts []grpc.CallOption) {
	ctx, cancel := context.WithTimeout(context.Background(), c.timeout)
	defer cancel()
	call.resp, call.err = c.DaemonServiceClient.ListServices(ctx, in, opts...)
	call.at = c.now()
	close(call.done)
}

func (l *listCall) answered() bool {
	select {
	case <-l.done:
		return true
	default:
		return false
	}
}
