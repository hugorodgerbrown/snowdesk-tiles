// The 3-D map's elevation tiles, /dem/<version>/{z}/{x}/{y}.webp (SNOW-1444).
//
// Our own copy of Mapterhorn's Terrarium tiles (512 px lossless WebP), clipped
// to Snowdesk's supported area. snowdesk-data-pipeline's /map/3d/ reads them
// through MAP_3D_DEM_TILE_URL, and falls back to AWS's Terrarium tiles one tile
// at a time wherever this answers 204.
//
// Two things set this apart from the other tilesets on the origin.
//
// THE VERSION IS REAL. /tiles/, /terrain/ and /terrain-class/ ignore their
// version segment and rebuild in place. Here each version is its own set of
// archives under dem/<version>/ in the bucket, and stays there after the next
// refresh, so moving production back to the previous copy is an env change on
// the Django side — no rebuild, no deploy, no cache to wait out.
//
// IT IS SEVERAL ARCHIVES, NOT ONE. Mapterhorn publishes z0-12 as planet.pmtiles
// and z13+ split into one archive per z6 tile, named 6-<x>-<y>.pmtiles. The
// build keeps that split rather than merging (the pmtiles CLI has no merge, and
// the split is cheaper to rebuild in part), so this routes each tile to the
// archive that holds it: z <= 12 to planet.pmtiles, z >= 13 to the archive of its
// z6 ancestor. Only the z6 archives that touch the supported area are built, so
// a missing one is ordinary — no ground of ours there — and answers 204. A
// missing planet.pmtiles is a broken or unfinished publish, and answers an
// uncached 404 so it cannot outlive the fix.
//
// Kept apart from index.js, like tilesets.js, so `node --test` can exercise it
// with no dependencies installed: the archive reader is passed in.

import { ERROR_CACHE_CONTROL, TILE_CACHE_CONTROL } from "./tilesets.js";

export const DEM_PREFIX = "/dem/";

// Mapterhorn's split between the planet archive and the per-region ones. Keep
// in step with scripts/build-dem.sh, which extracts z0-12 from planet.pmtiles.
export const DEM_PLANET_MAX_ZOOM = 12;
// The zoom the regional archives are named by.
export const DEM_REGION_ZOOM = 6;
// The deepest zoom the build extracts (DEM_MAX_ZOOM in scripts/config.sh). The
// 3-D map's raster-dem source stops at z15 (map_3d_core.DEM_MAX_ZOOM), so a
// deeper request is answered 204 without an R2 read.
export const DEM_MAX_ZOOM = 15;

// A version is a path segment and a bucket prefix, so it is held to a plain
// token: no slashes, no dots at the start, nothing that could walk the bucket.
const DEM_PATH = /^\/dem\/([A-Za-z0-9][A-Za-z0-9_-]*)\/(\d+)\/(\d+)\/(\d+)\.webp$/;

/** The archive behind a tile could not be found in the bucket. */
export class MissingArchiveError extends Error {
  constructor(key) {
    super(`archive not found in bucket: ${key}`);
    this.name = "MissingArchiveError";
    this.key = key;
  }
}

/**
 * Work out which archive holds a /dem/ path, without touching the bucket.
 *
 * One of: a tile (its archive key, whether that archive must exist, and the
 * z/x/y to read), "empty" for a zoom the copy is not built at, or "not-found"
 * for a path that is not a tile request.
 */
export function resolveDem(pathname) {
  const match = DEM_PATH.exec(pathname);
  if (!match) return { kind: "not-found" };

  const [, version, ...indices] = match;
  const [z, x, y] = indices.map(Number);
  const size = 2 ** z;
  // An index off the grid is a caller's arithmetic bug, not missing ground.
  if (x >= size || y >= size) return { kind: "not-found" };
  if (z > DEM_MAX_ZOOM) return { kind: "empty" };

  if (z <= DEM_PLANET_MAX_ZOOM) {
    return { kind: "tile", key: `dem/${version}/planet.pmtiles`, required: true, z, x, y };
  }
  const shift = z - DEM_REGION_ZOOM;
  const rx = x >> shift;
  const ry = y >> shift;
  return {
    kind: "tile",
    key: `dem/${version}/${DEM_REGION_ZOOM}-${rx}-${ry}.pmtiles`,
    required: false,
    z,
    x,
    y,
  };
}

function empty() {
  return new Response(null, {
    status: 204,
    headers: { "Cache-Control": TILE_CACHE_CONTROL },
  });
}

function notFound(message) {
  return new Response(message, {
    status: 404,
    headers: { "Cache-Control": ERROR_CACHE_CONTROL },
  });
}

/**
 * Serve one /dem/ request.
 *
 * `openArchive(key)` returns a reader with `getZxy(z, x, y)` (a PMTiles over
 * the R2 binding, in index.js), whose reads throw MissingArchiveError when the
 * archive is absent.
 */
export async function serveDem(url, openArchive) {
  const target = resolveDem(url.pathname);
  if (target.kind === "not-found") return notFound("not found");
  if (target.kind === "empty") return empty();

  let tile;
  try {
    tile = await openArchive(target.key).getZxy(target.z, target.x, target.y);
  } catch (err) {
    if (!(err instanceof MissingArchiveError)) throw err;
    if (target.required) return notFound(`elevation copy not published: ${target.key}`);
    return empty();
  }

  // Outside the clip, inside an archive: no tile, and the same 204.
  if (!tile || !tile.data) return empty();
  return new Response(tile.data, {
    headers: {
      "Content-Type": "image/webp",
      "Cache-Control": TILE_CACHE_CONTROL,
    },
  });
}
