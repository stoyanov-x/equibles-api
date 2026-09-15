#!/usr/bin/env bash
#
# Local gate. Same checks CI runs, in the same order, so a green run here means a
# green run there.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-python3}"

echo "== ruff check"
"$PY" -m ruff check src tests

echo "== mypy (strict)"
"$PY" -m mypy

if command -v shellcheck >/dev/null 2>&1; then
  echo "== shellcheck"
  shellcheck ci-local.sh
else
  echo "== shellcheck (skipped, not installed)"
fi

echo "== pytest"
"$PY" -m pytest

echo "== OK"
