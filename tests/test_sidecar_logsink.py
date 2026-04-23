"""pytest port of tests/sidecar_logsink.test.js.

Exercises the file-tee installed by logsink.Logger. Each test uses a fresh
Logger instance (not default_logger) so they don't leak state between cases.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

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

from logsink import Logger, setup_file_log  # noqa: E402


def test_setup_file_log_no_dir_is_noop():
    logger = Logger()
    result = setup_file_log("", logger=logger)
    assert result["installed"] is False
    assert logger.installed is False


def test_setup_file_log_writes_info_and_error_lines(tmp_path):
    logger = Logger()
    try:
        result = setup_file_log(str(tmp_path), logger=logger)
        assert result["installed"] is True

        logger.info("hello", {"peer": "a"})
        logger.error(RuntimeError("boom"))
    finally:
        logger.close()

    log_path = tmp_path / "a2a-sidecar.log"
    body = log_path.read_text(encoding="utf-8")
    assert re.search(r'INFO hello \{"peer": "a"\}', body), "INFO line must be teed"
    assert re.search(r"ERROR .*boom", body), "ERROR line must include the exception message"
    first_line = body.splitlines()[0]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", first_line), "ISO timestamp prefix"


def test_setup_file_log_creates_missing_parents(tmp_path):
    logger = Logger()
    nested = tmp_path / "a" / "b" / "c"
    try:
        result = setup_file_log(str(nested), logger=logger)
        assert result["installed"] is True
        logger.info("nested")
    finally:
        logger.close()
    assert (nested / "a2a-sidecar.log").exists()


def test_setup_file_log_still_writes_to_stdout(tmp_path, capsys):
    logger = Logger()
    try:
        result = setup_file_log(str(tmp_path), logger=logger)
        assert result["installed"] is True
        logger.info("tee-me")
        logger.warn("warn-me")
    finally:
        logger.close()
    captured = capsys.readouterr()
    assert "tee-me" in captured.out, "stdout path must still fire"
    assert "warn-me" in captured.err, "stderr path must still fire"
