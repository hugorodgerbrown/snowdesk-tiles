# snowdesk-tiles

Self-hosted OpenFreeMap basemap for Snowdesk, served at
`https://tiles.snowdesk-data.info/`. Removes the OpenFreeMap volunteer-tier
dependency ([SNOW-485](https://linear.app/hugorodgerbrown/issue/SNOW-485)).

There is no server. The basemap is a set of static objects in a Cloudflare R2
bucket behind a custom domain; MapLibre reads the vector tiles directly out of
the `.pmtiles` archive using HTTP Range requests. This repo is the build
pipeline that produces those objects, the bucket's CORS policy, and the runbook.

The Django side (the `OPENFREEMAP_STYLE_URL` env var and the CSP `connect-src`
entry) lives in `snowdesk-data-pipeline` under SNOW-242.

## Layout

| Path | Purpose |
|------|---------|
| `scripts/config.sh` | Every tunable, as an env var with a default. Sourced by the rest. |
| `scripts/build-extract.sh` | planetiler → `dist/alps.pmtiles`. Slow; rarely rerun. |
| `scripts/mirror_assets.py` | Mirrors sprites, glyphs and the Natural Earth raster from upstream. |
| `scripts/rewrite_style.py` | Repoints the Liberty style JSON at our origin and archive. |
| `scripts/build.sh` | Runs the two above to assemble `dist/`. |
| `scripts/upload.sh` | Publishes `dist/` to R2 with per-class Content-Type and Cache-Control. |
| `scripts/setup-bucket.sh` | One-time bucket + CORS + custom domain provisioning. |
| `scripts/verify.sh` | Acceptance checks against the live origin. |
| `r2-cors.json` | The bucket's CORS policy, under version control. |

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

Client-side PMTiles: the raw `.pmtiles` file is served as a single object and
MapLibre reads it via Range requests (`pmtiles://` source in the style). Hence
the `curl -r … .pmtiles → 206` check in `verify.sh`.

> **Frontend dependency (in `snowdesk-data-pipeline`).** A `pmtiles://` source
> requires `static/js/map.js` to register the PMTiles protocol (`pmtiles.js` →
> `maplibregl.addProtocol`). That change belongs to SNOW-242's consumption of
> this origin, and must be live before the cutover.

## Step 1 — Build the vector tile archive

```bash
./scripts/build-extract.sh
```

Requires Java 21+. On macOS, Homebrew's versioned JDKs are keg-only — installed
but not symlinked onto `PATH` — and `/usr/bin/java` stays Apple's stub, which
reports "Unable to locate a Java Runtime". Point `JAVA_HOME` at the cellar
rather than putting a second Java on `PATH` for everything else:

```bash
brew install openjdk@21
JAVA_HOME=$(brew --prefix openjdk@21) ./scripts/build-extract.sh
```

The Liberty style expects the **OpenMapTiles** schema, which planetiler emits;
the ready-made extracts at `protomaps.com/extracts` are Protomaps-schema and
will not render with Liberty.

The default `alps` Geofabrik area covers CH / AT / IT-South-Tyrol / FR-Alps —
every current avalanche region — and produces a 2–4 GB archive in ~20 minutes.
Widen coverage by overriding the area:

```bash
PLANETILER_AREA=europe PMTILES_NAME=europe.pmtiles ./scripts/build-extract.sh
```

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
  **about 1 GB**, and it is the slow part of the mirror. Skip it with
  `python scripts/mirror_assets.py --skip-raster` while iterating, but ship it:
  without it the basemap 404s when zoomed out.
- **The style itself**, rewritten by `rewrite_style.py` to point every sprite,
  glyph, raster and vector URL at `TILES_ORIGIN`, with the vector source
  swapped to `pmtiles://$TILES_ORIGIN/$PMTILES_NAME`. The script exits non-zero
  if any upstream reference survives.

Resulting tree, which is also the object layout of the bucket:

```
dist/
├── styles/liberty
├── sprites/ofm_f384/ofm{,@2x}.{json,png}
├── fonts/{fontstack}/{range}.pbf
├── natural_earth/ne2sr/{z}/{x}/{y}.png
└── alps.pmtiles
```

## Step 3 — Provision the bucket (once)

```bash
op run --env-file=.env.1password -- ./scripts/setup-bucket.sh
```

Creates the bucket, applies `r2-cors.json`, and attaches
`tiles.snowdesk-data.info`. Rerun it after editing the CORS policy. It prints
one remaining manual step — adding a Cache Rule — because Cloudflare does not
cache JSON or unknown extensions by default.

Needs `CLOUDFLARE_API_TOKEN`, not `wrangler login`: wrangler's OAuth flow
requires a TTY and refuses to run without one, so anything depending on it
breaks outside an interactive shell. `CLOUDFLARE_ZONE_ID` is optional — leave
it unset while the nameserver transfer is pending and the script will create
the bucket and CORS policy, then tell you how to attach the domain later.

Attaching the custom domain needs zone edit permission on the token. If yours
is R2-only, the bucket and CORS steps still succeed and the domain can be
attached from the R2 dashboard instead.

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

Storage is ~5 GB at $0.015/GB/month. R2 has no egress fees, so the running cost
is Class B operations at $0.36/million reads. Total is well under $1/month.

One caveat worth knowing: the `.pmtiles` archive is larger than Cloudflare's
512 MB per-file cache limit on Free/Pro/Business plans, so its Range requests
always reach R2 rather than an edge cache. That is a latency question, not a
cost one. The style, sprites, glyphs and raster tiles are all small enough to
cache at the edge, and SNOW-484's service worker absorbs repeat reads on the
client.

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
