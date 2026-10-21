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

const env = () => ({ LICENSES: fakeKV(), PUBLIC_KEY, PRIVATE_KEY, ADMIN_TOKEN: "admin-secret" });

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

test("подделанная лицензия не проходит", async () => {
  const e = env();
  const { data: rec } = await call(e, "/admin/create", { plan: "solo", days: 31 }, adm);
  const { data } = await call(e, "/v1/activate", { code: rec.code, device: "AAAAA-BBBBB" });
  const [h, body, sig] = data.token.split(".");
  const forged = Buffer.from(Buffer.from(body, "base64url").toString().replace('"solo"', '"family"')).toString("base64url");
  assert.equal(await readToken(e, `${h}.${forged}.${sig}`), null);
});
