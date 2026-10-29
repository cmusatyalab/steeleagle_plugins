#!/usr/bin/env bash
# Installs the prebuilt engines of this package's version into bin/, unless bin/ already has that
# version. They are built by tools/package-release.sh and attached to the package's release on
# GitHub (see tools/release-name.sh), which this downloads and checks against its checksum.
#
# The stamp that records the version lives in bin/, so deleting bin/ discards it with the engines.
#
# H264_RUST_RELEASE_URL overrides the directory the files are downloaded from, e.g. a mirror or a
# file:// directory.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/release-name.sh"

REPO=cmusatyalab/steeleagle_plugins
base=${H264_RUST_RELEASE_URL:-https://github.com/$REPO/releases/download/$tag}
bin=$package_dir/bin
stamp=$bin/.version

die() {
    echo "fetch-binary: $*" >&2
    exit 1
}

case $(uname -m) in
aarch64 | arm64) ;;
*) die "the prebuilt engines are for aarch64, not $(uname -m)" ;;
esac

if [[ -f $stamp && $(<"$stamp") == "$version" ]] && [[ -x $bin/h264-plugin ]]; then
    echo "fetch-binary: $version is already installed"
    exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "fetch-binary: downloading $base/$asset"
curl -fsSL -o "$work/$asset" "$base/$asset" || die "could not download $base/$asset"
curl -fsSL -o "$work/$checksum" "$base/$checksum" || die "could not download $base/$checksum"
(cd "$work" && sha256sum --check --quiet "$checksum") || die "$asset does not match its checksum"

tar -xzf "$work/$asset" -C "$work"
for engine in "${engines[@]}"; do
    [[ -x $work/$engine ]] || die "$asset has no $engine"
done

# Replace the engines together, and the stamp last, so a failure leaves no half install that the
# stamp vouches for.
mkdir -p "$bin"
rm -f "$stamp"
for engine in "${engines[@]}"; do
    install -m 755 "$work/$engine" "$bin/$engine"
done
echo "$version" >"$stamp"
echo "fetch-binary: installed $version"
