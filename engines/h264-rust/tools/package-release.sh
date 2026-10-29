#!/usr/bin/env bash
# Cross-compiles the engines for an aarch64 device in Docker (Dockerfile.aarch64), and writes the
# release files to dist/ (see tools/release-name.sh). CI runs this to make a release; attach the
# two files to the release tagged h264-rust-v<version>.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/release-name.sh"

IMAGE=h264-rust-aarch64
TARGET=aarch64-unknown-linux-gnu
dist=$package_dir/dist
cd "$package_dir"

docker build --quiet -t "$IMAGE" -f Dockerfile.aarch64 .
# A target directory of its own keeps the build scripts, linked against glibc 2.31, apart from the
# host's.
docker run --rm -u "$(id -u):$(id -g)" -v "$package_dir:/src" -w /src \
    -e CARGO_TARGET_DIR=/src/target/aarch64 -e HOME=/tmp "$IMAGE" \
    cargo build --release --target "$TARGET" --locked

built=$package_dir/target/aarch64/$TARGET/release
rm -rf "$dist"
mkdir -p "$dist"
# Sorted, owned by root and dated 0, so the same engines make the same tarball
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 \
    -C "$built" -cf - "${engines[@]}" | gzip -n >"$dist/$asset"
(cd "$dist" && sha256sum "$asset" >"$checksum")
echo "package-release: $dist/$asset"
cat "$dist/$checksum"
