#!/usr/bin/env bash
set -euo pipefail
curl -s http://localhost:18181/health | jq
