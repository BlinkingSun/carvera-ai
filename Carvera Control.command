#!/bin/zsh
# macOS double-click launcher: start the Carvera Control server (if it is not already running) and open the UI.
# Elsewhere, run:  python3 -m carvera_control   and open http://127.0.0.1:17916/
cd "${0:A:h}"
PY="${CARVERA_PYTHON:-python3}"
PORT=${CARVERA_PORT:-17916}
mkdir -p data
if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/api/status"; then
  nohup "$PY" -m carvera_control --port "$PORT" >> data/server.log 2>&1 &
  for i in {1..30}; do curl -s -o /dev/null "http://127.0.0.1:$PORT/api/status" && break; sleep 0.2; done
fi
open "http://127.0.0.1:$PORT/"
