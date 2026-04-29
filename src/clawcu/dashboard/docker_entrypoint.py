"""Docker entrypoint for the ClawCU dashboard container.

Reads CLAWCU_HOME from the environment (falls back to /root/.clawcu),
then starts the dashboard server on 0.0.0.0:8765.
"""
from __future__ import annotations

import os

from clawcu.dashboard.server import serve_dashboard


def main() -> None:
    # When the host mounts ~/.clawcu into the container, it lands at
    # /root/.clawcu by default.  Honour an explicit override just in case.
    os.environ.setdefault("CLAWCU_HOME", "/root/.clawcu")
    serve_dashboard(host="0.0.0.0", port=8765, open_browser=False)


if __name__ == "__main__":
    main()
