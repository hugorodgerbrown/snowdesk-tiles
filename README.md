# snowdesk-tiles

Self-hosted OpenFreeMap basemap for Snowdesk, served at
`https://tiles.snowdesk-data.info/`. Removes the OpenFreeMap volunteer-tier
dependency ([SNOW-485](https://linear.app/hugorodgerbrown/issue/SNOW-485)).

The basemap is a set of static objects in a Cloudflare R2 bucket, fronted by a
small Worker that also serves vector tiles as XYZ out of the `.pmtiles` archive.
This repo is the build pipeline that produces those objects, the Worker, and the
runbook.

The same origin also serves a **terrain elevation grid** — Int16 heights on a
5 m grid, sampled per point by Django rather than rendered by MapLibre
([SNOW-908](https://linear.app/hugorodgerbrown/issue/SNOW-908)). It is a
separate, one-off pipeline with its own section at the bottom.

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
| `scripts/terrain_grid.py` | The terrain grid's geometry and encoding. The contract with SNOW-917. |
| `scripts/fetch_swissalti3d.py` | Lists the swissALTI3D squares over a box, from swisstopo's STAC API. |
| `scripts/cut_terrain_tiles.py` | Cuts the warped raster into skirted Int16 tiles, and writes `grid.json`. |
| `scripts/build-terrain.sh` | Runs the three above plus GDAL → `dist/terrain/`. One-off; needs ~100 GB. |
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

- `/tiles/<version>/{z}/{x}/{y}.mvt` — vector tiles, read as byte windows out of
  the `.pmtiles` archive through an R2 binding. The archive is never fetched
  whole. The Worker ignores the version segment; it exists so a rebuilt archive
  gets fresh URLs (see below).
- `/terrain/<version>/{x}/{y}.s16` — elevation tiles, read straight out of the
  bucket with the version segment stripped. Absent tiles answer `204`, not
  `404`: most of the Alps has no source yet, so "nothing covers this ground" is
  an ordinary, cacheable answer rather than an error. `/terrain/<version>/grid.json`
  is the grid definition.
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
working files, and the output. Renting a machine for two hours is cheaper than
clearing that much space locally, and the multi-GB intermediates never touch your
disk.

Not on Cloudflare, despite everything else living there. Containers cap at 20 GB
disk and 12 GiB memory, Workers at 128 MB with a CPU-time limit, and there is no
VM product. Cloudflare holds the storage and does the serving; the build happens
elsewhere and uploads in. OpenFreeMap publishes only `planet` and `monaco`, so
there is no smaller prebuilt file to convert as a way round it — and their planet
is 101 GB of MBTiles, needing the same class of machine to convert anyway.

**What the machine needs:** ~160 GB disk, 16 GB RAM, 8 vCPU. Disk is the binding
constraint — the source, planetiler's working files and the output have to
coexist.

**Recommended: Hetzner Cloud CX42** — 8 vCPU, 16 GB RAM, 160 GB NVMe, €0.0273
per hour. Falkenstein or Nuremberg, because Geofabrik is hosted in Germany and
the 28 GB source then downloads at line speed. CX52 (16 vCPU, 32 GB, 320 GB,
€0.054/hour) if you want it finished sooner or CX42 has no capacity.

Note the **hourly** rate. Hetzner advertises monthly prices — €16.40 for CX42,
€32.40 for CX52 — but bills by the hour, so a two-hour build costs about six
cents, not forty euros. Destroy the server when it finishes and that is all you
pay.

Other providers work; they cost several times more for the same disk. Anything
with 160 GB and 16 GB RAM will do.

#### From scratch

1. **Create the server.** console.hetzner.cloud → New Project → Add Server.
   Location Falkenstein or Nuremberg, image **Ubuntu 24.04**, type **CX42**,
   and add your SSH key. Everything else default. It boots in under a minute.

2. **Get the R2 credentials to hand.** You need three values, all already in
   1Password:

   ```bash
   op item get "CloudFlare R2 - Snowdesk" --format json \
       | jq -r '.fields[] | select(.label|test("Account ID|S3 Access Key ID|S3 Secret Access Key")) | "\(.label): \(.value)"'
   ```

3. **SSH in and run the build:**

   ```bash
   ssh root@<server-ip>
   git clone https://github.com/hugorodgerbrown/snowdesk-tiles.git
   ./snowdesk-tiles/scripts/vm-build.sh
   ```

   It prompts for the three values — paste each one; they are not echoed and do
   not reach shell history. Then it installs Java and the AWS CLI, checks disk,
   sizes planetiler's heap to the box, builds, and uploads to R2. Expect one to
   two hours; run it under `tmux` if your connection is unreliable.

   The AWS CLI comes from AWS's own installer rather than `apt`: Ubuntu 24.04
   has no `awscli` package at all, so the apt line this script used to carry
   failed on the image recommended right above.

   `./scripts/vm-build.sh terrain` builds the elevation tileset instead — same
   box, same prompts, GDAL in place of Java.

4. **Destroy the server.** Hetzner console → Server → Delete. The R2 credentials
   were in its memory, and deleting the box is the cheapest rotation there is.

5. **Verify, from your laptop:**

   ```bash
   ./scripts/verify.sh
   ```

Only the archive is built and uploaded. The style, sprites, glyphs and raster are
already published and unaffected — the style names the tile URL template rather
than the archive, and the Worker resolves the archive through `PMTILES_KEY`. So
this replaces one object and changes nothing else.

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

### Bump TILE_VERSION on every rebuild

Tiles are served `immutable` for a year and cached by URL — at the edge and in
the browser. Replacing the archive under a fixed path therefore changes nothing
a client can see: the old tiles keep being served until they expire, and
`verify.sh` reports the old coverage, which reads as a failed build.

`TILE_VERSION` in `config.sh` is the path segment that fixes this. Bump it, run
`build.sh`, upload — the new archive gets new URLs and takes effect at once, with
no cache purge.

The Worker ignores the segment and always reads `PMTILES_KEY`, so a bump needs
no Worker deploy, and requests still arriving for the previous version return
current data — which matters while the old style is inside its one-hour TTL.

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
  an XYZ `tiles` array pointing at `$TILES_ORIGIN/$TILE_PATH` and the archive's
  zoom range (`TILE_MIN_ZOOM` / `TILE_MAX_ZOOM`). The script exits non-zero if
  any upstream reference survives.

  The zoom range has to be stated because the rewrite drops the upstream `url`,
  and that TileJSON is where it used to come from. A vector source with no
  `maxzoom` defaults to 22 in MapLibre, so the client requests z15+ tiles the
  archive does not hold — the Worker answers 204 — instead of overzooming z14,
  and every basemap layer disappears above z14. It also breaks Snowdesk's
  offline area downloads, which pin z10-14 and rely on overzoom below that.
  Keep the two values in step with the archive; `verify.sh` checks the published
  style against the Worker's TileJSON, which reads the PMTiles header.

  The **attribution** is stated for the same reason and was lost the same way:
  it lived in that dropped TileJSON too. Snowdesk's map legend builds its "Map
  data" section by reading `attribution` off each runtime source, so a style
  carrying none on any source renders the section as a bare heading — with the
  OpenStreetMap and OpenMapTiles credits we are obliged to show missing
  entirely ([SNOW-640](https://linear.app/hugorodgerbrown/issue/SNOW-640)). The
  rewrite writes the same string the Worker publishes in its TileJSON, and
  `verify.sh` compares the two live so a style that was not rebuilt shows up.
  The Natural Earth raster source gets its own credit. Upstream's OpenFreeMap
  credit is deliberately dropped: they no longer serve any of this data.

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

## The terrain elevation grid

A second tileset on the same origin, and a different kind of thing from the
basemap: **nothing renders it**. Django fetches tiles and reads Int16 heights
out of them to answer "what is the terrain height at this coordinate, and
therefore what is the slope angle" ([SNOW-908]). That question is what
[SNOW-910] (colouring a route line by the slope it crosses), [SNOW-911] (crux
marking) and [SNOW-839] (scoring a route against the bulletin) all need, and it
is what a pre-rendered slope overlay cannot answer — MapLibre paints those, but
nothing can read a value back out.

The Django half — the sampling API, the source registry and `TERRAIN_TILE_URL` —
is [SNOW-917], in `snowdesk-data-pipeline`. What lives here is the build and the
grid definition the two sides share.

[SNOW-908]: https://linear.app/hugorodgerbrown/issue/SNOW-908
[SNOW-910]: https://linear.app/hugorodgerbrown/issue/SNOW-910
[SNOW-911]: https://linear.app/hugorodgerbrown/issue/SNOW-911
[SNOW-839]: https://linear.app/hugorodgerbrown/issue/SNOW-839
[SNOW-917]: https://linear.app/hugorodgerbrown/issue/SNOW-917
[SNOW-693]: https://linear.app/hugorodgerbrown/issue/SNOW-693

### The licence, confirmed before anything was downloaded

SNOW-908 made this a stop condition, and it passes. swisstopo have published all
federal geodata under their responsibility as **Open Government Data since 1
March 2021**: the data "may be used, distributed and made accessible", "may be
enriched and processed", and may be "used commercially". geocat's metadata
record for swissALTI3D states the constraint as *"Opendata BY: Open use. Must
provide the source."* No authorisation is needed. So redistributing a derived,
resampled, requantised tileset is permitted.

The one obligation is attribution. swisstopo accept `Source: Federal Office of
Topography swisstopo` or `© swisstopo`; the short form is what
`terrain_grid.py` publishes, on the source entry in `grid.json`, so it travels
with the data rather than being remembered separately. Creative Commons
licences are deliberately *not* used — swisstopo state they are incompatible
with GeoIG/GeoIV — so this is not a CC-BY dataset even though the obligation
looks like one.

- [Terms of use for free geodata and geoservices (OGD)](https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices)
- [swissALTI3D](https://www.swisstopo.admin.ch/en/height-model-swissalti3d)

### What the grid is

`scripts/terrain_grid.py` is the definition, and it is the only copy. Everything
below is published in `grid.json` alongside the tiles.

| | | Why |
|---|---|---|
| Projection | EPSG:3035 (ETRS89-LAEA) | Equal-area and metric across the whole Alps. One reprojection at build time now, against rebuilding the grid the first time a source outside Switzerland is added. |
| Cell spacing | 5 m | Storage, not analysis — see below. |
| Tile | 256 × 256 cells (1280 m) | Small enough that one point sample pulls 133 kB, not half a megabyte. |
| Skirt | 1 cell on each side | So a sample in the outermost data cell can read its own neighbours. Stored size is 258 × 258. |
| Stored value | little-endian Int16, `height_m / 0.25` | See below. |
| Nodata | `-32768` | Distinct from every representable height, including 0 m. |
| Row order | north to south, west to east | A plain north-up raster, as GDAL writes one. |
| Boundary rule | half-open `[south, north)`, `[west, east)` | **Not GDAL's rule.** See below. |
| Tile indexing | east and north from the CRS origin | Both positive everywhere in Europe, so the Worker's route matches `\d+` and rejects anything else. |

Switzerland and Liechtenstein come to 27,331 populated tiles and 3.6 GB.
Every tile that exists is the same 133,128 bytes, so that total is
arithmetic rather than a measurement — see the build below.

**The stored grid and the analysis window are different numbers.** Slope is a
derivative over a neighbourhood, so it is the *window* — how far apart the two
heights you difference are — that decides which terrain features survive. A 90 m
window averages away the 40 m steep step that catches people; a 6 m window
measures boulders. Because heights are stored rather than slope, the window is a
read-time choice: a 3×3 neighbourhood here is a 15 m window, 5×5 is 25 m,
box-filter to 10 m first and you have a 30 m window, all from the same bytes.
Storing coarser would have foreclosed every window below the storage spacing
permanently, to save disk we are not paying for. The default window is 10 m,
because that is what swisstopo compute `ch.swisstopo.hangneigung-ueber_30` at and
SNOW-910's route line has to agree with the raster underneath it.

**Int16 at a 0.25 m scale, not Int16 metres.** Two bytes either way. Rounding to
whole metres puts ±0.5 m of independent noise on each cell, which differenced
across a 10 m window is ±1 m on the rise — at a true 35° that spans 31.0° to
38.7°. The thresholds this data exists to resolve are 5° apart, so metre
quantisation would be larger than the distinction being drawn. At 0.25 m the
same worst case is ±1.0°, and the step sits below swissALTI3D's own vertical
accuracy (0.3–0.5 m from LiDAR, 1–3 m from stereo correlation above 2000 m)
rather than above it.

**The boundary rule is not GDAL's, on purpose.** A coordinate landing exactly on
a cell boundary belongs to the cell *north and east* of it. GDAL resolves a
boundary northing downwards instead, but `tile_for` puts a coordinate on a
tile's south edge inside that tile — so the southernmost row has to own its own
south edge, or tiles and cells would disagree along every tile boundary in the
grid. It only bites on exact multiples of 5 m, and it moves the answer by one
cell.

### The contract with SNOW-917

The grid definition is the deliverable the Django side depends on, and the two
live in different repositories. A grid rebuilt with different geometry and a
sampler still applying the old numbers does not fail — it returns plausible,
silently wrong heights. Three things keep that from happening:

1. `grid.json` is published with the tiles and carries every number needed to
   decode them. SNOW-917 reads it rather than hardcoding anything.
2. `verify.sh` compares the published `grid.json` against
   `python3 scripts/terrain_grid.py definition` on every run, so a rebuild whose
   definition moved shows up as a failure rather than as odd slope angles.
3. `TERRAIN_VERSION` is in the tile URL. Bump it for **any** change to the grid
   definition, not just to the heights — tiles are cached immutable for a year
   by URL, so a client holding tiles cut on one geometry and reading them under
   another decodes nonsense. The Worker ignores the segment and always reads
   `terrain/{x}/{y}.s16`, so a bump needs a rebuilt `grid.json` and no deploy.

Outside coverage the Worker answers **204, never 404 and never a height**. That
distinction is load-bearing: SNOW-839 and SNOW-910 both turn on an absent answer
never rendering as gentle ground. The rule is stated in `grid.json` itself so it
travels with the data.

### Build it

```bash
./scripts/build-terrain.sh
```

One-off and slow. Terrain does not move on human timescales, so unlike the
basemap there is no schedule and no refresh cadence — the only things that would
ever trigger a re-run are swisstopo's six-yearly re-survey and a change to our
own parameters, both years apart. That is exactly why it is a committed script
rather than a sequence someone performed once: a re-run should be a diff, not an
archaeology exercise.

Needs GDAL (`apt install gdal-bin`, `brew install gdal`) and Python 3.12+. It
does five things, each skipped if its output is already there, so an interrupted
run resumes:

1. List the swissALTI3D squares over `TERRAIN_BBOX` from swisstopo's STAC API,
   taking the **2 m** GeoTIFF of the four assets on each item, one per square
   kilometre, newest survey wins.
2. Download them — 43,650 files, ~44 GB, and the long pole by a distance. The
   catalogue holds about twice that many items: the 2026-09-13 build listed
   80,485 and dropped 36,835 as superseded.
3. `gdalwarp` to EPSG:3035 at 5 m with `-r average`. Averaging is the whole
   reason for taking the 2 m source rather than a coarser one.
4. `gdal_translate` to a flat Int16 ENVI raster, quantised onto the stored scale
   in one exact linear step. The `-scale` endpoints come from `terrain_grid.py`,
   so the encoding cannot drift from the encoding SNOW-917 decodes with.
5. Cut tiles. Because the raster arrives pre-quantised and tile-aligned, this is
   pure byte slicing — no arithmetic per cell, which is what keeps numpy and
   GDAL's Python bindings out of this repo entirely.

Budget ~100 GB of disk and about **90 minutes**. Measured on 2026-09-13 on a
CX42 (8 vCPU): roughly 20 minutes to page the catalogue, 20 to download at eight
parallel curls, and the warp the longest single stage; the quantise and the cut
are fast by comparison. Peak disk was 56 GB in `work/terrain` alongside 3.6 GB
of tiles. Same advice as the basemap: rent a box rather than clearing that much
space locally. It wants disk and cores, not planetiler's 16 GB of RAM.

On a VM that is one command, which installs GDAL, prompts for the R2
credentials and publishes when the build finishes:

```bash
./scripts/vm-build.sh terrain
```

That build covered Switzerland with 43,650 squares surveyed between 2019 and
2025 — swisstopo re-survey on a six-year cycle, so newest-per-square makes the
grid a deliberate patchwork of vintages rather than an accidental one. It
produced a 71,680 × 49,152 cell grid and **27,331 tiles, 3.6 GB**, out of 53,760
tile slots: the rest are empty because Switzerland is diagonal in a rectangular
grid, and an absent tile answers 204.

That 3.6 GB is exact, not rounded off a `du`: a tile is a fixed 133,128 bytes
whether it is Jungfraujoch or Lake Geneva, so the tileset is 27,331 × 133,128 =
**3,638,521,368 bytes**. Quote it in decimal GB, which is the unit R2 bills in;
`du -h` reports the same bytes as 3.4 GiB, and reading one number in one unit
against the other is how this figure came to be written down two different ways.

While iterating, build one region — a 66 km² box around Zermatt runs end to end
in 22 seconds, which makes it a cheap way to prove the whole pipeline before
committing to the full download:

```bash
TERRAIN_BBOX="7.70 45.98 7.80 46.05" ./scripts/build-terrain.sh
```

That box is a useful smoke test because the answers are checkable: Zermatt
village reads 1608.5 m against a published 1,608 m, the Gornergrat ridge 3124 m,
and the whole grid spans 1459.5–3391.5 m — which is the right range for that
valley. One number landing on the village pins the projection, the tile and cell
addressing, the row order and the height scale all at once; each of those is
individually capable of returning a plausible height for every coordinate while
being wrong.

`work/` holds the intermediates and is gitignored; delete it once the tiles are
published.

### Publish and verify

```bash
op run --env-file=.env.1password -- ./scripts/upload.sh
./scripts/verify.sh
```

`upload.sh` syncs the tiles as `application/octet-stream`, immutable for a year,
and copies `grid.json` last with a one-hour TTL — the same ordering rule as the
style, for the same reason.

It publishes whatever is staged in `dist/` and leaves the rest alone, so the box
that ran the build can publish the tileset itself with the R2 credentials in its
environment — `build-terrain.sh` stages `dist/terrain/` and nothing else, and an
absent style or mirror means "not rebuilt", never "delete it". That matters
because the full build wants a rented machine, and the alternative is mirroring
~1 GB of unchanged assets on it purely to satisfy a precondition.

`verify.sh` checks the definition against this repo's, reads heights at named
places and asserts them against known ground (two lake surfaces, which are flat
and known to the metre, then the range of the country from Basel to the
Jungfraujoch), and confirms that a coordinate outside coverage answers 204 while
a negative tile index answers 404. A grid that is offset, flipped north-south or
built in the wrong projection still returns a plausible height for every
coordinate; comparing against known ground is the only thing that catches it.

`TERRAIN=0 ./scripts/verify.sh` skips the section. That is for shipping a
basemap-only change before the terrain build has ever been run — not a way past
a failure.

### Coverage, and what is honestly missing

The grid is Alps-wide from day one and only the Swiss part has data in it.
Adding a source later ([SNOW-693], Copernicus GLO-30) is "resample a coarser
source onto the existing grid"; the grid itself never moves. A source is a
registry entry in `terrain_grid.py` — coverage, quality tier, native resolution,
licence, attribution — and native resolution is recorded separately from cell
spacing because they are not the same claim. A 30 m source on a 5 m grid is
upsampling, which is honest only so long as nothing downstream reads 5 m cells
as 5 m of information.

The accepted limitation, stated plainly: **the display overlay covers more
ground than the sampling grid**. In the Vanoise or the Écrins a user will see
slope shading under an uncoloured route line. That is not a correctness bug
while unknown never renders as gentle — and it is the strongest argument for
GLO-30 as source number two.

### Cost

Storage is the only ongoing cost and it is the reason the grid can be this fine.
3.6 GB in R2 is about five cents a month, egress is free, and the writes are
one-off. There is no dyno, no schedule and nothing to keep alive.

## Development

```bash
tox
```

Runs formatting, lint, type-check and tests. The Python here is stdlib-only
operational tooling — no Django, no runtime dependencies.

## Licence

Server config and scripts in this repo: choose a licence for the repo. The
served map assets (style, sprites, glyphs, tiles) are OpenFreeMap /
OpenMapTiles / OpenStreetMap data under their own permissive licences.

The credits the map UI has to show ride on the style's sources — see step 2.
OpenStreetMap's ODbL and the OpenMapTiles terms both require attribution, and
the client has nowhere else to read it from; the OpenFreeMap style, sprites and
glyphs are BSD-licensed and need no UI credit.

The terrain grid is derived from **swissALTI3D**, swisstopo free geodata (OGD).
Redistributing a derived tileset is permitted; indicating the source is not
optional. `© swisstopo` rides on the source entry in `grid.json`, so it travels
with the data — anything surfacing a height or a slope derived from it has to
show it. See the terrain section above for the wording swisstopo accept and why
this is not a Creative Commons licence.
