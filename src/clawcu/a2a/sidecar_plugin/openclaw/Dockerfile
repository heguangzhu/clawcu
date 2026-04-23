# a2a-enabled OpenClaw image (baked by clawcu).
#
# Extends the stock OpenClaw image with:
#   - /opt/a2a/server.py           python sidecar (stdlib only)
#   - /usr/local/bin/entrypoint-a2a.sh   supervisor that runs both the
#                                        stock OpenClaw gateway and the
#                                        A2A sidecar in a single container.
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
# Copy the entire sidecar/ directory, not just server.py. The sidecar is
# split across multiple files (server.py, readiness.py, ratelimit.py, ...)
# and server.py imports them as siblings at __file__'s directory. Copying
# only server.py would leave the module-resolution chain broken at runtime.
COPY --chown=node:node sidecar/ /opt/a2a/

COPY entrypoint.sh /usr/local/bin/entrypoint-a2a.sh
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
