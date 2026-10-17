#!/usr/bin/env bash
# Regenerate references/models.md from the relay's public pricing endpoint.
#
# Needs only httpx, so it asks the shared resolver for a lighter dependency set
# than run.sh. See scripts/_resolve_python.sh.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_resolve_python.sh
. "$DIR/_resolve_python.sh"

PY="$(imagegen_resolve_python "httpx" "httpx")"
exec "$PY" "$DIR/update_pricing.py" "$@"
