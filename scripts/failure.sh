# Shared failure reporting for the operator scripts (SNOW-1505).
#
# Sourced, not run. After it, a script names each stage as it starts:
#
#     source scripts/failure.sh
#     stage "downloading swissALTI3D" \
#         "A download failed: usually a network blip." \
#         "Run the same command again. It resumes where it stopped."
#
# and on any non-zero exit (a failed command under set -e, or an explicit
# exit 1 after an error message) it prints one box saying which stage stopped,
# the command and line that failed, what a failure there usually means and
# what to do next. The person running these builds cannot see the code, so a
# bare traceback or "exit status 1" is not an answer.
#
# A script run by another that also sources this reports once: the innermost
# prints the box, and each outer one adds a single line naming its own stage.
# They share a marker file through SNOWDESK_FAILURE_MARK for that.

# Its messages are stamped with log from log.sh, found beside this file, so it
# works whether or not config.sh was sourced first.
# shellcheck source=scripts/log.sh
source "$(dirname "${BASH_SOURCE[0]}")/log.sh"

FAILURE_SCRIPT=$(basename "$0")
FAILURE_STAGE="starting up"
FAILURE_MEANS="The script stopped before its first stage, while checking its setup."
FAILURE_DO="Read the error just above. If it is not clear, send Claude the last 40 lines."
FAILURE_CMD=""
FAILURE_LINE=""

if [ -z "${SNOWDESK_FAILURE_MARK:-}" ]; then
    SNOWDESK_FAILURE_MARK=$(mktemp "${TMPDIR:-/tmp}/snowdesk-failure.XXXXXX")
    FAILURE_OWNS_MARK=1
    export SNOWDESK_FAILURE_MARK
else
    FAILURE_OWNS_MARK=0
fi

# Name the stage that starts now, what its failure usually means, and what to do.
stage() {
    FAILURE_STAGE=$1
    FAILURE_MEANS=$2
    FAILURE_DO=$3
}

_failure_on_err() {
    FAILURE_CMD=$1
    FAILURE_LINE=$2
}

_failure_on_exit() {
    local status=$1
    if [ "$status" -ne 0 ]; then
        if [ -s "$SNOWDESK_FAILURE_MARK" ]; then
            log "    (${FAILURE_SCRIPT} stopped too, while ${FAILURE_STAGE}; the box above says why)" >&2
        else
            {
                echo
                echo "=========================================================================="
                log " STOPPED: ${FAILURE_SCRIPT}, while ${FAILURE_STAGE}"
                if [ -n "$FAILURE_CMD" ]; then
                    echo "   Failed: ${FAILURE_CMD}"
                    echo "           (line ${FAILURE_LINE}, exit status ${status})"
                else
                    echo "   Failed: see the error message just above (exit status ${status})"
                fi
                echo
                echo " What it means: ${FAILURE_MEANS}"
                echo " What to do:    ${FAILURE_DO}"
                echo "=========================================================================="
            } >&2
            echo "$FAILURE_SCRIPT" >"$SNOWDESK_FAILURE_MARK"
        fi
    fi
    [ "$FAILURE_OWNS_MARK" = 1 ] && rm -f "$SNOWDESK_FAILURE_MARK"
    return 0
}

set -E
trap '_failure_on_err "$BASH_COMMAND" "$LINENO"' ERR
trap '_failure_on_exit $?' EXIT
