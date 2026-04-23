# a2a-enabled OpenClaw image (baked by clawcu).
#
# Extends the stock OpenClaw image with:
#   - /opt/a2a/server.py                 stdlib-Python A2A sidecar (entry)
#   - /opt/a2a/adapters.py               config-source adapters
#                                        (HostAdapter / LocalAdapter +
#                                        read_gateway_auth)
#   - /opt/a2a/http_client.py            outbound HTTP helpers
#                                        (parse_http_url, post_json,
#                                        http_request_raw, capped read)
#   - /opt/a2a/logsink.py                stdout + optional file-tee logger
#   - /opt/a2a/readiness.py              gateway readiness wait / invalidation
#   - /opt/a2a/_common/                  stdlib primitives shared with the
#                                        Hermes sidecar (streams, ratelimit,
#                                        readiness, thread store, MCP
#                                        dispatch)
#   - /usr/local/bin/entrypoint-a2a.sh   supervisor that runs both the
#                                        stock OpenClaw gateway and the
#                                        A2A sidecar in a single container.
#
# The COPY below grabs the entire sidecar/ dir so new sibling modules
# land under /opt/a2a/ automatically — no Dockerfile edit needed.
#
# Build args:
#   OPENCLAW_VERSION        base image tag (default 2026.4.12)
#   OPENCLAW_IMAGE_REPO     base image repo  (default ghcr.io/openclaw/openclaw)
#   CLAWCU_PLUGIN_VERSION   version of the clawcu CLI that baked this image
#                           — stamped into the sidecar's /health payload so
#                           callers can verify the plugin was built at the
#                           same release as the clawcu that installs it.
ARG OPENCLAW_VERSION=2026.4.12
ARG OPENCLAW_IMAGE_REPO=ghcr.io/openclaw/openclaw
FROM ${OPENCLAW_IMAGE_REPO}:${OPENCLAW_VERSION}

ARG CLAWCU_PLUGIN_VERSION=unknown

USER root

# The stock OpenClaw image is node-based; the a2a sidecar now runs on
# Python 3 (stdlib only) so we share a single language with the hermes
# sidecar. Install python3 via apt (~15-20 MB); no pip wheels required.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/a2a && chown node:node /opt/a2a
# Build context is sidecar_plugin/ (one level up from this Dockerfile) so we
# can COPY both the service-specific sidecar sources and the shared _common/
# package baked beside them. server.py prepends __file__'s directory to
# sys.path at startup, which then makes `import _common.xxx` resolve against
# /opt/a2a/_common/.
COPY --chown=node:node openclaw/sidecar/ /opt/a2a/
COPY --chown=node:node _common/          /opt/a2a/_common/

COPY openclaw/entrypoint.sh /usr/local/bin/entrypoint-a2a.sh
RUN chmod +x /usr/local/bin/entrypoint-a2a.sh

USER node

# A2A sidecar port inside the container. Publish it at run time (e.g.
# OpenClaw gateway 18789 → host 18819, A2A 18790 → host 18820).
EXPOSE 18790

ENV A2A_SIDECAR_PORT=18790 \
    A2A_SIDECAR_ROLE="OpenClaw agent" \
    A2A_SIDECAR_SKILLS="chat,reason" \
    CLAWCU_PLUGIN_VERSION=${CLAWCU_PLUGIN_VERSION}

ENTRYPOINT ["/usr/local/bin/entrypoint-a2a.sh"]
CMD ["node", "openclaw.mjs", "gateway", "--allow-unconfigured"]
