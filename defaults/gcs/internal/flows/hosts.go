package flows

import (
	"fmt"
	"net"
	"sort"
	"strings"

	"github.com/cmusatyalab/steeleagle_plugins/gcs/internal/fleet"
)

// SplitTemplateName splits a template name {host}.{name} at its last dot,
// since hosts (IPs and FQDNs) contain dots and names don't. ok is false if
// there is no dot or either side is empty.
func SplitTemplateName(name string) (host, bare string, ok bool) {
	i := strings.LastIndex(name, ".")
	if i <= 0 || i == len(name)-1 {
		return "", "", false
	}
	return name[:i], name[i+1:], true
}

// ResolveHost returns the IP of the daemon a template host names, using only
// the catalog. An IP literal names that daemon if listed; otherwise the host
// matches a hostname exactly, then by its first label, ignoring case.
func (c Catalog) ResolveHost(host string) (string, error) {
	if d, ok := c.Daemon(host); ok {
		return d.IP, nil
	}
	for _, label := range []bool{false, true} {
		if label && net.ParseIP(host) != nil {
			break // an IP has no short name
		}
		var ips []string
		for _, d := range c.Daemons {
			h, want := d.Hostname, host
			if label {
				h, want = firstLabel(h), firstLabel(host)
			}
			if h != "" && strings.EqualFold(h, want) {
				ips = append(ips, d.IP)
			}
		}
		sort.Strings(ips)
		switch {
		case len(ips) > 1:
			return "", fmt.Errorf("flow host %s is ambiguous: it matches %s and %s", host, ips[0], ips[1])
		case len(ips) == 1:
			return ips[0], nil
		}
	}
	return "", fmt.Errorf("flow host %s is not a known daemon", host)
}

// HostLabel is the name a daemon is shown and stored under in flows: its
// hostname if that resolves back to it and no unavailable daemon has the same
// hostname, exactly or by first label, else its IP.
func (c Catalog) HostLabel(d Daemon) string {
	if d.Hostname != "" && !c.clashesUnavailable(d.Hostname) {
		if ip, err := c.ResolveHost(d.Hostname); err == nil && ip == d.IP {
			return d.Hostname
		}
	}
	return d.IP
}

// clashesUnavailable reports whether an unavailable daemon's hostname is h,
// exactly or by first label, ignoring case.
func (c Catalog) clashesUnavailable(h string) bool {
	for _, u := range c.Unavailable {
		if u.Hostname != "" && (strings.EqualFold(u.Hostname, h) || strings.EqualFold(firstLabel(u.Hostname), firstLabel(h))) {
			return true
		}
	}
	return false
}

func firstLabel(h string) string {
	l, _, _ := strings.Cut(h, ".")
	return l
}

// Prefixes of names relative to a vehicle, as the vehicle's own flows name
// their nodes: one of its streams, an engine (or engine producer) on its
// daemon, or one on the orchestrator
const (
	relVehicle = "vehicle"
	relLocal   = "local"
	relRemote  = "remote"
)

// ForVehicle translates a name relative to a vehicle into a template name
// for the vehicle (full name) on the orchestrator at orchestratorIP:
// vehicle.<stream> to <vehicle>.<stream>, labeled by the vehicle's bare
// name; local.<rest> to <its daemon>.<rest>; and remote.<rest> to
// <orchestrator>.<rest>, each daemon named as the Flow Builder names it
// (HostLabel). Any other name is already a template name, and is returned
// as is.
func (c Catalog) ForVehicle(name, vehicle, orchestratorIP string) (string, error) {
	prefix, rest, ok := strings.Cut(name, ".")
	if !ok || rest == "" {
		return name, nil
	}
	label := func(ip string) (string, error) {
		d, ok := c.Daemon(ip)
		if !ok {
			return "", fmt.Errorf("%s: daemon %s is not running", name, ip)
		}
		return c.HostLabel(d) + "." + rest, nil
	}
	vehicleIP, bare := fleet.SplitName(vehicle)
	switch prefix {
	case relVehicle:
		return bare + "." + rest, nil
	case relLocal:
		if vehicleIP == "" {
			return "", fmt.Errorf("%s: vehicle %s has no daemon IP in its name", name, vehicle)
		}
		return label(vehicleIP)
	case relRemote:
		return label(orchestratorIP)
	}
	return name, nil
}
