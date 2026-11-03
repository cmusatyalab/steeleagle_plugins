import { describe, it, expect } from 'vitest';
import { parsePackageUri, repoHref } from './packageUri.js';

describe('parsePackageUri', () => {
    it('splits repo, subdir and tag', () => {
        expect(parsePackageUri('https://github.com/cmusatyalab/steeleagle_plugins//defaults/h264@v4_backend_rework')).toEqual({
            name: 'h264', tag: 'v4_backend_rework', repo: 'https://github.com/cmusatyalab/steeleagle_plugins', subdir: 'defaults/h264',
        });
    });

    it('handles a bare host and no tag', () => {
        expect(parsePackageUri('github.com/cmusatyalab/steeleagle_plugins//defaults/h264')).toEqual({
            name: 'h264', tag: '', repo: 'github.com/cmusatyalab/steeleagle_plugins', subdir: 'defaults/h264',
        });
    });

    it('names a package without a subdir after its repo', () => {
        expect(parsePackageUri('https://github.com/a/my-plugin.git@main')).toMatchObject({ name: 'my-plugin', tag: 'main', subdir: '' });
    });

    it("doesn't take an ssh user for a tag", () => {
        expect(parsePackageUri('ssh://git@github.com/a/b//x')).toMatchObject({ name: 'x', tag: '', repo: 'ssh://git@github.com/a/b' });
        expect(parsePackageUri('git@github.com:a/b')).toMatchObject({ name: 'b', tag: '', repo: 'git@github.com:a/b' });
    });

    it('keeps a tag with a slash in it', () => {
        expect(parsePackageUri('https://github.com/a/b//c@feature/x')).toMatchObject({ name: 'c', tag: 'feature/x', subdir: 'c' });
    });
});

describe('repoHref', () => {
    it('links web URLs and bare hosts only', () => {
        expect(repoHref('https://github.com/a/b')).toBe('https://github.com/a/b');
        expect(repoHref('github.com/a/b')).toBe('https://github.com/a/b');
        expect(repoHref('git@github.com:a/b')).toBeNull();
        expect(repoHref('/srv/plugins')).toBeNull();
    });
});
