#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
website_results="$(mktemp -d "${TMPDIR:-/tmp}/website-content-check.XXXXXX")"
website_py="${PYTHON:-.venv-pilot/bin/python}"
website_port="$($website_py -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export INFE_PILOT_PREVIEW=1
export INFE_PILOT_DB="$website_results/preview.sqlite3"
export INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
export INFE_PILOT_COOKIE_SECURE=0
export INFE_PILOT_ADMIN_EMAILS=boss@example.com
export PILOT_BASE="http://127.0.0.1:$website_port"
unset INFE_PILOT_ORIGIN INFE_PILOT_WECHAT_GROUP_IMG INFE_PILOT_WECHAT_GROUP_UNTIL
"$website_py" -m pilot_app.web --host 127.0.0.1 --port "$website_port" > "$website_results/server.log" 2>&1 &
website_pid=$!
trap 'kill "$website_pid" 2>/dev/null || true; wait "$website_pid" 2>/dev/null || true' EXIT
for website_attempt in {1..50}; do
  if curl -fsS "$PILOT_BASE/health" > /dev/null 2>&1; then break; fi
  sleep 0.1
done
"$website_py" tools/seed_preview.py "$INFE_PILOT_DB" --base "$PILOT_BASE" > "$website_results/seed.log" 2>&1
echo "Browser evidence: $website_results"
node tools/website_content_check.js 2>&1 | tee "$website_results/check.log"
