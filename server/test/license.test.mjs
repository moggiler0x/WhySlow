// node --test test/ — сервер с поддельным KV, без Cloudflare
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import worker from "../src/index.js";
import { requireLicense } from "../src/auth.js";
import { readToken } from "../src/crypto.js";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
// Тестовая пара ключей (RFC 8032, пример 1) — не настоящая
const PRIVATE_KEY = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
// Секрет шифрования отдельный от ключа подписи — так и на сервере
const SEAL_SECRET = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff";
const PUBLIC_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";

function fakeKV() {
  const m = new Map();
  return {
    m,
    async get(k, type) { const v = m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; },
    async put(k, v) { m.set(k, v); },
    async list({ prefix }) { return { keys: [...m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })), list_complete: true }; },
  };
}

const env = () => ({ LICENSES: fakeKV(), PUBLIC_KEY, PRIVATE_KEY, SEAL_SECRET, ADMIN_TOKEN: "admin-secret" });

async function call(e, path, body, headers = {}) {
  const r = await worker.fetch(new Request("https://x.dev" + path, {
    method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json", ...headers },
  }), e);
  return { status: r.status, data: await r.json() };
}
const adm = { Authorization: "Bearer admin-secret" };

test("админка без пароля закрыта", async () => {
  const e = env();
  assert.equal((await call(e, "/admin/create", { plan: "solo" })).status, 403);
  assert.equal((await call(e, "/admin/create", { plan: "solo" }, { Authorization: "Bearer wrong-secret" })).status, 403);
});

test("соло: одно устройство, повторная активация того же — можно", async () => {
  const e = env();
  const { data: rec } = await call(e, "/admin/create", { plan: "solo", days: 31, note: "тест" }, adm);
  assert.match(rec.code, /^WS-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$/);
  // Сам код в базе не хранится
  assert.ok(![...e.LICENSES.m.values()].some((v) => v.includes(rec.code.replace(/^WS-|-/g, ""))));
  const a = await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" });
  assert.equal(a.status, 200);
  const lic = await readToken(e, a.data.token);
  assert.equal(lic.plan, "solo");
  assert.deepEqual(lic.dev, ["AAAAA-BBBBB"]);
  assert.ok(lic.exp <= Date.now() / 1000 + 7 * 86400 + 5);
  assert.equal((await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" })).status, 200);
  assert.deepEqual((await call(e, "/v1/activate", { code: rec.code, device: "CCCCC-DDDDD" })).data, { error: "limit" });
  // Отключили на первом — второй подключается
  await call(e, "/v1/deactivate", { code: rec.code, device: "AAAAA-BBBBB" });
  assert.equal((await call(e, "/v1/activate", { code: rec.code, device: "CCCCC-DDDDD" })).status, 200);
  assert.deepEqual((await call(e, "/v1/refresh", { code: rec.code, device: "AAAAA-BBBBB" })).data, { error: "device" });
});

test("отзыв и неизвестный код", async () => {
  const e = env();
  const { data: rec } = await call(e, "/admin/create", { plan: "family", days: 31 }, adm);
  const a = await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" });
  const req = () => new Request("https://x.dev/compare", { headers: { Authorization: `Bearer ${a.data.token}` } });
  assert.equal((await requireLicense(req(), e)).ok, true);
  await call(e, "/admin/revoke", { code: rec.code }, adm);
  assert.deepEqual((await call(e, "/v1/refresh", { code: rec.code, device: "AAAAA-BBBBB" })).data, { error: "revoked" });
  assert.equal((await requireLicense(req(), e)).ok, false);
  assert.deepEqual((await call(e, "/v1/activate", { code: "WS-AAAA-AAAA-AAAA-AAAA", device: "AAAAA-BBBBB" })).data, { error: "unknown" });
});

test("лицензию сервера принимает программа", async () => {
  const e = env();
  const { data: rec } = await call(e, "/admin/create", { plan: "solo", days: 31 }, adm);
  const { data } = await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" });
  const out = execFileSync("python3", ["-c", `
import sys; from whyslow import license
d = license.read_token(sys.argv[1], public=sys.argv[2], device="AAAAA-BBBBB")
print(d["plan"], d["kid"] == license.code_kid(license.normalize_code(sys.argv[3])))`, data.token, PUBLIC_KEY, rec.code], { cwd: ROOT });
  assert.equal(out.toString().trim(), "solo True");
});

test("ключ функции с сервера расшифровывает модуль в программе", async () => {
  const e = env();
  const { data: rec } = await call(e, "/admin/create", { plan: "family", days: 31 }, adm);
  const { data } = await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" });
  // Токен несёт ключи функций; они должны совпасть с whyslow.seal.feature_key и открыть .wsl
  const out = execFileSync("python3", ["-c", `
import sys, json
from whyslow import license, seal
d = license.read_token(sys.argv[1], public=sys.argv[2], device="AAAAA-BBBBB")
seed = bytes.fromhex(sys.argv[3])
key = bytes.fromhex(d["keys"]["compare"])
assert key == seal.feature_key(seed, "compare")
blob = seal.seal_bytes(b"def build(m): return m", key)
print(seal.open_sealed(blob, key) == b"def build(m): return m")`, data.token, PUBLIC_KEY, SEAL_SECRET], { cwd: ROOT });
  assert.equal(out.toString().trim(), "True");
});

test("подделанная лицензия не проходит", async () => {
  const e = env();
  const { data: rec } = await call(e, "/admin/create", { plan: "solo", days: 31 }, adm);
  const { data } = await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" });
  const [h, body, sig] = data.token.split(".");
  const forged = Buffer.from(Buffer.from(body, "base64url").toString().replace('"solo"', '"family"')).toString("base64url");
  assert.equal(await readToken(e, `${h}.${forged}.${sig}`), null);
});

const HW = ["a".repeat(64), "b".repeat(64)];

test("пробный период: один раз на железо, переустановка не помогает", async () => {
  const e = env();
  const t = await call(e, "/v1/trial", { device: "AAAAA-BBBBB", hw: HW, name: "pc" });
  assert.equal(t.status, 200);
  const lic = await readToken(e, t.data.token);
  assert.equal(lic.trial, 1);
  assert.deepEqual(lic.dev, ["AAAAA-BBBBB"]);
  assert.ok(lic.sub <= Date.now() / 1000 + 7 * 86400 + 5 && lic.sub > Date.now() / 1000 + 6 * 86400);
  assert.ok(lic.keys.compare, "пробный период открывает платные модули");
  // Сырые хеши программы не лежат в базе — только хеш от хеша
  assert.ok(![...e.LICENSES.m.keys(), ...e.LICENSES.m.values()].some((v) => v.includes(HW[0])));
  // Продление по выданному коду работает
  assert.equal((await call(e, "/v1/refresh", { code: t.data.code, device: "AAAAA-BBBBB" })).status, 200);
  // Переустановили систему: новый код устройства, но тот же диск — отказ
  assert.deepEqual((await call(e, "/v1/trial", { device: "CCCCC-DDDDD", hw: [HW[1], "c".repeat(64)] })).data,
    { error: "trial_used" });
  // Код пробного периода нельзя перенести на другой компьютер, даже «отключив»
  await call(e, "/v1/deactivate", { code: t.data.code, device: "AAAAA-BBBBB" });
  assert.deepEqual((await call(e, "/v1/activate", { code: t.data.code, device: "CCCCC-DDDDD" })).data,
    { error: "trial_device" });
  assert.equal((await call(e, "/v1/refresh", { code: t.data.code, device: "AAAAA-BBBBB" })).status, 200);
});

test("пробный период закончился — trial_over, и без хешей железа не выдаётся", async () => {
  const e = env();
  assert.equal((await call(e, "/v1/trial", { device: "AAAAA-BBBBB", hw: [] })).status, 400);
  assert.equal((await call(e, "/v1/trial", { device: "AAAAA-BBBBB", hw: ["серийник"] })).status, 400);
  const t = await call(e, "/v1/trial", { device: "AAAAA-BBBBB", hw: HW });
  for (const [k, v] of e.LICENSES.m) {
    if (k.startsWith("lic:")) e.LICENSES.m.set(k, JSON.stringify({ ...JSON.parse(v), exp: 1 }));
  }
  assert.deepEqual((await call(e, "/v1/refresh", { code: t.data.code, device: "AAAAA-BBBBB" })).data, { error: "trial_over" });
});

test("пробных периодов с одного адреса — не больше трёх в сутки", async () => {
  const e = env();
  const ip = { "CF-Connecting-IP": "203.0.113.5" };
  for (let i = 0; i < 3; i++) {
    assert.equal((await call(e, "/v1/trial", { device: "AAAAA-BBBBB", hw: [String(i).repeat(64)] }, ip)).status, 200);
  }
  assert.deepEqual((await call(e, "/v1/trial", { device: "AAAAA-BBBBB", hw: ["9".repeat(64)] }, ip)).data, { error: "busy" });
  assert.ok(![...e.LICENSES.m.keys()].some((k) => k.includes("203.0.113.5")));
});
