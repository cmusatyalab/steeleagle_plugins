// Splits a package URI, repo[//subdir][@version], into its parts for display:
// { name, tag, repo, subdir }. The name is the subdir's last segment, or the
// repo's own name when there's no subdir; tag is '' when the URI pins none.
export function parsePackageUri(uri) {
    let rest = (uri ?? '').trim();
    // A scheme's "://" isn't the subdir separator, so look past it
    const scheme = rest.match(/^[a-z][a-z0-9+.-]*:\/\//i)?.[0] ?? '';
    let body = rest.slice(scheme.length);

    // The last '@' is the version only once it's past the host, so the user
    // in ssh://git@host/... or git@host:org/repo isn't mistaken for one
    let tag = '';
    const at = body.lastIndexOf('@');
    if (at > 0 && body.slice(0, at).includes('/')) {
        tag = body.slice(at + 1);
        body = body.slice(0, at);
    }

    let subdir = '';
    const sep = body.indexOf('//');
    if (sep >= 0) {
        subdir = body.slice(sep + 2).replace(/^\/+|\/+$/g, '');
        body = body.slice(0, sep);
    }
    const repo = scheme + body.replace(/\/+$/, '');

    const lastSegment = (path) => path.split(/[/:]/).filter(Boolean).pop() ?? '';
    const name = lastSegment(subdir) || lastSegment(body).replace(/\.git$/, '') || uri;
    return { name, tag, repo, subdir };
}

// The repo as a link target, for repos given as web URLs or bare hosts
export function repoHref(repo) {
    if (/^https?:\/\//i.test(repo)) return repo;
    if (/^[\w.-]+\.[a-z]{2,}\//i.test(repo)) return `https://${repo}`;
    return null;
}
