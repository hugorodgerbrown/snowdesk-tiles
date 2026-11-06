# Resolve a script's credentials through the 1Password CLI. Source this file;
# do not run it.
#
# A script that needs secrets calls, once it has cd'd to the repo root:
#
#     source scripts/op-env.sh
#     op_reexec scripts/upload.sh CLOUDFLARE_ACCOUNT_ID AWS_ACCESS_KEY_ID -- "$@"
#
# If every name is already in the environment, nothing happens: CI, a VM where
# the values were typed in, or a run you wrapped in `op run` yourself all work
# as before. If any is missing and the `op` CLI is installed, the script
# re-executes itself under
#
#     op run --env-file=.env.1password -- <script> <args>
#
# so `./scripts/upload.sh` is the whole command. Only the missing names are
# resolved; a value already set is never replaced by the stored one. op
# resolves the op:// references at launch and hands the values to that one
# process: nothing is written to disk and nothing reaches shell history. On a
# laptop the desktop app unlocks it (Touch ID); on a VM, OP_SERVICE_ACCOUNT_TOKEN
# does (see vm-build.sh).
#
# Settings:
#     OP_ENV_FILE=path    the references to resolve (default .env.1password)
#     USE_1PASSWORD=0     never call op; the script's own checks then name
#                         whatever is missing
#
# Without op, or with USE_1PASSWORD=0, this returns quietly and the calling
# script reports the missing variable itself, as it always has.

: "${OP_ENV_FILE:=.env.1password}"

# op_reexec SCRIPT NAME... -- ARG...
op_reexec() {
    local script=$1 name missing=()
    shift
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        name=$1
        [ -n "${!name:-}" ] || missing+=("$name")
        shift
    done
    [ "${1:-}" = "--" ] && shift

    [ "${#missing[@]}" -gt 0 ] || return 0
    # Already under op run: whatever is still missing is not in the env file,
    # and a second op run would loop. Let the script say what is missing.
    [ -z "${SNOWDESK_OP_RUN:-}" ] || return 0
    [ "${USE_1PASSWORD:-1}" != "0" ] || return 0
    if ! command -v op >/dev/null 2>&1; then
        echo "note: ${missing[*]} not set, and the 1Password CLI (op) is not installed" >&2
        return 0
    fi
    if [ ! -f "$OP_ENV_FILE" ]; then
        echo "note: ${missing[*]} not set, and there is no ${OP_ENV_FILE} to resolve them from" >&2
        return 0
    fi

    # Only the missing names go to op. An env file outranks the shell in op
    # run's precedence, so handing it all of OP_ENV_FILE would replace a value
    # the caller set on purpose (another account, say) with the stored one.
    # The temporary file holds op:// references, not secrets, and the child
    # removes it before it runs the script.
    local refs pattern
    refs=$(mktemp "${TMPDIR:-/tmp}/snowdesk-op.XXXXXX")
    pattern="^($(IFS='|'; echo "${missing[*]}"))="
    grep -E "$pattern" "$OP_ENV_FILE" >"$refs" || true

    echo "==> resolving ${missing[*]} from 1Password (${OP_ENV_FILE})" >&2
    SNOWDESK_OP_RUN=1 exec op run --env-file="$refs" -- \
        bash -c 'rm -f "$0"; exec "$@"' "$refs" "$script" "$@"
}
