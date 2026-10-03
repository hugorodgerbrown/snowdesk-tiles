// The tilesets this Worker serves straight out of the bucket (SNOW-908, SNOW-987).
//
// Two of them today, and they are the same kind of object behind the same rules:
//
//   /terrain/<version>/{x}/{y}.s16          Int16 heights on the 5 m EPSG:3035
//                                           grid, sampled per point by Django.
//   /terrain-class/<version>/{z}/{x}/{y}.png  height, slope band and aspect
//                                           octant per pixel, read by the
//                                           browser (SNOW-987).
//
// Neither is left to serveObject's pass-through, for two reasons. The version
// segment has to be stripped — objects are stored unversioned so a rebuild
// replaces them in place, while a new version still gives clients new URLs past
// a year of immutable caching — and, more importantly, an absent tile has to be
// a 204 rather than a 404.
//
// That distinction is the point of the routes. Switzerland is a diagonal country
// in a rectangular grid and most of the Alps has no source yet, so roughly half
// of all tile requests are legitimately for ground nothing covers. 204 says "no
// source here" and is cached like any other tile; the pass-through's 404 is
// marked no-store, so every request outside coverage would reach R2 for ever.
// The caller must be able to tell that apart from data — absent terrain can
// never be allowed to read as flat ground.
//
// Each tileset's descriptor (grid.json, tiles.json) is the exception: it is the
// contract, so its absence is a broken publish and gets a 404 rather than being
// quietly treated as empty.
//
// Kept apart from index.js so it imports nothing, and so `node --test` can
// exercise the routing with no dependencies installed.

// Tiles are immutable within a version, like every other asset.
export const TILE_CACHE_CONTROL = "public, max-age=31536000, immutable";
// A descriptor names the current build — the mutable pointer, like the style
// document — so it gets the short TTL.
export const TILEJSON_CACHE_CONTROL = "public, max-age=3600";
// Never let a 404 be cached. One emitted before an archive or a route exists —
// a probe during a deploy, say — otherwise outlives the thing that fixed it and
// reads as a broken deploy. Cloudflare applies a short default TTL to responses
// with no Cache-Control, which is enough to mislead.
export const ERROR_CACHE_CONTROL = "no-store";

export const TILESETS = [
  {
    name: "terrain",
    prefix: "/terrain/",
    // \d+ and not -?\d+ deliberately. Tile indices count east and north from
    // the EPSG:3035 origin, and both are positive everywhere in Europe — so a
    // negative index is a caller's arithmetic bug, and 404 says so where 204
    // would look like ordinary missing coverage.
    tilePath: /^\/terrain\/[^/]+\/(\d+)\/(\d+)\.s16$/,
    tileKey: ([x, y]) => `terrain/${x}/${y}.s16`,
    contentType: "application/octet-stream",
    descriptorPath: /^\/terrain\/[^/]+\/grid\.json$/,
    descriptorKey: "terrain/grid.json",
  },
  {
    name: "terrain-class",
    prefix: "/terrain-class/",
    tilePath: /^\/terrain-class\/[^/]+\/(\d+)\/(\d+)\/(\d+)\.png$/,
    tileKey: ([z, x, y]) => `terrain-class/${z}/${x}/${y}.png`,
    contentType: "image/png",
    descriptorPath: /^\/terrain-class\/[^/]+\/tiles\.json$/,
    descriptorKey: "terrain-class/tiles.json",
    // The tiles exist at z12-14 only, which tiles.json states. Outside that a
    // 204 is the true answer — there is nothing at that zoom — and saves an R2
    // read for every tile MapLibre asks for before it has read the descriptor.
    // Keep in step with MIN_ZOOM / MAX_ZOOM in scripts/terrain_class.py.
    minZoom: 12,
    maxZoom: 14,
  },
];

/** Return the tileset a path belongs to, or null. */
export function tilesetFor(pathname) {
  return TILESETS.find((tileset) => pathname.startsWith(tileset.prefix)) ?? null;
}

/**
 * Work out what a path asks a tileset for, without touching the bucket.
 *
 * One of: the descriptor, a tile (with its bucket key), "empty" for a tile
 * that cannot exist (a zoom the set is not built at), or "not-found" for a
 * path that is not a request this tileset understands.
 */
export function resolve(tileset, pathname) {
  if (tileset.descriptorPath.test(pathname)) {
    return { kind: "descriptor", key: tileset.descriptorKey };
  }
  const match = tileset.tilePath.exec(pathname);
  if (!match) return { kind: "not-found" };

  const indices = match.slice(1);
  if (tileset.minZoom !== undefined) {
    const zoom = Number(indices[0]);
    if (zoom < tileset.minZoom || zoom > tileset.maxZoom) return { kind: "empty" };
  }
  return { kind: "tile", key: tileset.tileKey(indices) };
}

function notFound(message) {
  return new Response(message, {
    status: 404,
    headers: { "Cache-Control": ERROR_CACHE_CONTROL },
  });
}

function empty() {
  return new Response(null, {
    status: 204,
    headers: { "Cache-Control": TILE_CACHE_CONTROL },
  });
}

/** Serve one tileset request: a tile, an empty 204, or the descriptor. */
export async function serveTileset(tileset, env, url) {
  const target = resolve(tileset, url.pathname);

  if (target.kind === "not-found") return notFound("not found");
  if (target.kind === "empty") return empty();

  const object = await env.BUCKET.get(target.key);
  if (target.kind === "descriptor") {
    if (!object) return notFound(`${tileset.name} descriptor not published`);
    return new Response(object.body, {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": TILEJSON_CACHE_CONTROL,
      },
    });
  }

  if (!object) return empty();
  return new Response(object.body, {
    headers: {
      "Content-Type": tileset.contentType,
      "Cache-Control": TILE_CACHE_CONTROL,
    },
  });
}
