# snowdesk-tiles

Self-hosted OpenFreeMap basemap for Snowdesk, served at
`https://tiles.snowdesk-data.info/`. Removes the OpenFreeMap volunteer-tier
dependency ([SNOW-485](https://linear.app/hugorodgerbrown/issue/SNOW-485)).

The basemap is a set of static objects in a Cloudflare R2 bucket, fronted by a
small Worker that also serves vector tiles as XYZ out of the `.pmtiles` archive.
This repo is the build pipeline that produces those objects, the Worker, and the
runbook.

The Django side (the `OPENFREEMAP_STYLE_URL` env var and the CSP `connect-src`
entry) lives in `snowdesk-data-pipeline` under SNOW-242.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/config.sh` | Every tunable, as an env var with a default. Sourced by the rest. |
| `scripts/build-extract.sh` | planetiler → `dist/snowdesk.pmtiles`. Slow; needs ~100 GB disk. |
| `scripts/mirror_assets.py` | Mirrors sprites, glyphs and the Natural Earth raster from upstream. |
| `scripts/rewrite_style.py` | Repoints the Liberty style JSON at our origin and archive. |
| `scripts/build.sh` | Runs the two above to assemble `dist/`. |
| `scripts/upload.sh` | Publishes `dist/` to R2 with per-class Content-Type and Cache-Control. |
| `scripts/setup-bucket.sh` | One-time bucket creation. |
| `scripts/vm-build.sh` | Builds the archive on a throwaway VM and uploads it to R2. |
| `scripts/verify.sh` | Acceptance checks against the live origin. |
| `worker/` | Worker serving XYZ tiles, and the CORS allowlist (`ALLOWED_ORIGINS`). |

## Why R2 and not an origin server

An earlier revision of this repo served the same assets from Caddy on a Render
web service with a persistent disk. R2 is better on every axis that matters
here: assets are on Cloudflare's edge rather than in one region, there is no
instance to keep alive, no disk to resize, no cold-start sync of a multi-GB
archive, and no single-instance/no-zero-downtime constraint that a Render disk
forces. It is also about 20× cheaper. The Caddy version is in git history at
`c0cfc9e` if it is ever needed.

### Why a separate domain

The tiles are served from `snowdesk-data.info`, not a subdomain of the site.
Two reasons:

1. **Cookies.** A cookie scoped to `.snowdesk.info` would be attached to every
   one of the hundreds of Range requests a single map session fires at the
   archive. A separate registrable domain makes that structurally impossible,
   whatever `SESSION_COOKIE_DOMAIN` is set to later.
2. **DNS blast radius.** R2 custom domains require the zone to be on Cloudflare
   nameservers — there is no CNAME-in from an external provider below the
   Business plan. Using `snowdesk-data.info` keeps production DNS for
   `snowdesk.info` where it is.

This costs nothing in integration: `tiles.snowdesk.info` would have been a
distinct origin from `snowdesk.info` under the same-origin policy anyway, so
the CORS policy and the CSP `connect-src` entry are identical either way.

### Prerequisite

**`snowdesk-data.info` must be added as a zone in the same Cloudflare account
as the bucket**, on Cloudflare nameservers (full setup). Do that before running
`setup-bucket.sh`. The `*.r2.dev` fallback subdomain is rate-limited and not
supported for production traffic.

## Serving model

The Worker (`worker/`) owns the hostname and answers everything:

- `/tiles/{z}/{x}/{y}.mvt` — vector tiles, read as byte windows out of the
  `.pmtiles` archive through an R2 binding. The archive is never fetched whole.
- Everything else — style, sprites, glyphs, the Natural Earth raster — passed
  through to the bucket, returning each object's stored `Content-Type` and
  `Cache-Control` (the values `upload.sh` set; R2 infers neither).

CORS is the Worker's too, from `ALLOWED_ORIGINS`. The bucket has no custom
domain and is reached only through the binding, so its own CORS policy would
never apply.

The style therefore carries an ordinary XYZ `tiles` array, not a `pmtiles://`
URL, and **the frontend needs no change** — `OPENFREEMAP_STYLE_URL` is the only
thing that moves.

That is the reason for the Worker. Client-side PMTiles was the original design,
but it needs `maplibregl.addProtocol` *and* it hands the frontend a source with
no tile URLs. Snowdesk's map cannot work with that: SNOW-521 resolves each
basemap's vector-tile URL template and SNOW-484's service worker pins basemap
URLs for offline use, and neither can express range reads into a single
multi-GB object.

It also fixes the caching problem. The archive is over Cloudflare's 512 MB
per-file limit and so is never edge-cached — every range read reaches R2.
Individual tiles are a few kB, cache normally, and are served from the edge on
repeat reads.

Both halves must sit on the same hostname. The Django CSP derives a single
`connect-src` origin from `OPENFREEMAP_STYLE_URL`, so tiles served from a
second hostname would need a Django change and reintroduce exactly the drift
that derivation exists to prevent.

## Step 1 — Build the vector tile archive

Use a cloud VM — see below. To run it locally anyway you need ~100 GB free and
Java 21+:

```bash
JAVA_HOME=$(brew --prefix openjdk@21) ./scripts/build-extract.sh
```

On macOS, Homebrew's versioned JDKs are keg-only — installed but not symlinked
onto `PATH` — and `/usr/bin/java` stays Apple's stub reporting "Unable to locate
a Java Runtime", hence `JAVA_HOME`.

### Build it on a cloud VM, not your laptop

The build needs **~100 GB of free disk** — a ~28 GB `europe` source, planetiler's
working files, and the output. Renting a machine for two hours is cheaper and
faster than clearing that much space locally, and the multi-GB intermediates
never touch your disk.

**Recommended: Hetzner Cloud CPX41** — 8 vCPU, 16 GB RAM, 240 GB NVMe, about
€0.05/hour in Falkenstein or Nuremberg. Two reasons beyond price: Geofabrik is
hosted in Germany, so the 28 GB source download runs at line speed, and hourly
billing means a two-hour build costs about €0.10. AWS and DigitalOcean equivalents
cost several times more and give less disk. CCX33 (dedicated vCPU, 32 GB RAM) is
the upgrade if you want it finished sooner.

Create it with Ubuntu 24.04, then:

```bash
CLOUDFLARE_ACCOUNT_ID=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
    ./scripts/vm-build.sh
```

That installs Java and the AWS CLI, sizes planetiler's heap to the box, builds
the archive, and uploads it straight to R2. **Only the archive is built and
uploaded** — the style, sprites, glyphs and raster are already published and are
unaffected, because the style names the tile URL template rather than the
archive, and the Worker resolves the archive through `PMTILES_KEY`.

Use an R2 token scoped to Object Read & Write, and **destroy the VM afterwards**:
the credentials are in its shell history and environment, and deleting the box is
the cheapest rotation there is.

Then, locally:

```bash
./scripts/verify.sh
```

### The bounding box matches the live map

`PLANETILER_BOUNDS` defaults to `1.0,42.0,18.0,50.5` — the extent the map is
actually used at, roughly Paris to Zagreb and Luxembourg to central Italy. It
covers all of Switzerland and Austria, northern Italy, the French Alps, southern
Germany and Slovenia.

The region reference data also lists Pyrenees (FR-64…FR-74) and Corsica
(FR-40/41) regions, which are **not** served on the map and are deliberately
outside this box. If that ever changes, the box has to change with it — neither
is anywhere near it.

### Use bounds, never a Geofabrik area alone

A Geofabrik area is clipped to a **polygon**. Tiles outside it are still
generated and served — they are simply empty — so every HTTP check passes while
the map renders blank. Under `--area=alps`, Basel returned a 0-byte tile and the
Jura was effectively absent, alongside the whole Pyrenees and Corsica.
`--bounds` keeps a true rectangle, which has no such holes.

The Liberty style expects the **OpenMapTiles** schema, which planetiler emits;
the ready-made extracts at `protomaps.com/extracts` are Protomaps-schema and
will not render with Liberty.

## Step 2 — Mirror the remaining assets and rewrite the style

```bash
./scripts/build.sh
```

This mirrors four things from upstream into `dist/`, keyed on the URL path so
that the only thing the style rewrite has to change is the hostname:

- **Sprites** — `ofm.json` / `ofm.png` and their `@2x` variants.
- **Glyphs** — the Noto Sans PBF ranges for each fontstack the style uses.
  Ranges upstream does not publish 404 and are skipped; that is expected.
- **Natural Earth raster** — Liberty layers a shaded-relief raster under the
  vector data at low zoom (`ne2_shaded`, maxzoom 6). That is 5,461 tiles and
  ~310 MB, and it is the slow part of the mirror by request count. Skip it with
  `python scripts/mirror_assets.py --skip-raster` while iterating, but ship it:
  without it the basemap 404s when zoomed out.
- **The style itself**, rewritten by `rewrite_style.py` to point every sprite,
  glyph, raster and vector URL at `TILES_ORIGIN`, with the vector source given
  an XYZ `tiles` array pointing at `$TILES_ORIGIN/$TILE_PATH`. The script exits
  non-zero if any upstream reference survives.

Resulting tree, which is also the object layout of the bucket:

```
dist/
├── styles/liberty
├── sprites/ofm_f384/ofm{,@2x}.{json,png}
├── fonts/{fontstack}/{range}.pbf
├── natural_earth/ne2sr/{z}/{x}/{y}.png
└── snowdesk.pmtiles
```

## Step 3 — Provision the bucket (once)

```bash
op run --env-file=.env.1password -- ./scripts/setup-bucket.sh
```

Creates the bucket and prints the one remaining manual step — adding a Cache
Rule — because Cloudflare does not cache JSON or unknown extensions by default.

It no longer sets a bucket CORS policy. A bucket CORS policy applies only to
requests made directly to the bucket over HTTP, and nothing does that: the
Worker owns the hostname and reads objects through its R2 binding. The allowlist
lives in `ALLOWED_ORIGINS` in `worker/wrangler.toml`, and only there — the
staging origin went missing because it was duplicated across two files and only
one of them was live.

Needs `CLOUDFLARE_API_TOKEN`, not `wrangler login`: wrangler's OAuth flow
requires a TTY and refuses to run without one, so anything depending on it
breaks outside an interactive shell. It also needs `CLOUDFLARE_ACCOUNT_ID` —
without one wrangler resolves the account through `/memberships`, a
user-scoped endpoint an account-scoped R2 token cannot read. `CLOUDFLARE_ZONE_ID` is optional — leave
it unset while the nameserver transfer is pending and the script will create
the bucket and CORS policy, then tell you how to attach the domain later.

Attaching the custom domain needs zone edit permission on the token. If yours
is R2-only, the bucket and CORS steps still succeed and the domain can be
attached from the R2 dashboard instead.

## Step 3b — Deploy the tile Worker

```bash
cd worker && npm install && npx wrangler deploy
```

**Remove the R2 custom domain first** (R2 → `snowdesk-tiles` → Settings →
Custom Domains). Otherwise it keeps answering and the Worker is never invoked —
see below. Deploying recreates the DNS record pointing at the Worker.

Needs a token with Workers edit permission; the R2 object token used for uploads
is not enough. `PMTILES_KEY` in `worker/wrangler.toml` must match `PMTILES_NAME`
in `scripts/config.sh`, and the tile route must match `TILE_PATH`.

### Why the Worker serves the bucket objects too

The obvious arrangement — keep the R2 custom domain, route only `/tiles/*` to
the Worker — does not work. Cloudflare documents that routes "take precedence if
configured on the same hostname", but that does not hold against an *R2* custom
domain: `/tiles/0/0/0.mvt` was answered by R2's own 404 page and never reached
the Worker.

So the Worker owns the hostname and passes non-tile paths through to the bucket
binding, returning each object's stored `Content-Type` and `Cache-Control`. Those
still come from what `upload.sh` set — R2 infers neither, and the Worker does not
override them.

Splitting across two hostnames would avoid this, and is the one option ruled out:
the CSP derives a single origin from `OPENFREEMAP_STYLE_URL`.

## Step 4 — Publish

```bash
op run --env-file=.env.1password -- ./scripts/upload.sh
```

Credentials come from an R2 API token. `.env.1password` holds 1Password
*references*, not values — `op run` resolves them at launch and injects them
into the child process only, so nothing is written to disk and no secret lands
in shell history. Adjust the vault and item names in that file to match your
setup. Any other mechanism works too, as long as the three variables are in the
environment:

```bash
CLOUDFLARE_ACCOUNT_ID=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
    ./scripts/upload.sh
```

Uses the AWS CLI against R2's S3-compatible endpoint, not wrangler:
`wrangler r2 object put` caps at 315 MB and the archive is several GB, so
multipart upload is required.

Content-Type is set explicitly per asset class — R2 does not infer it, and a
`.pmtiles` or `.pbf` served as the wrong type breaks the client reader.
Cache-Control is object metadata fixed at upload time: everything gets
`immutable` for a year except the style, which is the mutable pointer naming
the current archive and gets an hour.

The style is uploaded **last**, so a client fetching the new style never sees it
before the tiles it references exist.

If the upload fails with a header error naming a checksum — `Header
'x-amz-checksum-crc32' not implemented`, `CRC64NVME not implemented`, or an
`XAmzContentChecksumMismatch` — that is the AWS CLI adding checksum headers R2
has not always accepted, not a problem with the assets or credentials. Retry
with checksums off:

```bash
AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
    op run --env-file=.env.1password -- ./scripts/upload.sh
```

`sync` skips what already transferred, so a retry resumes rather than restarts.

## Step 5 — Verify

```bash
./scripts/verify.sh
```

Checks the style resolves with no residual upstream references, that the archive
answers a Range request with `206`, that sprites and glyphs are reachable, and
that CORS headers come back for the site origin. Exits non-zero on the first
failure, so it can gate a release.

## Step 6 — Production cutover (in `snowdesk-data-pipeline`, SNOW-242)

The production `connect-src` CSP currently allows only
`https://tiles.openfreemap.org` (`config/settings/base.py`). Adding
`https://tiles.snowdesk-data.info` there — plus reading `OPENFREEMAP_STYLE_URL` and
registering the PMTiles protocol in `map.js` — is SNOW-242's change and must be
live first. Then on the production service:

```
OPENFREEMAP_STYLE_URL=https://tiles.snowdesk-data.info/styles/liberty
BASEMAP=openfreemap_liberty
```

Load `/` and confirm no 403 / CORS / range errors in the network panel.

## Refreshing

OpenFreeMap publishes monthly snapshots; assets are immutable within one, hence
the year-long `Cache-Control`. Refresh cadence is manual/seasonal for v1: rerun
steps 1, 2 and 4. Automating it is a follow-up.

To swap archives without a stale-cache window, upload under a new
`PMTILES_NAME` and let the style — which has a one-hour TTL — cut over to it.

## Caching and cost

Measured sizes for the superseded `alps` build, as an order-of-magnitude guide.
The region-derived box covers a much larger area, so expect the archive to be
several times bigger:

| | Size | Objects |
|---|---|---|
| `alps.pmtiles` (superseded) | 1.5 GB | 1 |
| `natural_earth/` | 311 MB | 5,461 |
| `fonts/` | 101 MB | 768 |
| `sprites/` + `styles/` | 332 KB | 5 |

Storage is $0.015/GB/month, so even a 20 GB archive is about $0.30. R2 has no
egress fees. The running cost is per-request: R2 Class B operations at
$0.36/million, plus Cloudflare Workers.

**Workers requests are the figure to watch.** Because the R2 custom domain would
not yield to a path-scoped route, the Worker owns the hostname and every request
goes through it — not just tiles, but glyphs, sprites and the style too. The
free tier is 100k requests/day, and a single map session pulls dozens of tiles
plus glyph ranges. That is comfortable for current traffic and would not survive
a busy day at 10× it; check the Workers dashboard before assuming otherwise.
Beyond the free tier it is $0.30/million.

The edge cache absorbs most repeat reads before they reach either meter: tiles
and glyphs are immutable and cache normally. It is the cold, wide-ranging
sessions that cost.

### The archive is no longer on the hot path

Vector tiles now come from the Worker, which reads byte windows out of the
archive through the R2 binding. The archive is still served whole at
`/snowdesk.pmtiles` — `verify.sh` checks its Range behaviour, and it remains the
thing to point a `pmtiles://` client at — but no browser fetches it during
normal map use.

### The archive must be excluded from the Cache Rule

The Cache Rule that makes the style, glyphs and sprite JSON edge-cacheable must
**not** match `*.pmtiles`. Marked cache-eligible, Cloudflare intercepts the
archive, finds it over the 512 MB per-file limit on Free/Pro/Business, returns
`cf-cache-status: BYPASS` — and strips the `Range` header on the way through,
answering `200` with the entire 1.5 GB body instead of `206` with the requested
window. MapLibre then pulls the whole archive for every tile lookup.

This is the CDN behaviour SNOW-485 predicted would break PMTiles, and it is
easy to reintroduce: the rule looks correct, every asset still returns 200, and
only the status code on a Range request gives it away. `verify.sh` checks for
it. The archive is uncacheable at this size regardless, so excluding it costs
nothing — R2 egress is free, and SNOW-484's service worker absorbs repeat reads
on the client.

## Development

```bash
tox
```

Runs formatting, lint, type-check and tests. The Python here is stdlib-only
operational tooling — no Django, no runtime dependencies.

## Licence

Server config and scripts in this repo: choose a licence for the repo. The
served map assets (style, sprites, glyphs, tiles) are OpenFreeMap /
OpenMapTiles / OpenStreetMap data under their own permissive licences —
attribute accordingly in the map UI.
