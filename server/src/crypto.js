// Подпись лицензий Ed25519 и проверка — то же, что whyslow/license.py делает на стороне программы.

const enc = new TextEncoder();

export function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64url(s) {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function hex(s) {
  if (!/^[0-9a-f]{64}$/i.test(s || "")) throw new Error("ключ должен быть 64 hex-символа");
  return Uint8Array.from(s.match(/../g), (h) => parseInt(h, 16));
}

const keys = new Map();

async function signingKey(env) {
  if (!keys.has("sign")) {
    const jwk = { kty: "OKP", crv: "Ed25519", d: b64url(hex(env.PRIVATE_KEY)), x: b64url(hex(env.PUBLIC_KEY)) };
    keys.set("sign", await crypto.subtle.importKey("jwk", jwk, { name: "Ed25519" }, false, ["sign"]));
  }
  return keys.get("sign");
}

async function verifyKey(env) {
  if (!keys.has("verify")) {
    keys.set("verify", await crypto.subtle.importKey("raw", hex(env.PUBLIC_KEY), { name: "Ed25519" }, false, ["verify"]));
  }
  return keys.get("verify");
}

function concatBytes(...arrays) {
  const total = arrays.reduce((n, a) => n + a.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrays) { out.set(a, off); off += a.length; }
  return out;
}

async function hmacSha256(keyBytes, data) {
  const k = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
}

// HKDF-SHA256 (RFC 5869) — тот же вывод, что whyslow/seal.py._hkdf
async function hkdf(ikm, salt, info, length = 32) {
  const prk = await hmacSha256(salt, ikm);
  let out = new Uint8Array(0), block = new Uint8Array(0);
  for (let counter = 1; out.length < length; counter++) {
    block = await hmacSha256(prk, concatBytes(block, info, Uint8Array.of(counter)));
    out = concatBytes(out, block);
  }
  return out.slice(0, length);
}

function toHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Ключи расшифровки платных модулей для списка функций — кладём их в подписанный токен
export async function featureKeys(env, features) {
  const seed = Uint8Array.from((env.PRIVATE_KEY || "").match(/../g) || [], (h) => parseInt(h, 16));
  const salt = enc.encode("whyslow-seal-key");
  const out = {};
  for (const f of features) out[f] = toHex(await hkdf(seed, salt, enc.encode(f)));
  return out;
}

export async function makeToken(env, payload) {
  const body = enc.encode(JSON.stringify(payload));
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, await signingKey(env), body));
  return `WS1.${b64url(body)}.${b64url(sig)}`;
}

// Данные лицензии, если подпись верна и срок не вышел, иначе null
export async function readToken(env, token, now = Date.now() / 1000) {
  const parts = (token || "").split(".");
  if (parts.length !== 3 || parts[0] !== "WS1") return null;
  try {
    const body = unb64url(parts[1]);
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, await verifyKey(env), unb64url(parts[2]), body);
    if (!ok) return null;
    const data = JSON.parse(new TextDecoder().decode(body));
    return data && data.exp >= now ? data : null;
  } catch {
    return null;
  }
}

export async function sha256hex(text) {
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(text)));
  return [...h].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Сравнение секретов без утечки по времени
export function sameSecret(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || !a || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
