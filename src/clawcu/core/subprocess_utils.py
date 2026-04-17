from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, command: list[str], returncode: int, stdout: str | None, stderr: str | None):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        rendered = render_command(command)
        details = self.stderr.strip() or self.stdout.strip() or "unknown subprocess error"
        super().__init__(f"Command failed ({returncode}): {rendered}\n{details}")


def render_command(command: list[str]) -> str:
    return shlex.join(command)


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = True,
    check: bool = True,
    stream_output: bool = False,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if stream_output:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_chunks: list[str] = []
        assert process.stdout is not None
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                output_chunks.append(line)
            process.stdout.close()
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout = "".join(output_chunks) + (exc.stdout or "")
            stderr = (exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds"
            raise CommandError(command, 124, stdout, stderr) from exc
        result = subprocess.CompletedProcess(
            command,
            returncode,
            "".join(output_chunks),
            "",
        )
    else:
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=capture_output,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds"
            raise CommandError(command, 124, stdout, stderr) from exc
    if check and result.returncode != 0:
        raise CommandError(command, result.returncode, result.stdout, result.stderr)
    return result
