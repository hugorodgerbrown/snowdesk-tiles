// Routing tests for the bucket-backed tilesets (SNOW-987).
//
//     cd worker && npm test
//
// node:test and a fake R2 binding — no dependencies, so this runs without
// `npm install`. What is pinned is the part a deploy cannot show you until a
// client trips over it: the version segment is stripped from the bucket key,
// an absent tile is a cacheable 204 and never a 404, a zoom the class tiles
// are not built at is a 204 without an R2 read, and a missing descriptor is a
// loud, uncached 404.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ERROR_CACHE_CONTROL,
  TILE_CACHE_CONTROL,
  TILEJSON_CACHE_CONTROL,
  resolve,
  serveTileset,
  tilesetFor,
} from "../src/tilesets.js";

/** An R2 binding holding `objects`, recording every key it is asked for. */
function bucket(objects) {
  const reads = [];
  return {
    reads,
    BUCKET: {
      async get(key) {
        reads.push(key);
        return key in objects ? { body: objects[key] } : null;
      },
    },
  };
}

async function serve(path, objects = {}) {
  const env = bucket(objects);
  const url = new URL(`https://tiles.test${path}`);
  const tileset = tilesetFor(url.pathname);
  assert.ok(tileset, `no tileset for ${path}`);
  const response = await serveTileset(tileset, env, url);
  return { response, reads: env.reads };
}

test("each prefix finds its own tileset", () => {
  assert.equal(tilesetFor("/terrain/v1/3239/1990.s16").name, "terrain");
  assert.equal(tilesetFor("/terrain-class/v1/14/8544/5827.png").name, "terrain-class");
  assert.equal(tilesetFor("/tiles/v1/0/0/0.mvt"), null);
  assert.equal(tilesetFor("/styles/liberty"), null);
});

test("a class tile is served as PNG with the version stripped", async () => {
  const { response, reads } = await serve("/terrain-class/v7/14/8544/5827.png", {
    "terrain-class/14/8544/5827.png": "png bytes",
  });

  assert.deepEqual(reads, ["terrain-class/14/8544/5827.png"]);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "image/png");
  assert.equal(response.headers.get("Cache-Control"), TILE_CACHE_CONTROL);
  assert.equal(await response.text(), "png bytes");
});

test("an absent class tile is a cacheable 204, not a 404", async () => {
  const { response } = await serve("/terrain-class/v1/14/1/1.png");

  assert.equal(response.status, 204);
  assert.equal(response.headers.get("Cache-Control"), TILE_CACHE_CONTROL);
});

for (const zoom of [0, 11, 15, 22]) {
  test(`z${zoom} is a 204 without reading R2`, async () => {
    const { response, reads } = await serve(`/terrain-class/v1/${zoom}/1/1.png`);

    assert.equal(response.status, 204);
    assert.equal(response.headers.get("Cache-Control"), TILE_CACHE_CONTROL);
    assert.deepEqual(reads, []);
  });
}

for (const zoom of [12, 13, 14]) {
  test(`z${zoom} is read from the bucket`, async () => {
    const { reads } = await serve(`/terrain-class/v1/${zoom}/2/3.png`);

    assert.deepEqual(reads, [`terrain-class/${zoom}/2/3.png`]);
  });
}

test("the class descriptor is JSON with the short TTL", async () => {
  const { response, reads } = await serve("/terrain-class/v1/tiles.json", {
    "terrain-class/tiles.json": "{}",
  });

  assert.deepEqual(reads, ["terrain-class/tiles.json"]);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "application/json");
  assert.equal(response.headers.get("Cache-Control"), TILEJSON_CACHE_CONTROL);
});

test("a missing descriptor is an uncached 404, never an empty 204", async () => {
  const { response } = await serve("/terrain-class/v1/tiles.json");

  assert.equal(response.status, 404);
  assert.equal(response.headers.get("Cache-Control"), ERROR_CACHE_CONTROL);
});

for (const path of [
  "/terrain-class/v1/14/-1/2.png",
  "/terrain-class/v1/14/1/2.webp",
  "/terrain-class/14/1/2.png",
  "/terrain-class/v1/14/1.png",
  "/terrain-class/v1/grid.json",
]) {
  test(`${path} is a 404`, async () => {
    const { response, reads } = await serve(path);

    assert.equal(response.status, 404);
    assert.equal(response.headers.get("Cache-Control"), ERROR_CACHE_CONTROL);
    assert.deepEqual(reads, []);
  });
}

test("terrain tiles keep their route: octet-stream, version stripped", async () => {
  const { response, reads } = await serve("/terrain/v3/3239/1990.s16", {
    "terrain/3239/1990.s16": "int16",
  });

  assert.deepEqual(reads, ["terrain/3239/1990.s16"]);
  assert.equal(response.headers.get("Content-Type"), "application/octet-stream");
});

test("terrain has no zoom, so no zoom gate", () => {
  assert.deepEqual(resolve(tilesetFor("/terrain/"), "/terrain/v1/1/2.s16"), {
    kind: "tile",
    key: "terrain/1/2.s16",
  });
});

test("a negative terrain index is a 404, not missing coverage", async () => {
  const { response } = await serve("/terrain/v1/-1/-1.s16");

  assert.equal(response.status, 404);
});

test("an absent terrain tile is a 204 and grid.json is served", async () => {
  assert.equal((await serve("/terrain/v1/1/1.s16")).response.status, 204);
  const { response } = await serve("/terrain/v1/grid.json", {
    "terrain/grid.json": "{}",
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Type"), "application/json");
});
