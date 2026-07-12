#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export MERGED_APP_ROOT="$PWD"
exec "${PYTHON_BIN:-python3}" _runtime/reloader.py
