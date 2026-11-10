#!/usr/bin/env bash
# Installs the Prometheus server of PROMETHEUS_VERSION into bin/, unless bin/ already has that
# version. It downloads the official release for this machine's architecture and checks it against
# the release's own SHA-256, pinned here.
#
# conda-forge's prometheus package is years behind (2.34), so it isn't used.
#
# PROMETHEUS_RELEASE_URL overrides the directory the tarball is downloaded from, e.g. a mirror or a
# file:// directory.
set -euo pipefail

PROMETHEUS_VERSION=3.5.0
# From https://github.com/prometheus/prometheus/releases/download/v3.5.0/sha256sums.txt
declare -A SHA256=(
    [amd64]=e811827af26d822afb09a4f28314f61b618b12cff5369835a67f674d8b46f39a
    [arm64]=173389cc42bf09c4e6e54cb53fa07a5a835d7c261e14775d2183181d6e385d1c
)

package_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bin=$package_dir/bin
stamp=$bin/.version
base=${PROMETHEUS_RELEASE_URL:-https://github.com/prometheus/prometheus/releases/download/v$PROMETHEUS_VERSION}

die() {
    echo "fetch-prometheus: $*" >&2
    exit 1
}

case $(uname -m) in
x86_64 | amd64) arch=amd64 ;;
aarch64 | arm64) arch=arm64 ;;
*) die "no Prometheus release for $(uname -m)" ;;
esac

if [[ -f $stamp && $(<"$stamp") == "$PROMETHEUS_VERSION" ]] && [[ -x $bin/prometheus ]]; then
    echo "fetch-prometheus: $PROMETHEUS_VERSION is already installed"
    exit 0
fi

name=prometheus-$PROMETHEUS_VERSION.linux-$arch
asset=$name.tar.gz
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "fetch-prometheus: downloading $base/$asset"
curl -fsSL -o "$work/$asset" "$base/$asset" || die "could not download $base/$asset"
echo "${SHA256[$arch]}  $asset" >"$work/checksum"
(cd "$work" && sha256sum --check --quiet checksum) || die "$asset does not match its checksum"

tar -xzf "$work/$asset" -C "$work"
[[ -x $work/$name/prometheus ]] || die "$asset has no prometheus binary"

# The stamp goes last, so a failure leaves no half install that the stamp vouches for.
mkdir -p "$bin"
rm -f "$stamp"
install -m 755 "$work/$name/prometheus" "$bin/prometheus"
echo "$PROMETHEUS_VERSION" >"$stamp"
echo "fetch-prometheus: installed $PROMETHEUS_VERSION"
