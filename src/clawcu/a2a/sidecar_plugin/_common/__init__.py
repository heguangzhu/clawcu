"""Shared sidecar primitives for OpenClaw and Hermes A2A runtimes.

Both runtimes bake these modules into their containers alongside the
service-specific sidecar script (see each service's Dockerfile). The
authoritative source lives here so bug fixes land in one place and the
two runtimes cannot drift.
"""
