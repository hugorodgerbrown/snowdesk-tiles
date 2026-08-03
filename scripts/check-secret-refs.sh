#!/usr/bin/env bash
# Fail if a tracked .env* file holds a literal value instead of a reference.
#
# `.env.1password` is committed on purpose: it holds op:// references, which are
# pointers rather than secrets. That is safe, but it leaves a file in the repo
# that looks exactly like somewhere you would paste a real credential while
# debugging. This check makes that mistake fail before it lands.
#
# Only *tracked* files are scanned. An untracked, gitignored .env holding real
# values locally is fine and deliberately ignored — it cannot be committed by
# accident, which is the thing being guarded against.
#
#     ./scripts/check-secret-refs.sh              # all tracked .env* files
#     ./scripts/check-secret-refs.sh path/to/file # specific files

set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$#" -gt 0 ]; then
    files=("$@")
else
    # No tracked .env* files is a pass, not an error.
    IFS=$'\n' read -r -d '' -a files < <(git ls-files '.env*' && printf '\0') || true
fi

if [ "${#files[@]}" -eq 0 ]; then
    echo "no tracked .env* files to check"
    exit 0
fi

failures=0

for file in "${files[@]}"; do
    [ -f "$file" ] || continue
    line_number=0

    while IFS= read -r line || [ -n "$line" ]; do
        line_number=$((line_number + 1))

        # Skip blanks and comments.
        case "$line" in
            '' | \#*) continue ;;
        esac

        # Only assignments carry values; anything else is not our business.
        case "$line" in
            *=*) ;;
            *) continue ;;
        esac

        value=${line#*=}
        # Strip one layer of surrounding quotes.
        value=${value%\"}
        value=${value#\"}
        value=${value%\'}
        value=${value#\'}

        # An empty value holds nothing worth leaking.
        [ -z "$value" ] && continue

        case "$value" in
            op://*) ;;
            *)
                key=${line%%=*}
                key=${key#export }
                # Never echo the value itself — that is the whole point.
                printf '%s:%d: %s is a literal value, not an op:// reference\n' \
                    "$file" "$line_number" "$key" >&2
                failures=$((failures + 1))
                ;;
        esac
    done <"$file"
done

if [ "$failures" -gt 0 ]; then
    cat >&2 <<'EOF'

A tracked .env* file contains literal values. Committing it would put the
secret in git history, where rotating the credential is the only real fix.

Replace the value with an op:// reference, or move the file out of git.
EOF
    exit 1
fi

printf 'checked %d file(s): every value is an op:// reference\n' "${#files[@]}"
