#!/usr/bin/env bash
# Compatibility wrapper. The implementation lives in the tested OMD CLI.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --locked --no-dev --project "$SCRIPT_DIR" omd android setup "$@"
