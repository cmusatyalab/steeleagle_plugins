#!/usr/bin/env bash
# Installs liburiel into the Pixi environment, unless the environment already has the Uriel
# release that go.mod requires. The stamp records that version and lives inside the environment,
# so recreating the environment discards it along with the library.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
pin=$(sed -n 's|^[[:space:]]*github.com/cmusatyalab/uriel/go \(v[^ ]*\).*|\1|p' "$here/../go.mod")
stamp="$CONDA_PREFIX/share/uriel-version"

if [[ -n $pin ]] && pkg-config --exists uriel && [[ -f $stamp && $(<"$stamp") == "$pin" ]]; then
    echo "uriel: $pin is already installed"
    exit 0
fi

"$here/uriel-install.sh" --prefix "$CONDA_PREFIX"
mkdir -p "$(dirname "$stamp")"
echo "$pin" >"$stamp"
