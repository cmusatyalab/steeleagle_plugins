#!/usr/bin/env bash
# Builds liburiel into the Pixi environment, unless the environment already has the build of the
# gabriel commit that go.mod pins. The stamp records that commit and lives inside the environment,
# so recreating the environment discards it along with the library.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
pin=$(sed -n 's|.*github.com/cmusatyalab/gabriel/go v[^ ]*-\([0-9a-f]\{12\}\)[[:space:]]*$|\1|p' "$here/../go.mod")
stamp="$CONDA_PREFIX/share/uriel-commit"

if [[ -n $pin ]] && pkg-config --exists uriel && [[ -f $stamp && $(<"$stamp") == "$pin" ]]; then
    echo "uriel: $pin is already built"
    exit 0
fi

"$here/uriel-install.sh" --prefix "$CONDA_PREFIX"
mkdir -p "$(dirname "$stamp")"
echo "$pin" >"$stamp"
