#!/usr/bin/env bash
# Prints the release of this package's version and the names of its files, for the scripts that
# build and fetch a release. Source it.
#
# A release is tagged h264-rust-v<version>, since the repository holds other plugins, and has the
# engines for a device with the same glibc and GStreamer as Dockerfile.aarch64: a tarball of
# h264-plugin, h264-encode and h264-decode, and its checksum.

package_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The version of the [package] table of Cargo.toml
version=$(sed -n '/^\[package\]/,/^\[/{s/^version = "\(.*\)"$/\1/p}' "$package_dir/Cargo.toml")
[[ -n $version ]] || {
    echo "could not read the version from $package_dir/Cargo.toml" >&2
    exit 1
}
tag=h264-rust-v$version
asset=h264-rust-$version-linux-aarch64.tar.gz
checksum=$asset.sha256
engines=(h264-plugin h264-encode h264-decode)
