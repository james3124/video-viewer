#!/bin/sh
# Restart the viewer backend without the classic footgun: never `pkill -f
# server.py` (the pattern matches the shell running this script). Scan /proc
# cmdlines for python processes running THIS checkout's server.py instead.
#
#   tools/dev_restart.sh                # stop + start
#   tools/dev_restart.sh --purge-cache  # also drop extraction/playlist caches
#
# Binds 0.0.0.0:8080 by default — that is what lets the phone open
# http://<lan-ip>:8080/. Override with VIEWER_HOST / VIEWER_PORT (e.g.
# VIEWER_HOST=127.0.0.1 for this machine only).
#
# Logs go to ${VIEWER_LOG:-$TMPDIR/viewer.log}; the pid to ${VIEWER_PID:-$TMPDIR/viewer.pid}.
set -eu
APP=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY=${VIEWER_PYTHON:-$APP/.venv/bin/python}
LOG=${VIEWER_LOG:-${TMPDIR:-/tmp}/viewer.log}
PIDFILE=${VIEWER_PID:-${TMPDIR:-/tmp}/viewer.pid}

for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  [ -r "$d/cmdline" ] || continue
  # shellcheck disable=SC2002
  tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q "$APP/server\.py" || continue
  [ "$pid" = "$$" ] && continue
  if kill "$pid" 2>/dev/null; then echo "stopped $pid"; fi
done
sleep 1

if [ "${1:-}" = "--purge-cache" ]; then
  rm -rf "${VIEWER_CACHE:-${TMPDIR:-/tmp}/viewer-cache}"
  echo "cache purged"
fi

cd "$APP"
HOST=${VIEWER_HOST:-0.0.0.0}
PORT=${VIEWER_PORT:-8080}
# setsid: leave this shell's process group, so a terminal/agent teardown does
# not take the backend with it. Plain nohup survives SIGHUP but not a group kill.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup "$PY" "$APP/server.py" --host "$HOST" --port "$PORT" \
    >>"$LOG" 2>&1 </dev/null &
else
  nohup "$PY" "$APP/server.py" --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
fi
echo $! > "$PIDFILE"
echo "started pid $(cat "$PIDFILE") (log: $LOG)"
