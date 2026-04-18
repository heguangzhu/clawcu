from __future__ import annotations

import io
import json
import urllib.error

import pytest

from clawcu.core.registry import (
    RemoteTagResult,
    fetch_remote_tags,
    parse_repo,
)


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for urlopen()'s context-manager response.

    Carries a ``.headers`` mapping so the Link-based pagination path can
    inspect it exactly like ``http.client.HTTPResponse`` does.
    """

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:  # pragma: no cover - trivial
        self.close()


def _make_opener(url_to_response: dict[str, _FakeResponse]):
    """Build a URL-opener that returns pre-baked fake responses.

    The opener itself receives a ``urllib.request.Request`` so we can
    look up by full URL — matching how the production code calls
    ``urlopen``.
    """

    recorded: list[str] = []

    def opener(request, timeout):
        url = request.full_url
        recorded.append(url)
        if url not in url_to_response:
            raise AssertionError(f"unexpected URL in test: {url}")
        return url_to_response[url]

    opener.recorded = recorded  # type: ignore[attr-defined]
    return opener


# --- parse_repo ---------------------------------------------------------


def test_parse_repo_infers_docker_hub_for_bare_repo() -> None:
    endpoint = parse_repo("clawcu/hermes-agent")
    assert endpoint is not None
    assert endpoint.registry_host == "registry-1.docker.io"
    assert endpoint.repo_path == "clawcu/hermes-agent"
    assert endpoint.auth_service == "registry.docker.io"
    assert endpoint.auth_realm == "https://auth.docker.io/token"


def test_parse_repo_promotes_bare_name_to_library() -> None:
    endpoint = parse_repo("python")
    assert endpoint is not None
    # Docker Hub's implicit "library/" namespace for single-segment refs.
    assert endpoint.repo_path == "library/python"
    assert endpoint.registry_host == "registry-1.docker.io"


def test_parse_repo_routes_ghcr_to_its_token_service() -> None:
    endpoint = parse_repo("ghcr.io/openclaw/openclaw")
    assert endpoint is not None
    assert endpoint.registry_host == "ghcr.io"
    assert endpoint.repo_path == "openclaw/openclaw"
    assert endpoint.auth_service == "ghcr.io"
    assert endpoint.auth_realm == "https://ghcr.io/token"


def test_parse_repo_handles_ghcr_cn_mirror_with_matching_token_realm() -> None:
    endpoint = parse_repo("ghcr.nju.edu.cn/openclaw/openclaw")
    assert endpoint is not None
    # The CN mirror hostname is preserved for the /v2 path but it
    # issues its own bearer tokens — hence auth_realm points at the
    # mirror, not ghcr.io proper.
    assert endpoint.registry_host == "ghcr.nju.edu.cn"
    assert endpoint.auth_realm == "https://ghcr.nju.edu.cn/token"


def test_parse_repo_returns_none_for_empty_input() -> None:
    assert parse_repo("") is None
    assert parse_repo("   ") is None


# --- fetch_remote_tags --------------------------------------------------


def test_fetch_remote_tags_single_page_success() -> None:
    token_body = json.dumps({"token": "fake-token"}).encode("utf-8")
    tags_body = json.dumps({"tags": ["v2026.4.1", "v2026.4.2", "latest"]}).encode("utf-8")
    opener = _make_opener(
        {
            (
                "https://ghcr.io/token?"
                "service=ghcr.io&scope=repository%3Aopenclaw%2Fopenclaw%3Apull"
            ): _FakeResponse(token_body),
            "https://ghcr.io/v2/openclaw/openclaw/tags/list?n=100": _FakeResponse(
                tags_body
            ),
        }
    )

    result = fetch_remote_tags("ghcr.io/openclaw/openclaw", opener=opener)

    assert result.ok
    assert result.tags == ["v2026.4.1", "v2026.4.2", "latest"]
    assert result.registry == "ghcr.io"
    assert result.error is None
    assert result.pages_fetched == 1


def test_fetch_remote_tags_follows_link_header_pagination() -> None:
    token_body = json.dumps({"token": "fake"}).encode("utf-8")
    first_page = _FakeResponse(
        json.dumps({"tags": ["1.0.0", "1.1.0"]}).encode("utf-8"),
        headers={
            "Link": (
                '</v2/clawcu/hermes-agent/tags/list?n=100&last=1.1.0>; rel="next"'
            )
        },
    )
    second_page = _FakeResponse(
        json.dumps({"tags": ["1.2.0"]}).encode("utf-8"),
    )
    opener = _make_opener(
        {
            (
                "https://auth.docker.io/token?"
                "service=registry.docker.io&scope=repository%3Aclawcu%2Fhermes-agent%3Apull"
            ): _FakeResponse(token_body),
            "https://registry-1.docker.io/v2/clawcu/hermes-agent/tags/list?n=100": first_page,
            (
                "https://registry-1.docker.io/v2/clawcu/hermes-agent/tags/list"
                "?n=100&last=1.1.0"
            ): second_page,
        }
    )

    result = fetch_remote_tags("clawcu/hermes-agent", opener=opener)

    assert result.ok
    assert result.tags == ["1.0.0", "1.1.0", "1.2.0"]
    assert result.pages_fetched == 2


def test_fetch_remote_tags_deduplicates_across_pages() -> None:
    token_body = json.dumps({"token": "fake"}).encode("utf-8")
    first_page = _FakeResponse(
        json.dumps({"tags": ["1.0.0", "1.1.0"]}).encode("utf-8"),
        headers={"Link": '</v2/foo/bar/tags/list?n=100&last=1.1.0>; rel="next"'},
    )
    second_page = _FakeResponse(
        # "1.1.0" is a duplicate — must be collapsed.
        json.dumps({"tags": ["1.1.0", "1.2.0"]}).encode("utf-8"),
    )
    opener = _make_opener(
        {
            (
                "https://auth.docker.io/token?"
                "service=registry.docker.io&scope=repository%3Afoo%2Fbar%3Apull"
            ): _FakeResponse(token_body),
            "https://registry-1.docker.io/v2/foo/bar/tags/list?n=100": first_page,
            "https://registry-1.docker.io/v2/foo/bar/tags/list?n=100&last=1.1.0": second_page,
        }
    )

    result = fetch_remote_tags("foo/bar", opener=opener)
    assert result.ok
    assert result.tags == ["1.0.0", "1.1.0", "1.2.0"]


def test_fetch_remote_tags_reports_404_as_repo_not_found() -> None:
    token_body = json.dumps({"token": "fake"}).encode("utf-8")

    def opener(request, timeout):
        url = request.full_url
        if "/token" in url:
            return _FakeResponse(token_body)
        raise urllib.error.HTTPError(
            url=url,
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    result = fetch_remote_tags("ghcr.io/does/not-exist", opener=opener)
    assert not result.ok
    assert result.tags is None
    assert result.error is not None
    assert "not found" in result.error.lower()


def test_fetch_remote_tags_reports_429_as_rate_limit() -> None:
    token_body = json.dumps({"token": "fake"}).encode("utf-8")

    def opener(request, timeout):
        url = request.full_url
        if "/token" in url:
            return _FakeResponse(token_body)
        raise urllib.error.HTTPError(
            url=url,
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    result = fetch_remote_tags("ghcr.io/openclaw/openclaw", opener=opener)
    assert not result.ok
    assert "rate limit" in (result.error or "").lower()


def test_fetch_remote_tags_swallows_network_errors() -> None:
    def opener(request, timeout):
        raise urllib.error.URLError("no route to host")

    result = fetch_remote_tags("ghcr.io/openclaw/openclaw", opener=opener)
    assert not result.ok
    assert result.error is not None
    assert "no route to host" in result.error


def test_fetch_remote_tags_returns_error_for_empty_repo() -> None:
    result = fetch_remote_tags("")
    assert not result.ok
    assert "unparseable" in (result.error or "").lower() or "empty" in (
        result.error or ""
    ).lower()


def test_fetch_remote_tags_handles_non_json_body() -> None:
    token_body = json.dumps({"token": "fake"}).encode("utf-8")
    html_body = _FakeResponse(b"<html>oops</html>")
    opener = _make_opener(
        {
            (
                "https://ghcr.io/token?"
                "service=ghcr.io&scope=repository%3Aopenclaw%2Fopenclaw%3Apull"
            ): _FakeResponse(token_body),
            "https://ghcr.io/v2/openclaw/openclaw/tags/list?n=100": html_body,
        }
    )

    result = fetch_remote_tags("ghcr.io/openclaw/openclaw", opener=opener)
    assert not result.ok
    assert "non-JSON" in (result.error or "") or "json" in (result.error or "").lower()
