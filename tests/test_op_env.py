"""Tests for scripts/op-env.sh, the 1Password CLI wrapper.

What is pinned: a script missing a credential re-runs itself under
`op run --env-file=.env.1password`, with its own arguments; one that already
has every credential, or is already under op run, or was told USE_1PASSWORD=0,
does not call op; and with no op installed it carries on, so the script's own
check names the missing variable. A fake `op` on PATH stands in for 1Password.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "op-env.sh"

# A fake op: records how it was called, then runs the command after `--` with
# the credential set, as op run would once it had resolved the references.
FAKE_OP = """#!/bin/bash
printf '%s\\n' "$*" >> "$OP_LOG"
while [ "$1" != "--" ]; do shift; done
shift
SECRET=from-1password exec "$@"
"""

# The calling script: needs SECRET, and prints what it ended up with.
CALLER = """#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source "{helper}"
op_reexec "$PWD/caller.sh" SECRET -- "$@"
echo "SECRET=${{SECRET:-<unset>}} args=$*"
"""


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Return a directory holding the caller, an env file and a fake op."""
    (tmp_path / "bin").mkdir()
    op = tmp_path / "bin" / "op"
    op.write_text(FAKE_OP)
    op.chmod(0o755)
    caller = tmp_path / "caller.sh"
    caller.write_text(CALLER.format(helper=HELPER))
    caller.chmod(0o755)
    (tmp_path / ".env.1password").write_text('SECRET="op://Vault/Item/field"\n')
    return tmp_path


def run(
    workdir: Path, env: dict[str, str] | None = None, with_op: bool = True
) -> tuple[str, list[str]]:
    """Run the caller with two arguments; return its stdout and op's calls."""
    log = workdir / "op.log"
    path = f"{workdir / 'bin'}:/usr/bin:/bin" if with_op else "/usr/bin:/bin"
    result = subprocess.run(  # noqa: S603
        [str(workdir / "caller.sh"), "one", "two words"],
        capture_output=True,
        text=True,
        env={
            "PATH": path,
            "OP_LOG": str(log),
            "HOME": os.environ.get("HOME", "/"),
            **(env or {}),
        },
        check=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result.stdout.strip(), calls


def test_a_missing_credential_reruns_the_script_under_op(workdir: Path) -> None:
    out, calls = run(workdir)

    assert out == "SECRET=from-1password args=one two words"
    assert len(calls) == 1
    assert calls[0].startswith("run --env-file=.env.1password -- ")
    assert calls[0].endswith("caller.sh one two words")


def test_a_credential_already_set_is_used_as_it_is(workdir: Path) -> None:
    out, calls = run(workdir, {"SECRET": "typed-in"})

    assert out == "SECRET=typed-in args=one two words"
    assert calls == []


def test_under_op_run_already_it_does_not_loop(workdir: Path) -> None:
    out, calls = run(workdir, {"SNOWDESK_OP_RUN": "1"})

    assert out == "SECRET=<unset> args=one two words"
    assert calls == []


def test_use_1password_0_leaves_op_alone(workdir: Path) -> None:
    out, calls = run(workdir, {"USE_1PASSWORD": "0"})

    assert out == "SECRET=<unset> args=one two words"
    assert calls == []


def test_without_op_the_script_carries_on(workdir: Path) -> None:
    out, calls = run(workdir, with_op=False)

    assert out == "SECRET=<unset> args=one two words"
    assert calls == []


def test_another_references_file_can_be_named(workdir: Path) -> None:
    (workdir / "vm.env").write_text('SECRET="op://Shared/Item/field"\n')

    _, calls = run(workdir, {"OP_ENV_FILE": "vm.env"})

    assert calls[0].startswith("run --env-file=vm.env -- ")
