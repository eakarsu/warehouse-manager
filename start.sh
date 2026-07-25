#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export MERGED_APP_ROOT="$PWD"
export MERGED_HOST="${MERGED_HOST:-127.0.0.1}"
exec "${PYTHON_BIN:-python3}" _runtime/reloader.py
