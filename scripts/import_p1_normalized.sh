#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 /path/to/P1_STANDARD_REPLACEMENT_NORMALIZED.json" >&2
  exit 1
fi

FILE="$1"
API_URL="${FG_API_URL:-http://localhost:18181}"

curl -s -X POST "${API_URL}/p1/import-normalized" \
  -F "file=@${FILE}" | jq
