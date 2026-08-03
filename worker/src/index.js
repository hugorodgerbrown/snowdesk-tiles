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
// This Worker owns the whole hostname and serves bucket objects itself for every
// non-tile path. That is not the original design: the first attempt kept the R2
// custom domain and routed only /tiles/* here, on the strength of Cloudflare's
// documented "routes take precedence if configured on the same hostname". They
// do not take precedence over an *R2* custom domain — /tiles/* was answered by
// R2's own 404 page and never reached the Worker at all.
//
// One hostname is non-negotiable rather than tidiness: the Django CSP derives a
// single connect-src origin from OPENFREEMAP_STYLE_URL, so tiles on a second
// hostname would need a Django change and reintroduce the drift that derivation
// exists to prevent.

import { PMTiles } from "pmtiles";

// Tiles are immutable within a monthly snapshot, like every other asset.
const TILE_CACHE_CONTROL = "public, max-age=31536000, immutable";
// TileJSON names the archive, so it is the mutable pointer — same reasoning as
// the style document, same TTL.
const TILEJSON_CACHE_CONTROL = "public, max-age=3600";
// Never let a 404 be cached. One emitted before an archive or a route exists —
// a probe during a deploy, say — otherwise outlives the thing that fixed it and
// reads as a broken deploy. Cloudflare applies a short default TTL to responses
// with no Cache-Control, which is enough to mislead.
const ERROR_CACHE_CONTROL = "no-store";

// /tiles/<version>/{z}/{x}/{y}.mvt — the version segment is captured and then
// ignored. It exists to give a rebuilt archive fresh URLs: tiles are immutable
// for a year and cached by URL, so replacing the archive under a fixed path
// would change nothing a client can see. Because the Worker does not act on it,
// bumping TILE_VERSION needs only the style republished, and requests for an
// older version keep working and return current data.
const TILE_PATH = /^\/tiles\/[^/]+\/(\d+)\/(\d+)\/(\d+)\.(mvt|pbf)$/;
const TILEJSON_PATH = /^\/tiles\/[^/]+\/tiles\.json$/;

// Unversioned paths, from before TILE_VERSION existed. Kept so deploying this
// Worker does not 404 clients still holding the previous style: that style is
// cached for an hour at the edge and in browsers, so it outlives the upload of
// its replacement. The two shapes cannot collide — versioned has four segments
// after /tiles/, unversioned three.
//
// Safe to delete once no request has arrived for them in a day or so.
const LEGACY_TILE_PATH = /^\/tiles\/(\d+)\/(\d+)\/(\d+)\.(mvt|pbf)$/;
const LEGACY_TILEJSON_PATH = /^\/tiles\/tiles\.json$/;

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
    // Range and the 206 metadata: needed by anything reading the archive
    // directly, and by SNOW-484's service worker to validate a cross-origin
    // partial response.
    "Access-Control-Allow-Headers": "Range",
    "Access-Control-Expose-Headers": "Content-Range, Accept-Ranges, Content-Length",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

/**
 * Attach CORS headers to a response on its way out.
 *
 * Applied *after* the cache lookup, never before it. The Cache API keys on URL,
 * and a response stored from a request carrying no Origin has no
 * `Vary: Origin` to key on — so that entry would later be handed to a
 * cross-origin request with no Access-Control-Allow-Origin, and the browser
 * would block a resource the origin is entitled to. Storing responses clean and
 * deciding CORS per request keeps the cache entry origin-independent.
 */
function withCors(response, cors) {
  if (!Object.keys(cors).length) return response;
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(cors)) headers.set(name, value);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
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
  const url = new URL(request.url);
  // Advertise tiles under the same version the TileJSON was fetched with, so a
  // client following it stays on one set of URLs.
  const segments = url.pathname.split("/");
  const version = segments.length > 3 ? segments[2] : null;

  return {
    tilejson: "3.0.0",
    tiles: [
      version
        ? `${url.origin}/tiles/${version}/{z}/{x}/{y}.mvt`
        : `${url.origin}/tiles/{z}/{x}/{y}.mvt`,
    ],
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

/**
 * Serve a bucket object for any path that is not a tile.
 *
 * Content-Type and Cache-Control come from the object's stored metadata, which
 * upload.sh set per asset class — R2 infers neither, so this is the only place
 * they exist. Range is honoured so the raw archive stays readable by byte
 * window for anything that wants it.
 */
async function serveObject(request, env) {
  const url = new URL(request.url);
  // Object keys are the decoded path: "fonts/Noto Sans Regular/0-255.pbf" is
  // requested as fonts/Noto%20Sans%20Regular/...
  const key = decodeURIComponent(url.pathname.slice(1));
  if (!key) {
    return new Response("not found", {
      status: 404,
      headers: { "Cache-Control": ERROR_CACHE_CONTROL },
    });
  }

  const range = request.headers.get("Range");
  const match = range && /^bytes=(\d+)-(\d*)$/.exec(range);
  const options = {};
  if (match) {
    const offset = Number(match[1]);
    options.range = match[2]
      ? { offset, length: Number(match[2]) - offset + 1 }
      : { offset };
  }

  const object = await env.BUCKET.get(key, options);
  if (!object) {
    return new Response("not found", {
      status: 404,
      headers: { "Cache-Control": ERROR_CACHE_CONTROL },
    });
  }

  const headers = {
    "Content-Type": object.httpMetadata?.contentType ?? "application/octet-stream",
    "Cache-Control": object.httpMetadata?.cacheControl ?? "public, max-age=3600",
    "Accept-Ranges": "bytes",
    ETag: object.httpEtag,
  };

  if (object.range && match) {
    const start = object.range.offset ?? 0;
    const end = start + (object.range.length ?? object.size) - 1;
    headers["Content-Range"] = `bytes ${start}-${end}/${object.size}`;
    return new Response(object.body, { status: 206, headers });
  }
  return new Response(object.body, { headers });
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
    // hit here is free and keeps Class B operations down. Range requests are
    // excluded: the Cache API keys on URL, so a cached full response would be
    // served for a byte-window request — the same class of bug the Cache Rule
    // caused when it stripped Range from the archive.
    const cache = caches.default;
    const cacheable = !request.headers.get("Range");
    if (cacheable) {
      const cached = await cache.match(request);
      if (cached) return withCors(cached, cors);
    }

    // Everything outside /tiles/ is a bucket object. This Worker owns the whole
    // hostname, so it has to serve them; see the header comment for why.
    if (!url.pathname.startsWith("/tiles/")) {
      return withCors(await serveObject(request, env), cors);
    }

    const archive = new PMTiles(new R2Source(env.BUCKET, env.PMTILES_KEY));

    let response;
    if (TILEJSON_PATH.test(url.pathname) || LEGACY_TILEJSON_PATH.test(url.pathname)) {
      response = Response.json(await tileJson(archive, request, env), {
        headers: { "Cache-Control": TILEJSON_CACHE_CONTROL },
      });
    } else {
      const match =
        TILE_PATH.exec(url.pathname) ?? LEGACY_TILE_PATH.exec(url.pathname);
      if (!match) {
        return new Response("not found", {
          status: 404,
          headers: { ...cors, "Cache-Control": ERROR_CACHE_CONTROL },
        });
      }

      const [, z, x, y] = match;
      const tile = await archive.getZxy(Number(z), Number(x), Number(y));

      // A missing tile is normal — the extract is regional, and MapLibre treats
      // 204 as "nothing here" rather than an error, which 404 would surface in
      // the console on every pan outside the Alps.
      if (!tile || !tile.data) {
        response = new Response(null, {
          status: 204,
          headers: { "Cache-Control": TILE_CACHE_CONTROL },
        });
      } else {
        response = new Response(tile.data, {
          headers: {
            "Content-Type": "application/x-protobuf",
            "Cache-Control": TILE_CACHE_CONTROL,
          },
        });
      }
    }

    // Cache the response *without* CORS headers, then decide CORS per request.
    // See withCors for why storing them would poison the entry.
    if (cacheable) ctx.waitUntil(cache.put(request, response.clone()));
    return withCors(response, cors);
  },
};
