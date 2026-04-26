"""Build a2a-flavored variants of the stock OpenClaw / Hermes images.

Keeps the Docker-facing plumbing out of the service layer: given a base
image tag plus the current clawcu version, produce a new tag
`clawcu/<service>-a2a:<base-ver>-plugin<clawcu-ver>` that bakes the
sidecar and supervisor entrypoint from `clawcu.a2a.sidecar_plugin.<service>/`.

The resulting image exposes the stock service on its original port AND the
A2A protocol on a neighbor port (18790 for openclaw, 9119 for hermes), so
federation via `clawcu a2a up` finds the AgentCard with no host-side
sidecar process.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from clawcu.a2a.sidecar_plugin import plugin_fingerprint, plugin_source_dir
from clawcu.core.validation import normalize_service_version

if TYPE_CHECKING:
    from clawcu.core.docker import DockerManager


_A2A_REPO: dict[str, str] = {
    "openclaw": "clawcu/openclaw-a2a",
    "hermes": "clawcu/hermes-agent-a2a",
}

# Per-service Dockerfile build-arg names for the base-image coordinates.
# The package-shipped Dockerfiles declare these, so we need to pass both
# the version and the repo (in case the user configured a mirror repo).
_A2A_BUILD_ARGS: dict[str, tuple[str, str]] = {
    "openclaw": ("OPENCLAW_VERSION", "OPENCLAW_IMAGE_REPO"),
    "hermes": ("HERMES_VERSION", "HERMES_IMAGE_REPO"),
}

_TAG_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

# Docker official tag spec: first char is [A-Za-z0-9_], rest may include
# dots and hyphens, up to 128 chars total. Any resulting tag we emit must
# satisfy this allow-list — see Review-1 §14.
# https://docs.docker.com/engine/reference/commandline/tag/#description
_DOCKER_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _tag_component(value: str) -> str:
    cleaned = _TAG_SAFE.sub("-", value.strip()).strip("-.")
    return cleaned or "unknown"


def a2a_image_tag(service: str, base_version: str, clawcu_version: str) -> str:
    """Return the canonical a2a image tag for this (service, base, clawcu) triple.

    The plugin component is a ``<clawcu_version>.<source-sha>`` fingerprint
    (see ``clawcu.a2a.sidecar_plugin.plugin_fingerprint``); the short-sha defeats
    editable-install staleness — bumping only clawcu's ``__version__`` without
    re-installing still changes the tag whenever the on-disk sidecar changes.
    """
    if service not in _A2A_REPO:
        raise ValueError(f"A2A baking is not supported for service '{service}'.")
    normalized_base = normalize_service_version(service, base_version)
    fingerprint = plugin_fingerprint(service, clawcu_version)
    tag = f"{_tag_component(normalized_base)}-plugin{_tag_component(fingerprint)}"
    # Review-1 §14: belt-and-braces — after sanitising each component we
    # still validate the composed tag against docker's official allow-list
    # before handing it to `docker build -t`. Our sanitiser has historically
    # been blacklist-style; if a future change to either component
    # (plugin_fingerprint, normalize_service_version) ever produces a string
    # that sanitises to an invalid tag (leading '.', too long, etc.) we
    # prefer to fail loudly here than to have the docker CLI reject it
    # with an opaque error.
    if not _DOCKER_TAG_RE.match(tag):
        raise ValueError(
            f"Refusing to emit invalid docker tag {tag!r} for service={service}, "
            f"base_version={base_version!r}, clawcu_version={clawcu_version!r}. "
            f"Tag must match {_DOCKER_TAG_RE.pattern}."
        )
    return f"{_A2A_REPO[service]}:{tag}"


@dataclass
class A2AImageBuilder:
    docker: "DockerManager"
    clawcu_version: str
    reporter: object = None  # clawcu.core.service.Reporter

    def _report(self, message: str) -> None:
        if callable(self.reporter):
            self.reporter(message)

    def ensure_image(
        self,
        service: str,
        base_version: str,
        base_image: str,
    ) -> str:
        """Build the a2a variant of ``base_image`` if missing; return the tag.

        The base version and the clawcu version together determine the
        output tag. Base image repo is forwarded to the Dockerfile as a
        build arg so mirrored registries (e.g. ``ghcr.nju.edu.cn``) work.
        """
        if service not in _A2A_BUILD_ARGS:
            raise ValueError(f"A2A baking is not supported for service '{service}'.")
        target_tag = a2a_image_tag(service, base_version, self.clawcu_version)
        if self.docker.image_exists(target_tag):
            self._report(
                f"A2A image {target_tag} already exists locally. Skipping rebuild."
            )
            return target_tag
        if not self.docker.image_exists(base_image):
            # Pull the base so docker build doesn't race with a missing layer.
            self._report(
                f"Base image {base_image} is not present locally; pulling before baking."
            )
            self.docker.pull_image(base_image)
        # Split base_image into repo:tag for the Dockerfile build-args.
        repo, _, tag = base_image.rpartition(":")
        if not repo or not tag:
            raise RuntimeError(
                f"Could not split base image '{base_image}' into repo:tag."
            )
        version_arg, repo_arg = _A2A_BUILD_ARGS[service]
        service_dir = plugin_source_dir(service)
        # Build context is sidecar_plugin/ (parent of the service dir) so the
        # Dockerfile can COPY both the service-specific assets and the shared
        # _common/ package baked alongside them.
        context_dir = service_dir.parent
        dockerfile_path = service_dir / "Dockerfile"
        fingerprint = plugin_fingerprint(service, self.clawcu_version)
        self._report(
            f"Baking A2A-enabled image {target_tag} from {base_image} "
            f"(clawcu plugin fingerprint {fingerprint}). This can take ~30-60s "
            "on the first build."
        )
        self.docker.build_image(
            context_dir,
            target_tag,
            dockerfile=dockerfile_path,
            build_args={
                version_arg: tag,
                repo_arg: repo,
                "CLAWCU_PLUGIN_VERSION": fingerprint,
            },
        )
        return target_tag
