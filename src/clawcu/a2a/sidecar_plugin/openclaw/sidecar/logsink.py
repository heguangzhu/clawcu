"""File-tee logger for the openclaw A2A sidecar (Python port of logsink.js).

Mirrors the Node module: opt-in via A2A_SIDECAR_LOG_DIR. Tees INFO/ERROR
messages to <logDir>/a2a-sidecar.log while always writing to stderr so
`docker logs` and test harnesses keep working. Append-only, best-effort.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone


def _format_arg(arg) -> str:
    if isinstance(arg, str):
        return arg
    if isinstance(arg, BaseException):
        return "".join(traceback.format_exception(type(arg), arg, arg.__traceback__)).rstrip() or str(arg)
    try:
        return json.dumps(arg, default=str)
    except Exception:
        return str(arg)


class Logger:
    """Minimal console.log / console.error replacement.

    `info(...)` writes to stdout; `error(...)` and `warn(...)` write to stderr.
    When a log file is installed via `install_file_sink`, every record is also
    appended to the file with an ISO timestamp and level tag.
    """

    def __init__(self) -> None:
        self._file = None
        self._file_path: str | None = None
        self._lock = threading.Lock()

    @property
    def installed(self) -> bool:
        return self._file is not None

    @property
    def log_path(self) -> str | None:
        return self._file_path

    def install_file_sink(self, log_dir: str | None) -> dict:
        if not log_dir:
            return {"installed": False, "reason": "no log dir"}
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "a2a-sidecar.log")
            # Append-only, line-buffered (buffering=1) so tests see output quickly.
            self._file = open(log_path, "a", encoding="utf-8", buffering=1)
            self._file_path = log_path
            return {"installed": True, "logPath": log_path}
        except Exception as exc:
            sys.stderr.write(f"[sidecar] log-file setup failed, stderr-only: {exc}\n")
            self._file = None
            self._file_path = None
            return {"installed": False, "reason": str(exc)}

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                finally:
                    self._file = None
                    self._file_path = None

    def _tee(self, level: str, args) -> None:
        if self._file is None:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        msg = " ".join(_format_arg(a) for a in args)
        try:
            with self._lock:
                if self._file is not None:
                    self._file.write(f"{ts} {level} {msg}\n")
        except Exception:
            pass

    def info(self, *args) -> None:
        self._tee("INFO", args)
        line = " ".join(_format_arg(a) for a in args)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    # Alias to keep parity with console.log usage patterns.
    log = info

    def warn(self, *args) -> None:
        self._tee("WARN", args)
        line = " ".join(_format_arg(a) for a in args)
        sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def error(self, *args) -> None:
        self._tee("ERROR", args)
        line = " ".join(_format_arg(a) for a in args)
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def setup_file_log(log_dir: str | None, logger: Logger | None = None) -> dict:
    """Mirror of the Node `setupFileLog`. Returns {installed, logPath|reason}."""
    target = logger if logger is not None else default_logger
    return target.install_file_sink(log_dir)


default_logger = Logger()
