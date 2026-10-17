#!/usr/bin/env bash
# Entry point for the chroma-key background removal helper.
#
# Needs only Pillow, so it asks the shared resolver for a lighter dependency set
# than run.sh does. See scripts/_resolve_python.sh.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_resolve_python.sh
. "$DIR/_resolve_python.sh"

PY="$(imagegen_resolve_python "PIL" "pillow")"
exec "$PY" "$DIR/remove_chroma_key.py" "$@"
