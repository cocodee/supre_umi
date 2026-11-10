#!/usr/bin/env bash
# Start the UMI Studio web console. All arguments are passed to umi_web.py.
#   ./start_web.sh                       # local only, http://127.0.0.1:8770
#   ./start_web.sh --host 0.0.0.0        # LAN access (no auth by default!)
#   ./start_web.sh --auth umi:secret     # HTTP Basic auth
set -euo pipefail
cd "$(dirname "$0")"
exec python3 umi_web.py "$@"
