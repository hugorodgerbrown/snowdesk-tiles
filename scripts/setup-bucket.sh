#!/usr/bin/env bash
# One-time R2 bucket provisioning (SNOW-485).
#
# Creates the bucket, applies the committed CORS policy, and attaches the custom
# domain. Idempotent: rerun it after editing r2-cors.json to push the new policy.
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
export CLOUDFLARE_API_TOKEN

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
    exit 1
}

echo "==> creating bucket ${R2_BUCKET} (location hint ${R2_LOCATION_HINT})"
# shellcheck disable=SC2086 - $wrangler is an intentional multi-word command
run_idempotent "bucket create" "already exists" \
    $wrangler r2 bucket create "$R2_BUCKET" --location "$R2_LOCATION_HINT"

echo "==> applying CORS policy from r2-cors.json"
# If wrangler rejects the file, paste r2-cors.json into the dashboard editor
# instead (R2 > bucket > Settings > CORS Policy); it takes the same shape.
$wrangler r2 bucket cors set "$R2_BUCKET" --file r2-cors.json --force

if [ -z "$CLOUDFLARE_ZONE_ID" ]; then
    cat >&2 <<EOF

==> skipping custom domain: CLOUDFLARE_ZONE_ID is not set

The bucket and its CORS policy are in place, so you can build and upload now.
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

Add a Cache Rule so the style and sprite JSON are edge-cached. Cloudflare does
not cache JSON by default, and .pmtiles is not a recognised extension either:

    Rule name:  tiles-cache-everything
    When:       Hostname equals ${domain}
    Then:       Cache eligibility -> Eligible for cache
                Edge TTL -> Use cache-control header if present

Note the ${PMTILES_NAME} archive is larger than the 512 MB per-file cache limit
on Free/Pro/Business plans, so its Range requests will always reach R2 rather
than an edge cache. That is a latency question, not a cost one — R2 egress is
free. See README.md "Caching and cost".
EOF
