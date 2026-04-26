#!/usr/bin/env bash
# Supervisor entrypoint for the a2a-enabled OpenClaw image.
#
# Starts the A2A sidecar in the background, then the stock OpenClaw
# gateway in the foreground. If either dies, we tear down the other and
# exit with the main process's code so Docker sees a normal shutdown.
set -e

A2A_PORT="${A2A_SIDECAR_PORT:-18790}"
A2A_NAME="${A2A_SIDECAR_NAME:-$(hostname)}"
A2A_ROLE="${A2A_SIDECAR_ROLE:-OpenClaw agent \"$A2A_NAME\"}"
A2A_SKILLS="${A2A_SIDECAR_SKILLS:-chat,reason}"

# Let the sidecar self-report an endpoint the registry/CLI will see from
# the host. This is the host-side address the container's A2A port maps to.
A2A_ADVERTISE_HOST="${A2A_SIDECAR_ADVERTISE_HOST:-127.0.0.1}"

# Port the sidecar advertises in its AgentCard. Must match whatever the
# host maps our container port to (e.g. `docker run -p 18820:18790`).
A2A_ADVERTISE_PORT="${A2A_SIDECAR_ADVERTISE_PORT:-$A2A_PORT}"

python3 /opt/a2a/server.py --local \
    --port "$A2A_PORT" \
    --name "$A2A_NAME" \
    --role "$A2A_ROLE" \
    --skills "$A2A_SKILLS" \
    --advertise-host "$A2A_ADVERTISE_HOST" \
    --advertise-port "$A2A_ADVERTISE_PORT" &
SIDECAR_PID=$!

# Stock OpenClaw entrypoint, started as a child so we can watch both.
/usr/local/bin/docker-entrypoint.sh "$@" &
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
