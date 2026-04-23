"""pytest port of tests/sidecar_server_module_load.test.js.

The Node test guards that `require("server.js")` does NOT start a real
setInterval at module load. The Python equivalent is that importing
`server` does NOT start the outbound sweep thread — the sweep timer is
wired inside `server.main()`.
"""
from __future__ import annotations

import importlib
import os
import sys
import threading

_SIDECAR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
        "openclaw",
        "sidecar",
    )
)
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)


def test_import_server_does_not_start_sweep_thread():
    # Drop any cached import so the module body runs under our observation.
    for name in ("server",):
        sys.modules.pop(name, None)

    before = {t.name for t in threading.enumerate()}
    server = importlib.import_module("server")
    after = {t.name for t in threading.enumerate()}

    # Importing server must not spawn a sweep thread. The class still
    # needs to exist (main() constructs one), but nothing should run yet.
    new_threads = after - before
    assert not any("sweep" in n.lower() for n in new_threads), (
        f"import must not start a sweep thread, saw: {new_threads}"
    )
    # Sanity: the module did load the handler-building symbols.
    assert hasattr(server, "OUTBOUND_LIMITER")
    assert hasattr(server, "A2A_HOP_BUDGET")
    assert hasattr(server, "lookup_peer")
    assert hasattr(server, "forward_to_peer")
