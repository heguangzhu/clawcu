"""A2A sidecar-plugin assets bundled with clawcu.

"sidecar-plugin" (vs. a gateway-native plugin) describes the implementation:
each bundle here is a Dockerfile layer that bakes a **separate sidecar
process** (plus supervisor entrypoint) into the stock service image. The
sidecar listens on a neighbor port and speaks the A2A v0 protocol
(`GET /.well-known/agent-card.json`, `POST /a2a/send`); it does not touch
OpenClaw's or Hermes' own plugin systems.

The plugin fingerprint (``<clawcu_version>.<sha>``) is stamped into the image
via the ``CLAWCU_PLUGIN_VERSION`` build-arg. The trailing short-sha is a hash
over the on-disk plugin sources (Dockerfile, sidecar, entrypoint); if any of
those files change, the fingerprint changes and ``A2AImageBuilder`` bakes a
fresh image tag — even when the clawcu PyPI version is unchanged (e.g. an
editable dev install). This is the mechanism that closes review-5 P0-c.
"""
from __future__ import annotations

import functools
import hashlib
import os
import platform
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent


@functools.cache
def default_advertise_host() -> str:
    """Return the hostname other containers should use to reach *this* host.

    Order:
      1. ``$CLAWCU_A2A_ADVERTISE_HOST`` — site-wide override.
      2. ``host.docker.internal`` on Docker Desktop (macOS / Windows).
      3. ``127.0.0.1`` on plain Linux — caller passes ``--add-host`` so the
         magic DNS name resolves inside the container; the registered
         endpoint still has to be something peers can reach, and the
         existing integration tests all bind loopback.

    Review-9 P1-A3. Cached because detection does filesystem I/O.
    """
    override = (os.environ.get("CLAWCU_A2A_ADVERTISE_HOST") or "").strip()
    if override:
        return override
    if platform.system() == "Darwin":
        # Docker Desktop on macOS always routes host.docker.internal → host.
        return "host.docker.internal"
    # Windows users via Docker Desktop get the same treatment. On Linux we
    # fall back to loopback — the typical single-container test flow still
    # works, and multi-container deployments should pass --a2a-advertise-host
    # or set $CLAWCU_A2A_ADVERTISE_HOST to whatever the cluster uses.
    if platform.system() == "Windows":
        return "host.docker.internal"
    return "127.0.0.1"


def resolve_advertise_host(record) -> str:
    """Resolve the advertise-host for an instance record.

    The explicit per-record override wins over the process-wide default so
    users can pin a specific hostname (e.g. a LAN IP) regardless of where
    clawcu runs.
    """
    explicit = getattr(record, "a2a_advertise_host", None)
    if explicit:
        return str(explicit)
    return default_advertise_host()


def plugin_source_dir(service: str) -> Path:
    """Return the on-disk directory containing Dockerfile + sidecar for ``service``."""
    path = _PLUGIN_ROOT / service
    if not path.is_dir():
        raise ValueError(f"No bundled A2A plugin assets for service '{service}'.")
    return path


# Build-time noise that must NOT feed into the plugin fingerprint: touching
# these files should not trigger a fresh image bake. Review-8 P2-H.
# __init__.py is packaging metadata (needed for setuptools to include
# Dockerfile/*.sh/*.js as package-data) and is not runtime sidecar code.
_PLUGIN_SHA_IGNORED_DIRS = {"__pycache__", ".git", "node_modules"}
_PLUGIN_SHA_IGNORED_SUFFIXES = (".pyc", ".pyo")
_PLUGIN_SHA_IGNORED_NAMES = {"__init__.py"}


def plugin_source_sha(service: str) -> str:
    """Return a short hex digest over every file under the service's plugin dir.

    The digest folds in both file path (relative, posix) and byte content in
    sorted order, so reordering or renaming files invalidates the hash. Only
    files under the service subdir are considered — shared ``__init__.py``
    changes do not invalidate per-service fingerprints.

    Runtime artifacts (``__pycache__`` dirs, ``.pyc``/``.pyo`` bytecode,
    ``node_modules``, VCS metadata) are excluded so a transient import of
    the sidecar during CLI use doesn't invalidate the cached image tag.
    """
    source_dir = plugin_source_dir(service)
    hasher = hashlib.sha256()
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir)
        if any(part in _PLUGIN_SHA_IGNORED_DIRS for part in rel.parts):
            continue
        if path.suffix in _PLUGIN_SHA_IGNORED_SUFFIXES:
            continue
        if path.name in _PLUGIN_SHA_IGNORED_NAMES:
            continue
        hasher.update(rel.as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()[:10]


def plugin_fingerprint(service: str, clawcu_version: str) -> str:
    """Return the ``<clawcu_version>.<sha>`` fingerprint used for image tagging.

    Both ``clawcu_version`` and the plugin-source sha are included so a user
    can tell at a glance which CLI baked an image AND whether the sidecar
    sources have drifted since that CLI shipped.
    """
    return f"{clawcu_version}.{plugin_source_sha(service)}"
