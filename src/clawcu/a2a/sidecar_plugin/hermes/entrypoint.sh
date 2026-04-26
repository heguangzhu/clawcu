#!/usr/bin/env bash
# Supervisor entrypoint for the a2a-enabled Hermes image.
#
# Starts the A2A sidecar in the background, then hands off to the stock
# Hermes entrypoint in the foreground. Sidecar dies with main; if only
# the sidecar crashes, main keeps running (A2A goes offline but the
# Hermes agent itself is unaffected).
set -e

export A2A_BIND_HOST="${A2A_BIND_HOST:-0.0.0.0}"
export A2A_BIND_PORT="${A2A_BIND_PORT:-9119}"
export A2A_SELF_NAME="${A2A_SELF_NAME:-$(hostname)}"
export A2A_SELF_ROLE="${A2A_SELF_ROLE:-Hermes-backed assistant}"
export A2A_SELF_SKILLS="${A2A_SELF_SKILLS:-chat,a2a.bridge}"
# Default the advertised endpoint host to 127.0.0.1 and port to whatever
# the container port maps to on the host. Override both via env.
A2A_ADVERTISE_HOST="${A2A_ADVERTISE_HOST:-127.0.0.1}"
A2A_ADVERTISE_PORT="${A2A_ADVERTISE_PORT:-9129}"
export A2A_SELF_ENDPOINT="${A2A_SELF_ENDPOINT:-http://$A2A_ADVERTISE_HOST:$A2A_ADVERTISE_PORT/a2a/send}"

python3 /opt/a2a/server.py &
SIDECAR_PID=$!

/opt/hermes/docker/entrypoint.sh "$@" &
MAIN_PID=$!

cleanup() {
    kill -TERM "$SIDECAR_PID" "$MAIN_PID" 2>/dev/null || true
}
trap cleanup INT TERM

wait "$MAIN_PID"
MAIN_EXIT=$?
cleanup
wait "$SIDECAR_PID" 2>/dev/null || true
exit "$MAIN_EXIT"
