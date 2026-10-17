#!/usr/bin/env bash
# Interpreter resolver shared by scripts/run.sh and scripts/run_chroma.sh.
#
# Agent-neutral on purpose: this file must not assume Codex, Pi, Claude Code or any
# other harness, and must not hardcode a per-agent skills directory. The skill is
# found from its own location (`BASH_SOURCE`), so the directory can live anywhere
# and can be symlinked or copied under any agent's skills root.
#
# Layout contract
#   <skill dir>/            docs + scripts only (small, safe to copy/sync)
#   <runtime dir>/venvs/acctoken-imagegen/
#                           host-local Python environment (34MB, not part of the skill)
#   $ACCTOKEN_IMAGEGEN_HOME defaults to "$HOME/.agents" (the portable Agent Skills
#   home) and only needs overriding if you keep runtime dirs elsewhere.
#
# Resolution order — the first interpreter that can IMPORT the required modules wins:
#   1. $ACCTOKEN_IMAGEGEN_PYTHON   explicit interpreter, always respected
#   2. $ACCTOKEN_IMAGEGEN_VENV     explicit venv directory
#   3. <skill dir>/.venv           a venv bundled inside the skill (legacy/optional)
#   4. $ACCTOKEN_IMAGEGEN_HOME/venvs/acctoken-imagegen   shared host-local venv
#   5. python3 / python on PATH    if the dependencies are already importable
#   6. bootstrap with uv           creates (4) once, then re-checks
#
# "Executable" is not the test. A venv copied between paths can contain a python
# binary that is chmod +x but dead on arrival (stale dylib/interpreter references),
# which is exactly how this skill broke once before. Every candidate is probed with
# a real import, and the venv symlink is resolved rather than trusted.

# Required modules for each entry point, passed by the caller:
#   image_gen.py needs both; remove_chroma_key.py only needs Pillow.
imagegen_probe() {
  # $1 = interpreter path, $2 = comma separated import names
  local py="$1" mods="$2" test=""
  [ -n "$py" ] || return 1
  [ -x "$py" ] || return 1
  local IFS=','
  for m in $mods; do
    test="$test
import ${m}"
  done
  "$py" -c "$test" >/dev/null 2>&1
}

imagegen_bootstrap() {
  # $1 = venv directory, $2 = pip package names to install.
  # Creates the shared venv if needed, and tops up an existing one that is missing a
  # package (an unpinned `openai` upgrade can drop a transitive dep, e.g. openai 3.x
  # ships httpx2 while the CLI imports httpx). Network is required, so this is the
  # last resort, never the first move. Disable with ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP=1.
  local venv="$1" packages="$2"
  [ "${ACCTOKEN_IMAGEGEN_NO_BOOTSTRAP:-0}" = "1" ] && return 1
  command -v uv >/dev/null 2>&1 || return 1

  if [ ! -x "$venv/bin/python" ]; then
    echo "acctoken-imagegen: no working Python environment found; creating $venv with uv" >&2
    # --relocatable keeps the venv usable if the directory is moved later on.
    uv venv --relocatable "$venv" >&2 || return 1
  fi
  echo "acctoken-imagegen: ensuring dependencies [$packages] in $venv" >&2
  uv pip install --python "$venv/bin/python" $packages >&2 || return 1
  return 0
}

imagegen_resolve_python() {
  # $1 = comma separated import names required by the entry point
  # $2 = pip package names to install if bootstrapping is needed
  local mods="$1"
  local packages="${2:-openai pillow httpx}"
  local script_dir skill_dir runtime_home venv
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  skill_dir="$(dirname "$script_dir")"
  runtime_home="${ACCTOKEN_IMAGEGEN_HOME:-$HOME/.agents}"
  venv=""

  # 1. explicit interpreter
  if [ -n "${ACCTOKEN_IMAGEGEN_PYTHON:-}" ] && imagegen_probe "$ACCTOKEN_IMAGEGEN_PYTHON" "$mods"; then
    printf '%s\n' "$ACCTOKEN_IMAGEGEN_PYTHON"
    return 0
  fi

  # 2-4. candidate environments, in order
  local candidates=""
  [ -n "${ACCTOKEN_IMAGEGEN_VENV:-}" ] && candidates="$ACCTOKEN_IMAGEGEN_VENV/bin/python"
  candidates="$candidates
$skill_dir/.venv/bin/python
$runtime_home/venvs/acctoken-imagegen/bin/python"

  local py
  local IFS=$'\n'
  for py in $candidates; do
    [ -n "$py" ] || continue
    if imagegen_probe "$py" "$mods"; then
      printf '%s\n' "$py"
      return 0
    fi
  done
  unset IFS

  # 5. system interpreters that already have the dependencies
  for py in python3 python; do
    local real=""
    real="$(command -v "$py" 2>/dev/null || true)"
    [ -n "$real" ] || continue
    if imagegen_probe "$real" "$mods"; then
      printf '%s\n' "$real"
      return 0
    fi
  done

  # 6. last resort: create or top up the shared venv, then use it if it works
  venv="$runtime_home/venvs/acctoken-imagegen"
  if imagegen_bootstrap "$venv" "$packages"; then
    py="$venv/bin/python"
    if imagegen_probe "$py" "$mods"; then
      printf '%s\n' "$py"
      return 0
    fi
  fi

  cat >&2 <<EOF
acctoken-imagegen: no Python interpreter can import [$mods].

Fix (pick one, nothing here is tied to a specific agent):
  1. Bootstrap the shared environment with uv:
       uv venv --relocatable $runtime_home/venvs/acctoken-imagegen
       uv pip install --python $runtime_home/venvs/acctoken-imagegen/bin/python $packages
  2. Point at an environment you already have:
       export ACCTOKEN_IMAGEGEN_VENV=/path/to/.venv
  3. Point at an interpreter directly:
       export ACCTOKEN_IMAGEGEN_PYTHON=/path/to/python
EOF
  return 127
}
