// Vehicle names from the server are service names: "<daemon address>.<name>",
// e.g. "100.64.1.5.cassowary" or "fd7a:115c::1.harpy". These helpers are for
// display only; keys, comparisons and requests keep the full name.

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

export function isIPv6(s) {
    if (!s.includes(':') || !/^[0-9a-fA-F:.]+$/.test(s)) return false;
    let head = s;
    let groups = 8;
    // An embedded IPv4 tail counts for two groups.
    const lastColon = s.lastIndexOf(':');
    const tail = s.slice(lastColon + 1);
    if (tail.includes('.')) {
        if (!IPV4.test(tail)) return false;
        head = s.slice(0, lastColon + 1) + '0:0';
    }
    const halves = head.split('::');
    if (halves.length > 2) return false;
    const parse = (h) => (h === '' ? [] : h.split(':'));
    const parts = halves.flatMap(parse);
    if (!parts.every((p) => /^[0-9a-fA-F]{1,4}$/.test(p))) return false;
    return halves.length === 2 ? parts.length < groups : parts.length === groups;
}

export function isIP(s) {
    return IPV4.test(s) || isIPv6(s);
}

// The longest prefix of name that is an IP address followed by a dot (and a
// non-empty remainder), or '' when there is none.
export function daemonOf(name) {
    if (typeof name !== 'string') return '';
    for (let i = name.lastIndexOf('.'); i > 0; i = name.lastIndexOf('.', i - 1)) {
        if (i < name.length - 1 && isIP(name.slice(0, i))) return name.slice(0, i);
    }
    return '';
}

export function displayName(name) {
    const daemon = daemonOf(name);
    return daemon ? name.slice(daemon.length + 1) : name;
}
