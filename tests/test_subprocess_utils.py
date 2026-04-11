from __future__ import annotations

from clawcu.subprocess_utils import CommandError


def test_command_error_handles_missing_stdout_and_stderr() -> None:
    error = CommandError(["docker", "pull", "missing"], 1, None, None)

    assert error.stdout == ""
    assert error.stderr == ""
    assert "unknown subprocess error" in str(error)
