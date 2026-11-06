"""Tests for scripts/op-env.sh, the 1Password CLI wrapper.

What is pinned: a script missing a credential re-runs itself under
`op run --env-file=.env.1password`, with its own arguments; one that already
has every credential, or is already under op run, or was told USE_1PASSWORD=0,
does not call op; only the missing names are handed to op, so a value the
caller set is never replaced; and with no op installed it carries on, so the
script's own check names the missing variable. A fake `op` on PATH stands in
for 1Password.
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
file=${2#--env-file=}
cp "$file" "$OP_LOG.refs"
while [ "$1" != "--" ]; do shift; done
shift
# As op run: every name in the file is set, over anything already there.
while IFS='=' read -r name _; do
    [ -n "$name" ] && export "$name=from-1password"
done < "$file"
exec "$@"
"""

# The calling script: needs SECRET, and prints what it ended up with.
CALLER = """#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source "{helper}"
op_reexec "$PWD/caller.sh" SECRET OTHER -- "$@"
echo "SECRET=${{SECRET:-<unset>}} OTHER=${{OTHER:-<unset>}} args=$*"
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
    (tmp_path / ".env.1password").write_text(
        'SECRET="op://Vault/Item/field"\nOTHER="op://Vault/Item/other"\n'
    )
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


def refs_passed(workdir: Path) -> str:
    """Return the references file op was handed."""
    return (workdir / "op.log.refs").read_text()


def test_a_missing_credential_reruns_the_script_under_op(workdir: Path) -> None:
    out, calls = run(workdir)

    assert out == "SECRET=from-1password OTHER=from-1password args=one two words"
    assert len(calls) == 1
    assert calls[0].startswith("run --env-file=")
    assert calls[0].endswith("caller.sh one two words")
    assert refs_passed(workdir) == (workdir / ".env.1password").read_text()


def test_only_the_missing_names_are_resolved(workdir: Path) -> None:
    # An env file outranks the shell in op run, so passing every reference
    # would replace the value set here with the stored one.
    out, _ = run(workdir, {"SECRET": "another-account"})

    assert out == "SECRET=another-account OTHER=from-1password args=one two words"
    assert refs_passed(workdir) == 'OTHER="op://Vault/Item/other"\n'


def test_the_temporary_references_file_is_removed(workdir: Path) -> None:
    tmp = workdir / "tmp"
    tmp.mkdir()

    run(workdir, {"TMPDIR": str(tmp)})

    assert list(tmp.iterdir()) == []


def test_credentials_already_set_are_used_as_they_are(workdir: Path) -> None:
    out, calls = run(workdir, {"SECRET": "typed-in", "OTHER": "too"})

    assert out == "SECRET=typed-in OTHER=too args=one two words"
    assert calls == []


@pytest.mark.parametrize(
    ("env", "with_op"),
    [({"SNOWDESK_OP_RUN": "1"}, True), ({"USE_1PASSWORD": "0"}, True), ({}, False)],
    ids=["already-under-op-run", "use-1password-0", "no-op-installed"],
)
def test_otherwise_the_script_carries_on_without_op(
    workdir: Path, env: dict[str, str], with_op: bool
) -> None:
    out, calls = run(workdir, env, with_op=with_op)

    assert out == "SECRET=<unset> OTHER=<unset> args=one two words"
    assert calls == []


def test_another_references_file_can_be_named(workdir: Path) -> None:
    (workdir / "vm.env").write_text('SECRET="op://Shared/Item/field"\n')

    out, _ = run(workdir, {"OP_ENV_FILE": "vm.env"})

    assert refs_passed(workdir) == 'SECRET="op://Shared/Item/field"\n'
    assert out.startswith("SECRET=from-1password OTHER=<unset>")
