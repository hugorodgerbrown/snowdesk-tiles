# snowdesk-tiles

Self-hosted OpenFreeMap basemap origin for Snowdesk, served at
`https://tiles.snowdesk.info/`. Removes the OpenFreeMap volunteer-tier
dependency ([SNOW-485](https://linear.app/hugorodgerbrown/issue/SNOW-485)).

A minimal Caddy server serves the four Liberty asset classes — style JSON,
sprites, glyph PBFs, and the vector-tile `.pmtiles` archive — off a Render
persistent disk with correct HTTP Range and CORS behaviour. No application
code: this repo is the Docker image, the server config, and the operational
runbook. The Django side (the `OPENFREEMAP_STYLE_URL` env var and the CSP
`connect-src` entry) lives in `snowdesk-data-pipeline` under SNOW-242.

| File | Purpose |
|------|---------|
| `Dockerfile` | Caddy 2 image carrying only the `Caddyfile`; assets live on the disk, not the image. |
| `Caddyfile` | Static serving + Range + CORS + immutable caching. Binds `$PORT`. |
| `rewrite_style.py` | Rewrites the upstream Liberty style JSON for the self-hosted origin. Stdlib only. |

## Serving model

Client-side PMTiles: the raw `.pmtiles` file is served directly and MapLibre
reads it via Range requests (`pmtiles://` source in the style). Hence the
acceptance test `curl -r … .pmtiles → 206`.

> **Frontend dependency (in `snowdesk-data-pipeline`).** A `pmtiles://` source
> requires `static/js/map.js` to register the PMTiles protocol (`pmtiles.js` →
> `maplibregl.addProtocol`). That change belongs to SNOW-242's consumption of
> this origin. If it will not land, run `protomaps/go-pmtiles` `pmtiles serve`
> instead (XYZ + TileJSON, no frontend change, but the raw `.pmtiles` is then
> not the served surface).

## Step 1 — Build a Liberty-compatible extract

The Liberty style expects the **OpenMapTiles** vector schema. Build with
**planetiler** (which emits that schema). Do **not** use
`protomaps.com/extracts` — those are Protomaps-schema and will not render with
Liberty.

```bash
# Requires Java 21+. The Geofabrik "alps" extract covers CH / AT /
# IT-South-Tyrol / FR-Alps — every current avalanche region. ~2–4 GB output.
java -Xmx8g -jar planetiler.jar --download --area=alps --output=alps.pmtiles

# Sanity-check the schema (needs go-pmtiles):
pmtiles show alps.pmtiles
```

Widen coverage later by building a larger Geofabrik area (e.g. `europe`) or the
full planet; keep the filename convention and resize the disk to fit.

## Step 2 — Mirror style, sprites, glyphs

Small and immutable. Fetch from OpenFreeMap (permissive licence). Take the
sprite set (`ofm.json`, `ofm.png`, `@2x` variants) and the Noto Sans glyph PBF
ranges the style references (`fonts/{fontstack}/{range}.pbf`).

## Step 3 — Rewrite the style JSON

```bash
python rewrite_style.py \
    --origin https://tiles.snowdesk.info \
    --pmtiles alps.pmtiles \
    > styles/liberty
```

Swaps every `tiles.openfreemap.org` reference for the new origin and repoints
the vector source at `pmtiles://https://tiles.snowdesk.info/alps.pmtiles`. Exits
non-zero (with a warning) if any upstream reference survives.

## Step 4 — Disk layout

Everything the origin serves lives on the Render persistent disk mounted at
`/data`:

```
/data
├── styles/liberty          # rewritten in step 3
├── sprites/                # ofm.json, ofm.png, @2x
├── fonts/{fontstack}/{range}.pbf
└── alps.pmtiles
```

## Step 5 — Deploy to Render

Create a **Web Service** in the Render dashboard from this repo:

1. Runtime **Docker**, root directory the repo root, region **frankfurt**
   (matches the Snowdesk services).
2. Attach a **persistent disk** (~10 GB for the Alps extract) mounted at `/data`.
3. Set the **health-check path** to `/healthz` (the Caddyfile answers it; the
   disk root would 404).
4. Populate the disk (see step 6).
5. Add the custom domain **`tiles.snowdesk.info`**; add the CNAME Render shows
   at the `snowdesk.info` DNS provider; wait for TLS to provision.

## Step 6 — Populate / refresh the disk

**v1 (manual):** after the first deploy, open the Render Shell and `curl` the
built `alps.pmtiles` and the `styles/` `sprites/` `fonts/` trees onto `/data`
from a temporary location. Refresh = rebuild (steps 1–3) and repeat.

Refresh cadence: manual / seasonal for v1. OpenFreeMap publishes monthly
snapshots; assets are immutable within a snapshot, hence the
`max-age=31536000, immutable` caching in the `Caddyfile`. Automating the refresh
is a later follow-up.

## Step 7 — Verify

```bash
# Style rewritten, no residual upstream host
curl -s https://tiles.snowdesk.info/styles/liberty | grep -c 'tiles.openfreemap.org'   # → 0

# Range on the PMTiles → 206 + Content-Range + Accept-Ranges
curl -sI -r 0-1000 https://tiles.snowdesk.info/alps.pmtiles | grep -iE '206|content-range|accept-ranges'

# Sprites + glyphs → 200
curl -sI https://tiles.snowdesk.info/sprites/ofm.png | head -1
curl -sI 'https://tiles.snowdesk.info/fonts/Noto%20Sans%20Regular/0-255.pbf' | head -1

# CORS preflight from the site origin
curl -sI -X OPTIONS \
    -H 'Origin: https://snowdesk.info' \
    -H 'Access-Control-Request-Headers: Range' \
    https://tiles.snowdesk.info/alps.pmtiles | grep -i access-control
```

## Step 8 — Production cutover (in `snowdesk-data-pipeline`, SNOW-242)

The production `connect-src` CSP currently allows only
`https://tiles.openfreemap.org` (`config/settings/base.py`). Adding
`https://tiles.snowdesk.info` there — plus reading `OPENFREEMAP_STYLE_URL` — is
SNOW-242's change and must be live before the cutover. Then on the production
service:

```
OPENFREEMAP_STYLE_URL=https://tiles.snowdesk.info/styles/liberty
BASEMAP=openfreemap_liberty
```

Load `/` and confirm no 403 / CORS / range errors in the network panel.

## Licence

Server config and scripts in this repo: choose a licence for the repo. The
served map assets (style, sprites, glyphs, tiles) are OpenFreeMap /
OpenMapTiles / OpenStreetMap data under their own permissive licences — attribute
accordingly in the map UI.
