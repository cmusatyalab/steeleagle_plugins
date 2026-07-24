#!/bin/bash
INSTALL=false

while getopts "i" opt; do
    case "${opt}" in
        i)
            INSTALL=true
            ;;
        *)
            echo "Usage: $0 [-i]"
            exit 1
            ;;
    esac
done
shift "$((OPTIND-1))"

if [ "$INSTALL" = true ]; then
    buf generate
    uv pip install -e .
else
    uv run parrot-anafi "$@"
fi
