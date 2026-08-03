// Cloudflare Worker exposing the PMTiles archive as XYZ vector tiles (SNOW-485).
//
// Why this exists, given the archive is already served as a plain object:
//
// A `pmtiles://` source needs the client to register the PMTiles protocol, and
// the Snowdesk frontend is built around discrete tile URLs — SNOW-521 resolves
// each basemap's vector-tile URL template, and SNOW-484's service worker pins
// basemap URLs for offline use. Neither can express "range reads into one 1.5 GB
// object". Serving z/x/y here keeps the style shaped like every other basemap in
// the catalogue, so the frontend needs no change at all.
//
// It also fixes a caching problem. The archive is over Cloudflare's 512 MB
// per-file cache limit, so it is never edge-cached and every range read reaches
// R2. Individual tiles are a few kB, cache normally, and are served from the
// edge on repeat reads.
//
// Routed at /tiles/* on the same hostname as the style, deliberately: the Django
// CSP derives a single connect-src origin from OPENFREEMAP_STYLE_URL, so tiles
// served from a second hostname would need a Django change and reintroduce the
// drift that derivation exists to prevent.

import { PMTiles } from "pmtiles";

// Tiles are immutable within a monthly snapshot, like every other asset.
const TILE_CACHE_CONTROL = "public, max-age=31536000, immutable";
// TileJSON names the archive, so it is the mutable pointer — same reasoning as
// the style document, same TTL.
const TILEJSON_CACHE_CONTROL = "public, max-age=3600";

const TILE_PATH = /^\/tiles\/(\d+)\/(\d+)\/(\d+)\.(mvt|pbf)$/;

/**
 * Reads byte ranges out of the archive through the R2 binding.
 *
 * The pmtiles library drives this: it reads the header, walks directories, and
 * asks for the byte window holding the tile it wants. R2 range GETs are what
 * make that cheap — no request pulls more than it needs.
 */
class R2Source {
  constructor(bucket, key) {
    this.bucket = bucket;
    this.key = key;
  }

  getKey() {
    return this.key;
  }

  async getBytes(offset, length) {
    const object = await this.bucket.get(this.key, {
      range: { offset, length },
    });
    if (!object) {
      throw new Error(`archive not found in bucket: ${this.key}`);
    }
    return { data: await object.arrayBuffer() };
  }
}

/** Echo the Origin only for allowed sites, mirroring the bucket's CORS policy. */
function corsHeaders(request, allowedOrigins) {
  const origin = request.headers.get("Origin");
  if (!origin || !allowedOrigins.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function allowedOriginList(env) {
  return (env.ALLOWED_ORIGINS ?? "")
    .split(/[\s,]+/)
    .map((value) => value.trim())
    .filter(Boolean);
}

/**
 * TileJSON describing the archive, read from its own header rather than
 * hardcoded — bounds and zoom range then follow whatever was actually built,
 * so widening the extract needs no change here.
 */
async function tileJson(archive, request, env) {
  const header = await archive.getHeader();
  const origin = new URL(request.url).origin;

  return {
    tilejson: "3.0.0",
    tiles: [`${origin}/tiles/{z}/{x}/{y}.mvt`],
    minzoom: header.minZoom,
    maxzoom: header.maxZoom,
    bounds: [
      header.minLon,
      header.minLat,
      header.maxLon,
      header.maxLat,
    ],
    attribution:
      '<a href="https://openmaptiles.org/">&copy; OpenMapTiles</a> ' +
      '<a href="https://www.openstreetmap.org/copyright">&copy; OpenStreetMap contributors</a>',
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const cors = corsHeaders(request, allowedOriginList(env));

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", { status: 405, headers: cors });
    }

    // Serve from the edge cache before touching R2. Tiles are immutable, so a
    // hit here is free and keeps Class B operations down.
    const cache = caches.default;
    const cached = await cache.match(request);
    if (cached) return cached;

    const archive = new PMTiles(new R2Source(env.BUCKET, env.PMTILES_KEY));

    let response;
    if (url.pathname === "/tiles/tiles.json") {
      response = Response.json(await tileJson(archive, request, env), {
        headers: { ...cors, "Cache-Control": TILEJSON_CACHE_CONTROL },
      });
    } else {
      const match = TILE_PATH.exec(url.pathname);
      if (!match) {
        return new Response("not found", { status: 404, headers: cors });
      }

      const [, z, x, y] = match;
      const tile = await archive.getZxy(Number(z), Number(x), Number(y));

      // A missing tile is normal — the extract is regional, and MapLibre treats
      // 204 as "nothing here" rather than an error, which 404 would surface in
      // the console on every pan outside the Alps.
      if (!tile || !tile.data) {
        response = new Response(null, {
          status: 204,
          headers: { ...cors, "Cache-Control": TILE_CACHE_CONTROL },
        });
      } else {
        response = new Response(tile.data, {
          headers: {
            ...cors,
            "Content-Type": "application/x-protobuf",
            "Cache-Control": TILE_CACHE_CONTROL,
          },
        });
      }
    }

    // Only cache responses that vary by nothing but the URL. With an Origin
    // echo in play the entry is keyed by the request, and Vary: Origin above
    // keeps a cross-origin response from being served to a different site.
    ctx.waitUntil(cache.put(request, response.clone()));
    return response;
  },
};
