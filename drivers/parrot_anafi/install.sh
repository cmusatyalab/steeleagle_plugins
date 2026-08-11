#!/bin/bash
set -eu

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
	curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if ! command -v buf >/dev/null 2>&1; then
	mkdir -p "$HOME/.local/bin"
	curl -sSL "https://github.com/bufbuild/buf/releases/latest/download/buf-$(uname -s)-$(uname -m)" -o "$HOME/.local/bin/buf"
	chmod +x "$HOME/.local/bin/buf"
fi

buf generate
uv venv
uv pip install -e .
