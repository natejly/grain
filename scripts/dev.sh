#!/usr/bin/env bash
# Start the API and web dev servers together with preflight checks.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

fail() {
  echo "error: $1" >&2
  exit 1
}

[ -x .venv/bin/uvicorn ] || fail "missing .venv/bin/uvicorn — run 'make install' first"
[ -d node_modules ] || fail "missing node_modules — run 'make install' first"

for port in "$API_PORT" "$WEB_PORT"; do
  pid="$(lsof -ti tcp:"$port" 2>/dev/null | head -1 || true)"
  if [ -n "$pid" ]; then
    fail "port $port is already in use by PID $pid ($(ps -o comm= -p "$pid" 2>/dev/null)) — stop it or set API_PORT/WEB_PORT"
  fi
done

trap 'kill 0 2>/dev/null' EXIT INT TERM

.venv/bin/uvicorn app.main:app --app-dir apps/api --reload --host 127.0.0.1 --port "$API_PORT" 2>&1 \
  | sed -u 's/^/[api] /' &

for _ in $(seq 1 30); do
  if curl -sf -m 2 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
    api_up=1
    break
  fi
  sleep 0.5
done
if [ -z "${api_up:-}" ]; then
  echo "error: API did not become healthy on port $API_PORT within 15s — see [api] logs above" >&2
  exit 1
fi
echo "[dev] API healthy at http://127.0.0.1:$API_PORT"

npx --yes pnpm@9.15.9 --filter @workspace/web exec next dev -p "$WEB_PORT" 2>&1 \
  | sed -u 's/^/[web] /' &

echo "[dev] Web starting at http://localhost:$WEB_PORT"
wait
