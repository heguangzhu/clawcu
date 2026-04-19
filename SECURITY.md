# Security Policy

## Scope

ClawCU is a local-first CLI that manages Docker containers and reads/writes under
`~/.clawcu/` on the user's machine. It does not operate network services, does not
expose remote endpoints, and does not collect telemetry. The realistic security
surface is:

- Local Docker socket access (ClawCU shells out to `docker`).
- Filesystem operations under `~/.clawcu/` (instance data, env files, snapshots).
- Reading user-provided provider bundles (possibly containing API keys).
- Generated container artifacts (env files, dashboard tokens).

## Reporting a Vulnerability

If you believe you have found a security issue, please report it privately instead
of opening a public issue. Two channels:

1. **GitHub Security Advisories** — preferred. Go to the repository's Security tab
   and click "Report a vulnerability".
2. **Email** — open a GitHub issue asking for a private contact; we will coordinate
   a non-public channel.

Please include:

- The ClawCU version (`clawcu --version`).
- The OS and Docker version.
- A minimal reproduction if you have one.
- Impact you observed (data exposure, unintended file writes, privilege escalation,
  etc.).

We aim to acknowledge reports within 7 days and to ship a fix or mitigation in the
next patch release when the issue is confirmed.

## Supported Versions

Only the latest minor release receives security fixes.

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅        |
| < 0.2   | ❌        |

## Out of Scope

- Vulnerabilities in OpenClaw, Hermes, or other runtimes ClawCU manages — report
  those upstream.
- Issues that require an attacker to already have local shell access as the user
  running ClawCU.
- Denial-of-service via local resource exhaustion (e.g., creating thousands of
  instances).
