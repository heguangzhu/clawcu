"""pytest port of tests/sidecar_server_module_load.test.js.

The Node test guards that `require("server.js")` does NOT start a real
setInterval at module load. The Python equivalent is that importing
`server` does NOT start the outbound sweep thread — the sweep timer is
wired inside `server.main()`.
"""
from __future__ import annotations

import importlib.util
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
# Expose openclaw's sidecar/ so ``from logsink import ...`` and siblings
# resolve against the right copy (hermes's sidecar/ also has a server.py —
# name-based ``import server`` would race). The actual server.py is loaded
# by path below, so a later sys.path mutation from some hermes-side test
# cannot steal the import.
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)


def _load_openclaw_server_module():
    path = os.path.join(_SIDECAR, "server.py")
    spec = importlib.util.spec_from_file_location("_openclaw_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_server_does_not_start_sweep_thread():
    before = {t.name for t in threading.enumerate()}
    server = _load_openclaw_server_module()
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
