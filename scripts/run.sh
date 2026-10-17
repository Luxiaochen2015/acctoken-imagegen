#!/usr/bin/env bash
# Entry point for the Acctoken image generation CLI.
#
# Resolves a Python interpreter that can actually import openai + Pillow, wherever
# the skill directory happens to live. See scripts/_resolve_python.sh for the order
# and the overrides (ACCTOKEN_IMAGEGEN_PYTHON / ACCTOKEN_IMAGEGEN_VENV /
# ACCTOKEN_IMAGEGEN_HOME / ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_resolve_python.sh
. "$DIR/_resolve_python.sh"

PY="$(imagegen_resolve_python "openai,PIL,httpx" "openai pillow httpx")"
exec "$PY" "$DIR/image_gen.py" "$@"
