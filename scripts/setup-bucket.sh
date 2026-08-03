#!/usr/bin/env bash
# One-time R2 bucket provisioning (SNOW-485).
#
# Creates the bucket, applies the committed CORS policy, and attaches the custom
# domain. Idempotent. CORS is not set here — see the note by the bucket create
# step; the allowlist lives in worker/wrangler.toml.
#
# Requires wrangler (npx wrangler) authenticated against the Cloudflare account,
# and CLOUDFLARE_ZONE_ID for the snowdesk-data.info zone.
#
# PREREQUISITE: snowdesk-data.info must be added as a zone in the same
# Cloudflare account as the bucket, on Cloudflare nameservers. R2 custom domains
# are only available for zones Cloudflare hosts — there is no CNAME-in from an
# external DNS provider below the Business plan.
#
#     CLOUDFLARE_ZONE_ID=... ./scripts/setup-bucket.sh

set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/config.sh
source scripts/config.sh

# CLOUDFLARE_ZONE_ID is optional on purpose: the bucket and its CORS policy can
# be created before the zone is live on Cloudflare, so a pending nameserver
# transfer does not block staging and uploading the assets. Rerun with the zone
# id once the transfer completes to attach the domain.
: "${CLOUDFLARE_ZONE_ID:=}"
: "${R2_LOCATION_HINT:=weur}"

wrangler=${WRANGLER:-npx wrangler}
domain=${TILES_ORIGIN#https://}

# Require a token rather than an interactive login. `wrangler login` needs a TTY
# to run its OAuth flow and refuses outright when it does not have one, so a
# script that depends on it works by hand and fails everywhere else. A token is
# also the same shape as the S3 credentials upload.sh needs — both resolve from
# 1Password, both work unchanged in CI.
#
# Checked here rather than left to wrangler so the failure names the fix once,
# instead of surfacing three times over as three different command errors.
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    cat >&2 <<'EOF'
error: CLOUDFLARE_API_TOKEN is not set

Create one at Cloudflare > Manage Account > Account API Tokens. It needs
"Workers R2 Storage:Edit" on the account. Attaching the custom domain also
needs zone edit permission — if the token lacks it, the bucket and CORS steps
still work and the domain can be attached from the R2 dashboard instead.

An R2 API token created from the R2 page shows both a "Token value" (this
variable) and an S3 access key pair (what upload.sh uses). They are different
credentials from the same item.

Then run through 1Password so the value never reaches your shell history:

    op run --env-file=.env.1password -- ./scripts/setup-bucket.sh
EOF
    exit 1
fi

# Without an account id wrangler works out which account the token belongs to by
# calling /memberships — a *user*-scoped endpoint. An R2 API token is scoped to
# one account and cannot read it, so that call fails with:
#
#     ✘ [ERROR] A request to the Cloudflare API (/memberships) failed.
#
# Naming the account skips the lookup entirely.
if [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
    cat >&2 <<'EOF'
error: CLOUDFLARE_ACCOUNT_ID is not set

Without it wrangler tries to resolve the account through /memberships, which an
account-scoped R2 token has no permission to read. Find the id on the R2
overview page in the Cloudflare dashboard.

    op run --env-file=.env.1password -- ./scripts/setup-bucket.sh
EOF
    exit 1
fi

export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID

# Treat "already exists" as success and anything else as a real failure. A bare
# `|| echo "already exists"` swallows auth errors, quota errors and typos alike,
# and reports a bucket that was never created as ready to use.
run_idempotent() {
    local label=$1 expected=$2
    shift 2
    local output
    if output=$("$@" 2>&1); then
        [ -n "$output" ] && printf '%s\n' "$output" | sed 's/^/    /'
        return 0
    fi
    if printf '%s' "$output" | grep -qi "$expected"; then
        echo "    ${label} already done — continuing"
        return 0
    fi
    printf '%s\n' "$output" >&2
    echo "error: ${label} failed" >&2

    # A 403/10000 here is a permissions problem, not a bad token: the same token
    # authenticates fine for object operations. R2 tokens come in four levels and
    # only "Admin Read & Write" can create buckets or edit bucket configuration.
    if printf '%s' "$output" | grep -qi '10000\|403\|forbidden'; then
        cat >&2 <<'EOF'

This looks like a permissions problem rather than a bad token.

R2 API tokens come in four levels. "Object Read & Write" — the right one for
upload.sh — can read and write objects but cannot create buckets or edit bucket
configuration. Creating a bucket, setting CORS and attaching a domain all need
"Admin Read & Write".

Two ways forward:

  1. Do this one-time setup in the R2 dashboard: create the bucket there and
     skip this script. Keeps the token the pipeline uses every day unable to
     delete your buckets.

  2. Create a second, admin-scoped token for setup only, and keep the object
     token for uploads.
EOF
    fi
    exit 1
}

echo "==> creating bucket ${R2_BUCKET} (location hint ${R2_LOCATION_HINT})"
# shellcheck disable=SC2086 - $wrangler is an intentional multi-word command
run_idempotent "bucket create" "already exists" \
    $wrangler r2 bucket create "$R2_BUCKET" --location "$R2_LOCATION_HINT"

# No CORS step here on purpose. A bucket CORS policy only applies to requests
# made directly to the bucket over HTTP, and nothing does that any more: the
# Worker owns the hostname and reads objects through its R2 binding, which never
# involves CORS. The allowlist lives in ALLOWED_ORIGINS in worker/wrangler.toml,
# and only there — the staging origin went missing precisely because it was
# duplicated across two files and only one of them was live.

if [ -z "$CLOUDFLARE_ZONE_ID" ]; then
    cat >&2 <<EOF

==> skipping custom domain: CLOUDFLARE_ZONE_ID is not set

The bucket exists, so you can build and upload now.
Once ${domain#*.} is active on Cloudflare nameservers, rerun with the zone id
from the zone's overview page to attach ${domain}:

    CLOUDFLARE_ZONE_ID=... ./scripts/setup-bucket.sh
EOF
    exit 0
fi

echo "==> attaching custom domain ${domain}"
# shellcheck disable=SC2086 - $wrangler is an intentional multi-word command
run_idempotent "domain attach" "already \(exists\|attached\|associated\)" \
    $wrangler r2 bucket domain add "$R2_BUCKET" \
    --domain "$domain" \
    --zone-id "$CLOUDFLARE_ZONE_ID" \
    --min-tls 1.2 \
    --force

cat <<EOF

==> Remaining manual step (Cloudflare dashboard, once)

Add a Cache Rule so the style, sprite JSON and glyph PBFs are edge-cached.
Cloudflare decides cache eligibility by file extension and recognises none of
those, so without a rule each one reaches R2 on every request.

    Rule name:  tiles-cache-everything
    When:       Custom filter expression —

        (http.host eq "${domain}" and
         not ends_with(http.request.uri.path, ".pmtiles"))

    Then:       Cache eligibility -> Eligible for cache
                Edge TTL     -> Use cache-control header if present
                Browser TTL  -> Respect origin TTL

The .pmtiles exclusion is load-bearing. Marked cache-eligible, Cloudflare
intercepts the archive, finds it over the 512 MB per-file limit, returns
cf-cache-status: BYPASS — and strips the Range header on the way through,
answering 200 with the whole ${PMTILES_NAME} body instead of 206 with the
requested window. That breaks the reader outright: MapLibre would pull the
entire archive for every tile lookup. Excluded, Range reaches R2 intact.

Browser TTL must respect the origin, or the rule rewrites Cache-Control on the
way out and overrides the per-asset values upload.sh set — including the
style's deliberately short TTL, which is what lets a new archive be swapped in.

Run ./scripts/verify.sh afterwards: it checks both that the archive still
answers 206 and that the cacheable assets report HIT.
EOF
