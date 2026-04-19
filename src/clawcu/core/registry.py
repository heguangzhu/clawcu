"""Best-effort remote tag discovery for Docker registries.

This module implements the read-only slice of the Docker Registry HTTP
API v2 (`/v2/<repo>/tags/list`) needed to answer the question:

    "What upstream versions could I upgrade this instance to?"

It is deliberately **best-effort**: every failure mode (network error,
4xx/5xx, malformed payload, missing auth for a private repo) returns a
structured ``RemoteTagResult`` with ``tags=None`` and a human-readable
``error``. The caller decides whether to surface the error or silently
fall back to the local/history view.

Supported registries (auto-detected from the repo string):

- Docker Hub: ``repo``, ``library/repo``, ``docker.io/repo``
- GHCR: ``ghcr.io/owner/repo``, plus known CN mirrors
  (``ghcr.nju.edu.cn`` etc.) which proxy the same v2 API

Anonymous pull tokens are negotiated via the registry's
``WWW-Authenticate`` challenge, matching what ``docker pull`` itself
does for public images. Private repos would need explicit credentials
and are intentionally left to a future iteration.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from clawcu import __version__ as clawcu_version

_LOG = logging.getLogger(__name__)


_SEMVER_SORT_KEY_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)

# Stable releases should sort AFTER all prereleases of the same (X, Y, Z) per
# SemVer; tuples compare element-wise, so a larger sentinel for stable (1) and
# a smaller one for prerelease (0) achieves that with tuples alone.
_PRERELEASE_WEIGHT = 0
_STABLE_WEIGHT = 1


def semver_sort_key(tag: str) -> tuple:
    """Sort key for version-like tags that puts newest releases last.

    Accepts ``X.Y.Z`` or ``X.Y.Z-pre`` (with leading ``v`` tolerated).
    Unrecognized tags fall back to a low-sort bucket so they don't
    masquerade as "most recent".
    """
    value = tag.lstrip("v")
    match = _SEMVER_SORT_KEY_RE.match(value)
    if not match:
        # Unknown shape: sort before everything, preserve relative order
        # with a lexicographic fallback.
        return (-1, 0, 0, 0, _PRERELEASE_WEIGHT, value)
    major, minor, patch, pre = match.groups()
    weight = _STABLE_WEIGHT if pre is None else _PRERELEASE_WEIGHT
    pre_key = "" if pre is None else pre
    return (int(major), int(minor), int(patch), weight, pre_key)


def is_semver_release_tag(tag: str) -> bool:
    """True when ``tag`` matches the ``X.Y.Z[-pre]`` shape used by
    ``semver_sort_key``. Non-matching tags (``latest``, ``main``, commit
    shas, etc.) sort before every release but callers may want to label
    them explicitly in UI — this helper makes that decision local."""
    return bool(_SEMVER_SORT_KEY_RE.match(tag.lstrip("v")))

DEFAULT_TIMEOUT_SECONDS = 4
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 20  # Hard cap to avoid pathological loops on misbehaving registries.

_DOCKER_HUB_HOSTS = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
# Known GHCR mirrors. They all expose the same v2 API path and use GHCR's
# token service — if a user configures a different mirror, we fall back to
# treating the host itself as the registry.
_GHCR_HOSTS = frozenset({"ghcr.io", "ghcr.nju.edu.cn"})


@dataclass
class RemoteTagResult:
    """Outcome of a best-effort remote tag fetch."""

    repo: str
    registry: str
    tags: list[str] | None = None
    error: str | None = None
    # Extra per-page diagnostic, useful for tests and telemetry.
    pages_fetched: int = 0
    extras: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.tags is not None


@dataclass
class _RegistryEndpoint:
    registry_host: str
    repo_path: str  # URL-encoded path segment after /v2/
    auth_service: str
    auth_realm: str


UrlOpener = Callable[[urllib.request.Request, float], object]


def _default_opener(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


def parse_repo(image_repo: str) -> _RegistryEndpoint | None:
    """Map a docker-style repo string to a registry v2 endpoint.

    Returns ``None`` if the input is empty or unparseable. Examples:

    >>> parse_repo("ghcr.io/openclaw/openclaw").registry_host
    'ghcr.io'
    >>> parse_repo("clawcu/hermes-agent").registry_host
    'registry-1.docker.io'
    >>> parse_repo("library/python").repo_path
    'library/python'
    """
    cleaned = (image_repo or "").strip().strip("/")
    if not cleaned:
        return None

    # If the first segment looks like a hostname (contains ``.`` or
    # ``:``) treat it as an explicit registry host. Otherwise assume
    # Docker Hub.
    first, _, rest = cleaned.partition("/")
    if ("." in first or ":" in first or first == "localhost") and rest:
        host = first
        path = rest
    else:
        host = "registry-1.docker.io"
        path = cleaned
        if "/" not in path:
            # Bare "python" -> "library/python" per Docker Hub convention.
            path = f"library/{path}"

    # Normalize known Docker Hub aliases to the canonical registry host.
    if host in _DOCKER_HUB_HOSTS:
        host = "registry-1.docker.io"
        auth_service = "registry.docker.io"
        auth_realm = "https://auth.docker.io/token"
    elif host in _GHCR_HOSTS or host.endswith(".ghcr.io"):
        auth_service = "ghcr.io"
        auth_realm = f"https://{host}/token"
    else:
        # Unknown registry — best-effort defaults. If it doesn't speak
        # v2 we'll get a 404 and report that back cleanly.
        auth_service = host
        auth_realm = f"https://{host}/token"

    return _RegistryEndpoint(
        registry_host=host,
        repo_path=urllib.parse.quote(path, safe="/"),
        auth_service=auth_service,
        auth_realm=auth_realm,
    )


def _negotiate_token(
    endpoint: _RegistryEndpoint,
    *,
    timeout: float,
    opener: UrlOpener,
) -> str | None:
    """Fetch an anonymous pull-scoped bearer token.

    Returns ``None`` if the registry does not require (or does not
    offer) token auth — we'll just try the tags endpoint without one.
    """
    params = {
        "service": endpoint.auth_service,
        "scope": f"repository:{urllib.parse.unquote(endpoint.repo_path)}:pull",
    }
    url = f"{endpoint.auth_realm}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"clawcu/{clawcu_version}",
            "Accept": "application/json",
        },
    )
    try:
        with opener(request, timeout) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _LOG.debug("token negotiation failed for %s: %s", endpoint.registry_host, exc)
        return None

    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    token = payload.get("token") or payload.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _fetch_page(
    endpoint: _RegistryEndpoint,
    *,
    token: str | None,
    path_with_query: str,
    timeout: float,
    opener: UrlOpener,
) -> tuple[list[str], str | None]:
    """Fetch a single /tags/list page.

    Returns ``(tags, next_url)``. ``next_url`` is resolved against the
    registry host when the response carries a ``Link: <...>; rel=next``
    header (standard Docker registry pagination).
    """
    url = f"https://{endpoint.registry_host}{path_with_query}"
    headers = {
        "User-Agent": f"clawcu/{clawcu_version}",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    with opener(request, timeout) as response:
        body = response.read().decode("utf-8")
        link_header = response.headers.get("Link") if hasattr(response, "headers") else None

    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"registry returned non-JSON body: {exc}") from exc
    raw_tags = payload.get("tags") if isinstance(payload, dict) else None
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [tag for tag in raw_tags if isinstance(tag, str) and tag]

    next_url = _extract_next_link(link_header)
    return tags, next_url


def _extract_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    # Format per RFC 5988: '<next-relative-url>; rel="next"'
    for part in link_header.split(","):
        segments = [segment.strip() for segment in part.split(";") if segment.strip()]
        if not segments:
            continue
        target = segments[0]
        if not (target.startswith("<") and target.endswith(">")):
            continue
        rel = None
        for attr in segments[1:]:
            if attr.lower().startswith("rel="):
                rel = attr.split("=", 1)[1].strip().strip('"')
                break
        if rel == "next":
            return target[1:-1]
    return None


def fetch_remote_tags(
    image_repo: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    page_size: int = DEFAULT_PAGE_SIZE,
    opener: UrlOpener | None = None,
) -> RemoteTagResult:
    """Enumerate tags for ``image_repo`` against its docker v2 registry.

    Always returns a ``RemoteTagResult`` — exceptions are caught and
    surfaced via ``result.error``. Callers that want strict failure
    semantics can check ``result.ok``.
    """
    endpoint = parse_repo(image_repo)
    if endpoint is None:
        return RemoteTagResult(
            repo=image_repo,
            registry="",
            error="empty or unparseable image repo",
        )

    url_opener: UrlOpener = opener or _default_opener
    token = _negotiate_token(endpoint, timeout=timeout, opener=url_opener)

    path = f"/v2/{endpoint.repo_path}/tags/list?n={page_size}"
    all_tags: list[str] = []
    pages = 0
    try:
        while path and pages < MAX_PAGES:
            tags, next_url = _fetch_page(
                endpoint,
                token=token,
                path_with_query=path,
                timeout=timeout,
                opener=url_opener,
            )
            all_tags.extend(tags)
            pages += 1
            path = next_url
    except urllib.error.HTTPError as exc:
        reason = f"registry returned HTTP {exc.code}"
        if exc.code == 401:
            reason = "unauthorized (private repo or token expired)"
        elif exc.code == 404:
            reason = "repository not found on registry"
        elif exc.code == 429:
            reason = "rate limited by registry"
        return RemoteTagResult(
            repo=image_repo,
            registry=endpoint.registry_host,
            error=reason,
            pages_fetched=pages,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return RemoteTagResult(
            repo=image_repo,
            registry=endpoint.registry_host,
            error=f"network error: {exc}",
            pages_fetched=pages,
        )
    except RuntimeError as exc:
        return RemoteTagResult(
            repo=image_repo,
            registry=endpoint.registry_host,
            error=str(exc),
            pages_fetched=pages,
        )

    # Deduplicate while preserving a stable order.
    seen: set[str] = set()
    dedup: list[str] = []
    for tag in all_tags:
        if tag in seen:
            continue
        seen.add(tag)
        dedup.append(tag)

    return RemoteTagResult(
        repo=image_repo,
        registry=endpoint.registry_host,
        tags=dedup,
        pages_fetched=pages,
    )
