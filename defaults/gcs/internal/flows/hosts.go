package flows

import (
	"fmt"
	"net"
	"sort"
	"strings"
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
