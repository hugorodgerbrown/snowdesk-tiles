"""Tests for scripts/failure.sh: the box a stopped operator script prints."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURE = ROOT / "scripts" / "failure.sh"


def write_script(path: Path, body: str) -> Path:
    path.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\nsource {FAILURE}\n{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def run(script: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "SNOWDESK_FAILURE_MARK"}
    return subprocess.run(  # noqa: S603 - a script this test wrote
        [str(script)], capture_output=True, text=True, env=env, check=False
    )


def test_a_failed_command_names_the_stage_the_command_and_what_to_do(
    tmp_path: Path,
) -> None:
    script = write_script(
        tmp_path / "build.sh",
        'stage "downloading things" "A blip." "Run it again."\nfalse\necho never',
    )

    result = run(script)

    assert result.returncode == 1
    assert "never" not in result.stdout
    assert "STOPPED: build.sh, while downloading things" in result.stderr
    assert "Failed: false" in result.stderr
    assert "What it means: A blip." in result.stderr
    assert "What to do:    Run it again." in result.stderr


def test_an_explicit_exit_points_at_the_message_above(tmp_path: Path) -> None:
    script = write_script(
        tmp_path / "build.sh",
        'stage "checking" "Bad." "Fix it."\necho "error: disk full" >&2\nexit 1',
    )

    result = run(script)

    assert result.returncode == 1
    assert "see the error message just above" in result.stderr
    assert "while checking" in result.stderr


def test_a_nested_script_reports_once(tmp_path: Path) -> None:
    inner = write_script(
        tmp_path / "inner.sh", 'stage "inner work" "Inner." "Inner fix."\nfalse'
    )
    outer = write_script(
        tmp_path / "outer.sh", f'stage "outer work" "Outer." "Outer fix."\n{inner}'
    )

    result = run(outer)

    assert result.returncode == 1
    assert result.stderr.count("STOPPED:") == 1
    assert "STOPPED: inner.sh, while inner work" in result.stderr
    assert "outer.sh stopped too, while outer work" in result.stderr


def test_success_prints_nothing_and_leaves_no_marker(tmp_path: Path) -> None:
    script = write_script(tmp_path / "build.sh", 'stage "fine" "-" "-"\necho done')
    env = {k: v for k, v in os.environ.items() if k != "SNOWDESK_FAILURE_MARK"}
    env["TMPDIR"] = str(tmp_path)

    result = subprocess.run(  # noqa: S603 - a script this test wrote
        [str(script)], capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode == 0
    assert result.stdout == "done\n"
    assert result.stderr == ""
    assert not list(tmp_path.glob("snowdesk-failure.*"))
