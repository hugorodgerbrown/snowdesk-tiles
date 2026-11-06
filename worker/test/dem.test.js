// Routing tests for the 3-D map's elevation tiles (SNOW-1444).
//
//     cd worker && npm test
//
// node:test and a fake archive reader — no dependencies, so this runs without
// `npm install`. What is pinned: each zoom reaches the archive that holds it
// (planet.pmtiles to z12, the z6 ancestor's archive from z13), the version is
// part of the bucket key so two copies can live side by side, a tile outside
// the copy is a cacheable 204, and a missing planet archive is a loud,
// uncached 404 rather than a year of cached 204s.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEM_MAX_ZOOM,
  MissingArchiveError,
  isDemManifest,
  resolveDem,
  serveDem,
} from "../src/dem.js";
import { ERROR_CACHE_CONTROL, TILE_CACHE_CONTROL } from "../src/tilesets.js";

/** A reader over `archives` ({key: {"z/x/y": bytes}}), recording each key opened. */
function reader(archives) {
  const opened = [];
  const open = (key) => {
    opened.push(key);
    return {
      async getZxy(z, x, y) {
        if (!(key in archives)) throw new MissingArchiveError(key);
        const data = archives[key][`${z}/${x}/${y}`];
        return data ? { data } : undefined;
      },
    };
  };
  return { open, opened };
}

async function serve(path, archives = {}) {
  const { open, opened } = reader(archives);
  const response = await serveDem(new URL(`https://tiles.test${path}`), open);
  return { response, opened };
}

// Zermatt: z12 2136/1456, z15 17089/11654; its z6 ancestor is 33/22.
const ZERMATT_Z12 = "/dem/2026-11/12/2136/1456.webp";
const ZERMATT_Z15 = "/dem/2026-11/15/17089/11654.webp";

test("z12 and below read the version's planet archive", () => {
  assert.deepEqual(resolveDem(ZERMATT_Z12), {
    kind: "tile",
    key: "dem/2026-11/planet.pmtiles",
    required: true,
    z: 12,
    x: 2136,
    y: 1456,
  });
  assert.equal(resolveDem("/dem/2026-11/0/0/0.webp").key, "dem/2026-11/planet.pmtiles");
});

test("z13 and above read the archive of their z6 ancestor", () => {
  const target = resolveDem(ZERMATT_Z15);
  assert.equal(target.key, "dem/2026-11/6-33-22.pmtiles");
  assert.equal(target.required, false);
  assert.equal(resolveDem("/dem/2026-11/13/4272/2913.webp").key, "dem/2026-11/6-33-22.pmtiles");
});

test("the version is part of the key, so two copies sit side by side", () => {
  assert.equal(resolveDem("/dem/2026-05/12/2136/1456.webp").key, "dem/2026-05/planet.pmtiles");
  assert.notEqual(resolveDem("/dem/2026-05/12/2136/1456.webp").key, resolveDem(ZERMATT_Z12).key);
});

test("anything that is not a tile path is not found", () => {
  for (const path of [
    "/dem/2026-11/12/2136/1456.png",
    "/dem/12/2136/1456.webp",
    "/dem/../12/2136/1456.webp",
    "/dem/.hidden/12/2136/1456.webp",
    "/dem/2026-11/12/4096/0.webp",
    "/dem/2026-11/planet.pmtiles",
  ]) {
    assert.equal(resolveDem(path).kind, "not-found", path);
  }
});

test("a copy's manifest is told apart, for index.js to serve from the bucket", () => {
  assert.equal(isDemManifest("/dem/2026-11/manifest.json"), true);
  for (const path of [
    "/dem/manifest.json",
    "/dem/../manifest.json",
    "/dem/2026-11/sub/manifest.json",
    "/dem/2026-11/planet.pmtiles",
    ZERMATT_Z12,
  ]) {
    assert.equal(isDemManifest(path), false, path);
  }
});

test("a zoom past the copy is 204 without opening an archive", async () => {
  const { response, opened } = await serve(`/dem/2026-11/${DEM_MAX_ZOOM + 1}/0/0.webp`);
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("Cache-Control"), TILE_CACHE_CONTROL);
  assert.deepEqual(opened, []);
});

test("a tile in the copy is webp, immutable", async () => {
  const { response } = await serve(ZERMATT_Z12, {
    "dem/2026-11/planet.pmtiles": { "12/2136/1456": new Uint8Array([1, 2, 3]) },
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "image/webp");
  assert.equal(response.headers.get("Cache-Control"), TILE_CACHE_CONTROL);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), new Uint8Array([1, 2, 3]));
});

test("a tile outside the clip, inside an archive, is 204", async () => {
  const { response } = await serve(ZERMATT_Z12, { "dem/2026-11/planet.pmtiles": {} });
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("Cache-Control"), TILE_CACHE_CONTROL);
});

test("a regional archive that was not built is 204: no ground of ours there", async () => {
  const { response, opened } = await serve("/dem/2026-11/15/0/0.webp", {
    "dem/2026-11/planet.pmtiles": {},
  });
  assert.deepEqual(opened, ["dem/2026-11/6-0-0.pmtiles"]);
  assert.equal(response.status, 204);
});

test("a missing planet archive is an uncached 404 naming it", async () => {
  const { response } = await serve(ZERMATT_Z12);
  assert.equal(response.status, 404);
  assert.equal(response.headers.get("Cache-Control"), ERROR_CACHE_CONTROL);
  assert.match(await response.text(), /dem\/2026-11\/planet\.pmtiles/);
});

test("any other read failure is not swallowed as a 204", async () => {
  const open = () => ({
    async getZxy() {
      throw new Error("R2 timed out");
    },
  });
  await assert.rejects(serveDem(new URL(`https://tiles.test${ZERMATT_Z15}`), open), /timed out/);
});
