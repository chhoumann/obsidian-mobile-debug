#!/usr/bin/env bash
# Compatibility wrapper. The implementation lives in the tested OMD CLI.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m obsidian_mobile_debug.cli android setup "$@"
