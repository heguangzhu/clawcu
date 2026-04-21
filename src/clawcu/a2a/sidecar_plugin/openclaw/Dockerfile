# a2a-enabled OpenClaw image (baked by clawcu).
#
# Extends the stock OpenClaw image with:
#   - /opt/a2a/server.js           node sidecar (stdlib node:http)
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

RUN mkdir -p /opt/a2a && chown node:node /opt/a2a
# Copy the entire sidecar/ directory, not just server.js. The sidecar has
# been split across multiple files (readiness.js, ratelimit.js, logsink.js)
# as of review-8..10 and server.js `require`s them at __dirname. Copying
# only server.js would leave the module-resolution chain broken at runtime.
# Review-11 P2-D fix: this is a single COPY layer, so rebuild cost is the
# same as a single-file copy — we don't lose any cache granularity.
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
