# a2a-enabled Hermes image (baked by clawcu).
#
# Extends the stock Hermes image with:
#   - /opt/a2a/sidecar.py                stdlib-Python A2A sidecar
#   - /usr/local/bin/entrypoint-a2a.sh   supervisor that runs both the
#                                        stock Hermes gateway and the
#                                        A2A sidecar in a single container.
#
# Build args:
#   HERMES_VERSION         base image tag (default v2026.4.8)
#   HERMES_IMAGE_REPO      base image repo  (default clawcu/hermes-agent)
#   CLAWCU_PLUGIN_VERSION  version of the clawcu CLI that baked this image
#                          — stamped into the sidecar's /healthz payload so
#                          callers can verify the plugin was built at the
#                          same release as the clawcu that installs it.
ARG HERMES_VERSION=v2026.4.8
ARG HERMES_IMAGE_REPO=clawcu/hermes-agent
FROM ${HERMES_IMAGE_REPO}:${HERMES_VERSION}

ARG CLAWCU_PLUGIN_VERSION=unknown

# bootstrap.py rewrites /opt/data/config.yaml via PyYAML to auto-wire the
# A2A MCP server entry. The hermes base image ships python3 but no PyYAML
# and no pip, so without this step the sidecar logs
#   WARNING a2a-sidecar: PyYAML unavailable — cannot handle YAML MCP config
# and MCP auto-wire is silently skipped (review-10 P0-A4). We install the
# Debian system package rather than pip to avoid bringing in pip just for
# one dependency. Keep apt lists clean to minimise layer size.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-yaml \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/a2a
COPY sidecar.py         /opt/a2a/sidecar.py
# bootstrap.py and outbound_limit.py are loaded at runtime by
# sidecar.py via spec_from_file_location. They must land in the
# image alongside sidecar.py — else the sidecar silently skips
# MCP auto-wire and outbound rate limiting (review-9 P0-A3).
COPY bootstrap.py       /opt/a2a/bootstrap.py
COPY outbound_limit.py  /opt/a2a/outbound_limit.py
COPY entrypoint.sh /usr/local/bin/entrypoint-a2a.sh
RUN chmod +x /usr/local/bin/entrypoint-a2a.sh

# Container port the sidecar binds. Publish at run time (e.g. -p 9129:9119).
EXPOSE 9119

ENV A2A_BIND_PORT=9119 \
    A2A_SELF_ROLE="Hermes-backed assistant" \
    A2A_SELF_SKILLS="chat,a2a.bridge" \
    CLAWCU_PLUGIN_VERSION=${CLAWCU_PLUGIN_VERSION}

ENTRYPOINT ["/usr/local/bin/entrypoint-a2a.sh"]
CMD ["gateway", "run"]
