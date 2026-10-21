// node --test server/test
import { test } from "node:test";
import assert from "node:assert/strict";
import { clean, summarize, MIN_PEERS } from "../src/compare.js";

const id = (n) => n.toString(16).padStart(32, "0");
const row = (n, boot, idle, programs) => ({ id: id(n), metrics: { boot_s: boot, idle_cpu: idle, ram_used_pct: 50 }, programs });

test("clean drops junk and keeps only known fields", () => {
  const s = clean({
    id: id(1), schema: 1, user: "vasya",
    hw: { os: "win11", cpu: "Intel Core i5-8250U", cores: 4, threads: 8, ram_gb: 8, disk: "ssd", gpu: "x".repeat(200), hostname: "VASYA-PC" },
    metrics: { boot_s: 30, idle_cpu: 500, path: "C:/Users/vasya" },
    programs: ["steam", "steam", "bad name/../", 5],
  });
  assert.equal(s.cpu, "intel core i5-8250u");
  assert.equal(s.gpu.length, 60);
  assert.equal(s.metrics.idle_cpu, null);
  assert.equal(s.metrics.path, undefined);
  assert.deepEqual(s.programs, ["steam"]);
  assert.equal(JSON.stringify(s).includes("vasya"), false);
  assert.equal(clean({ id: "nope", schema: 1 }), null);
});

test("too few peers reveal nothing", () => {
  const mine = { id: id(0), programs: ["steam"] };
  const r = summarize(mine, [row(1, 20, 2, ["steam"]), row(0, 99, 9, [])], "model");
  assert.equal(r.enough, false);
  assert.equal(r.metrics, undefined);
  assert.equal(r.programs, undefined);
});

test("medians and program shares among fast peers", () => {
  const mine = { id: id(0), programs: ["updater_junk", "steam"] };
  const rows = [];
  for (let i = 1; i <= 10; i++) {
    const slow = i > 5;
    rows.push(row(i, slow ? 60 : 20, slow ? 8 : 2, slow ? ["updater_junk", "steam"] : ["steam"]));
  }
  const r = summarize(mine, rows, "model");
  assert.equal(r.enough, true);
  assert.equal(r.peers, 10);
  assert.equal(r.metrics.boot_s.median, 40);
  assert.equal(r.programs.steam.fast, 1);
  assert.equal(r.programs.updater_junk.fast, 0);
  assert.equal(r.programs.updater_junk.all, 0.5);
  assert.deepEqual(Object.keys(r.programs).sort(), ["steam", "updater_junk"]);
  assert.ok(MIN_PEERS >= 5);
});

test("router: /compare needs the database and a subscription, /compare/forget only the database", async () => {
  const { default: worker } = await import("../src/index.js");
  const post = (path, env, headers = {}) =>
    worker.fetch(new Request(`https://x${path}`, { method: "POST", body: "{}", headers }), env);
  const env = { PUBLIC_KEY: "00", PRIVATE_KEY: "00" };
  assert.equal((await post("/compare", env)).status, 503);
  assert.equal((await post("/compare/forget", env)).status, 503);
  const db = { prepare: () => { throw new Error("must not touch the database"); } };
  assert.equal((await post("/compare", { ...env, COMPARE_DB: db }, { Authorization: "Bearer WS1.bad.bad" })).status, 401);
  assert.equal((await post("/compare/forget", { ...env, COMPARE_DB: db })).status, 400); // пустое тело — нет id
});
