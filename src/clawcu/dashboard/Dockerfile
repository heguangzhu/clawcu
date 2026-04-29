# ClawCU Dashboard — standalone Docker image for the web dashboard.
#
# Build context: project root (so src/ and pyproject.toml are available).
#
# The dashboard server needs:
#   - Python runtime + clawcu package (for ClawCUService, data collection)
#   - docker CLI (to talk to the host Docker daemon via mounted socket)
#   - Static HTML pages (baked into the Python package via importlib.resources)
#
# Runtime mounts expected from the host:
#   - ~/.clawcu          → /root/.clawcu     (StateStore data)
#   - /var/run/docker.sock → /var/run/docker.sock (DockerManager)
#
FROM python:3.11-slim

# Install docker CLI only (no daemon).
# Use the official Docker apt repository for a recent, stable client.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/clawcu
COPY . /opt/clawcu/
RUN pip install --no-cache-dir -e /opt/clawcu/

ENV CLAWCU_IN_DOCKER=1
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health', timeout=3)" || exit 1

ENTRYPOINT ["python", "-m", "clawcu.dashboard.docker_entrypoint"]
