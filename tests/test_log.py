"""Tests for the timestamped log lines: scripts/log.sh and scripts/stamp.py.

Hugo, 20 Nov 2026: every logging statement carries a timestamp. What is
pinned: each helper puts the local date and time, to the second, in front of
the message, in the same shape from shell and Python; logf keeps printf's
formatting; stamp_lines stamps every line, including a last one with no
newline; and Python's log writes to stderr, so stdout stays for data.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from stamp import log, stamp  # noqa: E402

STAMPED = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (.*)$")


def bash(script: str, stdin: str = "") -> list[str]:
    """Run ``script`` after sourcing log.sh; return its stdout lines."""
    result = subprocess.run(  # noqa: S603 - a script this test wrote
        ["bash", "-c", f'source "{ROOT}/scripts/log.sh"\n{script}'],  # noqa: S607 - bash on PATH
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def messages(lines: list[str]) -> list[str]:
    """Return each line's message, failing on a line with no stamp."""
    found = []
    for line in lines:
        match = STAMPED.match(line)
        assert match, f"no timestamp on {line!r}"
        found.append(match.group(1))
    return found


def test_log_stamps_the_message() -> None:
    assert messages(bash('log "==> uploading to R2"')) == ["==> uploading to R2"]


def test_logf_formats_like_printf() -> None:
    lines = bash("logf '  ok    %s (%s m)' Zermatt 1608.50")

    assert messages(lines) == ["  ok    Zermatt (1608.50 m)"]


def test_stamp_lines_stamps_every_line_even_without_a_final_newline() -> None:
    lines = bash("stamp_lines", stdin="upload: a\nupload: b")

    assert messages(lines) == ["upload: a", "upload: b"]


def test_python_log_goes_to_stderr_with_the_same_stamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log("==> classifying", 42, "cells")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert messages(captured.err.splitlines()) == ["==> classifying 42 cells"]
    assert STAMPED.match(f"{stamp()} x")
