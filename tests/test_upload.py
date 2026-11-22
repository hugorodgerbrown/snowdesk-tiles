"""Tests for scripts/upload.sh's terrain publish against a fake AWS CLI.

What is pinned (SNOW-1505): a sync that loses objects to R2's 503s is run
again, up to three passes, and grid.json is copied only after a pass sends
everything; three failed passes stop the run with grid.json unpublished; the
CLI is told to retry adaptively; and every line the CLI prints is stamped.
A fake `aws` and a no-op `sleep` on PATH stand in for R2 and the back-off.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

UPLOAD = Path(__file__).resolve().parents[1] / "scripts" / "upload.sh"

# Records each call; `s3 sync` fails while $FAIL_SYNCS (a counter file) is
# above zero, as a sync does when R2 refuses a few writes.
FAKE_AWS = """#!/bin/bash
printf '%s | %s %s\\n' "$*" "$AWS_RETRY_MODE" "$AWS_MAX_ATTEMPTS" >> "$AWS_LOG"
case "$1 $2" in
    "s3api head-object") echo 6993302170 ;;
    "s3 sync")
        left=$(cat "$FAIL_SYNCS")
        if [ "$left" -gt 0 ]; then
            echo $((left - 1)) > "$FAIL_SYNCS"
            echo "upload failed: $3/3523/2097.s16 (ServiceUnavailable)" >&2
            exit 1
        fi
        echo "upload: $3/3523/2097.s16 to $4/3523/2097.s16" ;;
    "s3 cp") echo "upload: $3 to $4" ;;
esac
"""


def run_upload(tmp_path: Path, failed_passes: int) -> subprocess.CompletedProcess[str]:
    """Run upload.sh over a staged terrain tree, failing the first syncs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("aws", FAKE_AWS), ("sleep", "#!/bin/bash\n")):
        tool = bin_dir / name
        tool.write_text(body)
        tool.chmod(0o755)
    terrain = tmp_path / "dist" / "terrain" / "3523"
    terrain.mkdir(parents=True)
    (terrain / "2097.s16").write_bytes(b"\0\0")
    (terrain.parent / "grid.json").write_text("{}")
    (tmp_path / "fail").write_text(str(failed_passes))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(tmp_path / "aws.log"),
        "FAIL_SYNCS": str(tmp_path / "fail"),
        "DIST_DIR": str(tmp_path / "dist"),
        "CLOUDFLARE_ACCOUNT_ID": "account",
        "AWS_ACCESS_KEY_ID": "key",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "USE_1PASSWORD": "0",
        "SNOWDESK_FAILURE_MARK": str(tmp_path / "failure-mark"),
    }
    env.pop("AWS_RETRY_MODE", None)
    env.pop("AWS_MAX_ATTEMPTS", None)
    return subprocess.run(  # noqa: S603 - the repository's own script
        ["bash", str(UPLOAD)],  # noqa: S607 - bash on PATH, as the scripts expect
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def calls(tmp_path: Path) -> list[str]:
    """Return the fake CLI's calls, in order."""
    return (tmp_path / "aws.log").read_text().splitlines()


def is_grid_copy(call: str) -> bool:
    """Return whether ``call`` publishes grid.json (a sync only excludes it)."""
    return call.startswith("s3 cp") and "/grid.json " in call


def test_a_sync_that_loses_objects_runs_again_before_grid_json(
    tmp_path: Path,
) -> None:
    result = run_upload(tmp_path, failed_passes=1)

    assert result.returncode == 0, result.stderr
    made = calls(tmp_path)
    syncs = [i for i, call in enumerate(made) if call.startswith("s3 sync")]
    grid = next(i for i, call in enumerate(made) if is_grid_copy(call))
    assert len(syncs) == 2
    assert grid > syncs[-1]
    assert "sync pass 1 of 3 missed some objects" in result.stderr
    assert all(call.endswith("| adaptive 10") for call in made)


def test_three_failed_passes_stop_with_grid_json_unpublished(tmp_path: Path) -> None:
    result = run_upload(tmp_path, failed_passes=3)

    assert result.returncode != 0
    assert not any(is_grid_copy(call) for call in calls(tmp_path))
    assert "three sync passes each missed objects" in result.stderr
    assert "STOPPED: upload.sh" in result.stderr


def test_every_line_the_cli_prints_is_stamped(tmp_path: Path) -> None:
    result = run_upload(tmp_path, failed_passes=1)

    # The CLI's errors are merged into its output, so both kinds are here.
    cli_lines = [line for line in result.stdout.splitlines() if "upload" in line]
    assert any("upload failed" in line for line in cli_lines)
    assert any("upload: " in line for line in cli_lines)
    for line in cli_lines:
        assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d upload", line), line
