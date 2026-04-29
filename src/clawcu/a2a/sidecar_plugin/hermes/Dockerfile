# a2a-enabled Hermes image (baked by clawcu).
#
# Extends the stock Hermes image with:
#   - /opt/a2a/server.py                 stdlib-Python A2A sidecar (entry)
#   - /opt/a2a/config.py                 runtime env-var config
#   - /opt/a2a/gateway.py                co-resident Hermes gateway surface
#                                        (readiness probe + /v1/chat/completions)
#   - /opt/a2a/peering.py                outbound peer/registry helpers
#   - /opt/a2a/inbound_limits.py         body-size / hop-budget / content-length
#                                        reject-early guards
#   - /opt/a2a/_common/                  stdlib primitives shared with the
#                                        OpenClaw sidecar (streams, ratelimit,
#                                        readiness, thread store, MCP dispatch)
#   - /usr/local/bin/entrypoint-a2a.sh   supervisor that runs both the
#                                        stock Hermes gateway and the
#                                        A2A sidecar in a single container.
#
# The COPY below grabs the entire sidecar/ dir so new sibling modules
# land under /opt/a2a/ automatically — no Dockerfile edit needed.
#
# Build args:
#   HERMES_VERSION         base image tag (default v2026.4.8)
#   HERMES_IMAGE_REPO      base image repo  (default clawcu/hermes-agent)
#   HERMES_RUNTIME_USER    user to run the final image as (default ``root``,
#                          the user the upstream image runs as up through
#                          v2026.4.22). ``A2AImageBuilder`` overrides this
#                          to ``hermes`` for v2026.4.23+ where the upstream
#                          switched to a non-root user. Defaulting to root
#                          keeps the bake working on older base tags whose
#                          image has no ``hermes`` account — forcing
#                          ``USER hermes`` there makes the container fail
#                          at startup with "unable to find user hermes".
#   CLAWCU_PLUGIN_VERSION  version of the clawcu CLI that baked this image
#                          — stamped into the sidecar's /healthz payload so
#                          callers can verify the plugin was built at the
#                          same release as the clawcu that installs it.
ARG HERMES_VERSION=v2026.4.8
ARG HERMES_IMAGE_REPO=clawcu/hermes-agent
FROM ${HERMES_IMAGE_REPO}:${HERMES_VERSION}

ARG CLAWCU_PLUGIN_VERSION=unknown
ARG HERMES_RUNTIME_USER=root

# bootstrap.py rewrites /opt/data/config.yaml via PyYAML to auto-wire the
# A2A MCP server entry. The hermes base image ships python3 but no PyYAML
# and no pip, so without this step the sidecar logs
#   WARNING a2a-sidecar: PyYAML unavailable — cannot handle YAML MCP config
# and MCP auto-wire is silently skipped (review-10 P0-A4). We install the
# Debian system package rather than pip to avoid bringing in pip just for
# one dependency. Keep apt lists clean to minimise layer size.
#
# The upstream hermes-agent image switched its default USER from ``root``
# to ``hermes`` starting in v2026.4.23 — apt-get / mkdir / chmod under that
# user fail with ``permission denied``. Switch to root for the privileged
# steps and let HERMES_RUNTIME_USER (set by A2AImageBuilder based on the
# base tag) decide which user owns the runtime layer below.
USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-yaml \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/a2a
# Build context is sidecar_plugin/ (one level up from this Dockerfile) so we
# can COPY both the hermes-specific sidecar sources and the shared _common/
# package baked beside them. server.py prepends __file__'s directory to
# sys.path at startup, which then makes `import _common.xxx` and sibling
# imports (e.g. `from gateway import ...`) resolve against /opt/a2a/.
COPY hermes/sidecar/     /opt/a2a/
COPY _common/            /opt/a2a/_common/
COPY hermes/entrypoint.sh /usr/local/bin/entrypoint-a2a.sh
RUN chmod +x /usr/local/bin/entrypoint-a2a.sh
USER ${HERMES_RUNTIME_USER}

# Container port the sidecar binds. Publish at run time (e.g. -p 9129:9119).
EXPOSE 9119

ENV A2A_BIND_PORT=9119 \
    A2A_SELF_ROLE="Hermes-backed assistant" \
    A2A_SELF_SKILLS="chat,a2a.bridge" \
    CLAWCU_PLUGIN_VERSION=${CLAWCU_PLUGIN_VERSION}

ENTRYPOINT ["/usr/local/bin/entrypoint-a2a.sh"]
CMD ["gateway", "run"]
